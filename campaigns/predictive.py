# campaigns/predictive.py
"""
Predictive Dialing Algorithm
=============================
Based on Erlang-C queuing theory with real-time adaptive adjustments.

Core logic (runs every second via Celery beat):
  1. Read real-time metrics from DB (agents available, talk time, answer rate)
  2. Calculate optimal dial ratio using Erlang-C formula
  3. Constrain by abandon rate threshold
  4. Pop N leads from hopper and originate via ARI

Key guarantees:
  - Dial ratio is always read from DB — never from JS/virtual state
  - If abandon rate is too high, ratio is throttled immediately
  - Each campaign is independent — no shared state
"""
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings
from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger('dialflow')


@dataclass
class DialerMetrics:
    agents_ready:    int   = 0
    agents_on_call:  int   = 0
    agents_wrapup:   int   = 0
    avg_talk_time:   float = 120.0   # seconds
    avg_wrapup_time: float = 45.0    # seconds
    avg_ring_time:   float = 20.0    # seconds
    answer_rate:     float = 60.0    # percent
    abandon_rate:    float = 0.0     # percent
    calls_in_flight: int   = 0


def _erlang_c(n_agents: int, intensity: float) -> float:
    """
    Erlang-C formula — probability a call must wait (P_wait).
    intensity = arrival_rate / service_rate (offered traffic in Erlangs)
    n_agents  = number of agents serving
    """
    if n_agents <= 0 or intensity <= 0:
        return 1.0
    if intensity >= n_agents:
        # Overloaded — every call will wait
        return 1.0

    # P_0 (probability system is empty)
    sum_terms = sum((intensity ** k) / math.factorial(k) for k in range(n_agents))
    erlang_b_inv = sum_terms + (intensity ** n_agents) / (math.factorial(n_agents) * (1 - intensity / n_agents))
    p0 = 1.0 / erlang_b_inv

    # Erlang-C
    ec = (
        (intensity ** n_agents / (math.factorial(n_agents) * (1 - intensity / n_agents)))
        * p0
    )
    return min(ec, 1.0)


def calculate_dial_ratio(metrics: DialerMetrics, campaign) -> float:
    """
    Calculate the optimal dial ratio for the next tick.

    Returns: float — calls to originate per available agent
    """
    min_ratio = float(campaign.min_dial_ratio)
    max_ratio = float(campaign.max_dial_ratio)
    target_abandon = float(campaign.abandon_rate)

    agents_available = metrics.agents_ready
    if agents_available <= 0:
        return 0.0

    # If abandon rate is already too high → throttle hard
    if metrics.abandon_rate > target_abandon * 1.5:
        logger.warning(
            f'Campaign {campaign.id}: abandon rate {metrics.abandon_rate:.1f}% > '
            f'{target_abandon * 1.5:.1f}% threshold. Throttling to min ratio.'
        )
        return min_ratio

    # Traffic intensity (Erlangs) = agents × (talk_time / (talk_time + ring_time))
    service_time = metrics.avg_talk_time + metrics.avg_wrapup_time
    if service_time <= 0:
        service_time = 165.0

    # Expected answer rate as fraction
    ar = max(metrics.answer_rate / 100.0, 0.01)

    # How many raw dials do we need to keep N agents busy?
    # Each dial has `ar` probability of producing a call.
    # Target: keep agents busy ~85% of the time (safety factor).
    target_agents_busy = max(1, agents_available * 0.85)

    # Erlang-C: how many calls-in-flight to achieve target utilisation
    intensity = target_agents_busy
    ec        = _erlang_c(agents_available, intensity)

    # Adjust: if EC wait probability is high, we have enough calls — back off
    if ec > 0.3:
        ratio = min_ratio + (max_ratio - min_ratio) * 0.3
    else:
        # Ratio = (desired_calls / ar) / agents
        desired_calls = agents_available * (1.0 / (1.0 - ec))
        ratio         = desired_calls / (ar * agents_available)

    # Clamp to campaign limits
    ratio = max(min_ratio, min(max_ratio, ratio))

    # Safety: if answer rate is very low, don't dial too aggressively
    if ar < 0.2:
        ratio = min(ratio, min_ratio + 0.5)

    return round(ratio, 2)


def get_calls_to_dial(campaign_id: int) -> int:
    """
    Top-level function called every second by Celery beat.
    Returns: how many calls to originate for this campaign right now.
    """
    from campaigns.models import Campaign
    from agents.models import AgentStatus
    from campaigns.hopper import get_hopper_stats

    try:
        campaign = Campaign.objects.get(id=campaign_id, status=Campaign.STATUS_ACTIVE)
    except Campaign.DoesNotExist:
        return 0

    # Don't dial if no hopper
    hopper = get_hopper_stats(campaign_id)
    if hopper['queued'] == 0:
        return 0

    # Get real-time agent counts from DB
    assigned_ids = campaign.agents.filter(is_active=True).values_list('agent_id', flat=True)

    status_counts = AgentStatus.objects.filter(user_id__in=assigned_ids).aggregate(
        ready   = Count('id', filter=Q(status='ready')),
        on_call = Count('id', filter=Q(status='on_call')),
        wrapup  = Count('id', filter=Q(status='wrapup')),
    )

    agents_ready = status_counts['ready']   or 0
    agents_busy  = (status_counts['on_call'] or 0) + (status_counts['wrapup'] or 0)

    if agents_ready == 0:
        return 0  # No point dialing if no one can take the call

    # Pull rolling metrics from DB (last 100 calls today)
    from calls.models import CallLog
    from django.utils import timezone as tz
    today = tz.now().date()
    recent = CallLog.objects.filter(
        campaign_id=campaign_id,
        started_at__date=today,
        status='completed',
    ).aggregate(
        avg_talk   = Avg('duration'),
        total      = Count('id'),
        answered   = Count('id', filter=Q(status='completed')),
        abandoned  = Count('id', filter=Q(status='dropped')),
    )

    total    = recent['total']    or 1
    answered = recent['answered'] or 0
    abandoned= recent['abandoned'] or 0

    metrics = DialerMetrics(
        agents_ready   = agents_ready,
        agents_on_call = agents_busy,
        avg_talk_time  = float(recent['avg_talk'] or 120),
        answer_rate    = (answered / total * 100) if total > 0 else 60.0,
        abandon_rate   = (abandoned / total * 100) if total > 0 else 0.0,
        calls_in_flight= hopper['in_flight'],
    )

    if campaign.dial_mode == Campaign.DIAL_MODE_PROGRESSIVE:
        # One call per ready agent — simple
        ratio = 1.0
    elif campaign.dial_mode == Campaign.DIAL_MODE_PREDICTIVE:
        ratio = calculate_dial_ratio(metrics, campaign)
    else:
        # Preview mode — no auto-dialing
        return 0

    # Total calls to place = ratio × ready agents - already in flight
    target_calls = int(math.ceil(ratio * agents_ready))
    to_dial      = max(0, target_calls - hopper['in_flight'])

    # Never exceed hopper size
    to_dial = min(to_dial, hopper['queued'])

    logger.debug(
        f'Campaign {campaign_id}: ratio={ratio} ready={agents_ready} '
        f'in_flight={hopper["in_flight"]} → dial {to_dial}'
    )

    return to_dial


def originate_calls(campaign_id: int, count: int) -> int:
    """
    Pop `count` leads from hopper and originate via ARI.
    Returns number of calls actually initiated.
    """
    from campaigns.hopper import pop_lead
    from campaigns.models import Campaign
    from calls.models import CallLog
    import requests as req_lib
    from django.utils import timezone

    if count <= 0:
        return 0

    try:
        campaign = Campaign.objects.select_related('asterisk_server', 'carrier').get(id=campaign_id)
    except Campaign.DoesNotExist:
        return 0

    server   = campaign.asterisk_server
    ari_base = f'http://{server.ari_host}:{server.ari_port}/ari'
    auth     = (server.ari_username, server.ari_password)

    initiated = 0

    for _ in range(count):
        lead_data = pop_lead(campaign_id)
        if not lead_data:
            break

        phone  = lead_data['phone']
        lead_id = lead_data['lead_id']

        # Prepend dial prefix if configured
        dial_number = f"{campaign.dial_prefix}{phone}" if campaign.dial_prefix else phone

        # Determine carrier endpoint
        carrier_endpoint = 'PJSIP/dialout'  # default trunk
        if campaign.carrier:
            carrier_endpoint = f"PJSIP/{dial_number}@{campaign.carrier.name}"

        variables = {
            'CALL_TYPE':       'autodial',
            'CAMPAIGN_ID':     str(campaign_id),
            'LEAD_ID':         str(lead_id),
            'CUSTOMER_NUMBER': phone,
        }
        if campaign.amd_enabled:
            variables['AMD_ENABLED'] = '1'

        payload = {
            'endpoint':  carrier_endpoint,
            'app':       server.ari_app_name,
            'callerId':  campaign.caller_id or (campaign.carrier.caller_id if campaign.carrier else ''),
            'timeout':   campaign.dial_timeout,
            'variables': variables,
        }

        try:
            r = req_lib.post(
                f'{ari_base}/channels',
                json=payload,
                auth=auth,
                timeout=5,
            )
            r.raise_for_status()
            channel_id = r.json().get('id', '')

            # Create CallLog immediately
            CallLog.objects.create(
                campaign_id  = campaign_id,
                lead_id      = lead_id,
                channel_id   = channel_id,
                phone_number = phone,
                direction    = 'outbound',
                status       = 'initiated',
                started_at   = timezone.now(),
            )

            initiated += 1
            logger.info(f'Originated: campaign={campaign_id} lead={lead_id} channel={channel_id}')

        except Exception as e:
            logger.error(f'ARI originate failed for lead {lead_id}: {e}')
            # Re-queue lead back to hopper
            from campaigns.hopper import get_redis, hopper_key
            import json
            r2 = get_redis()
            r2.rpush(hopper_key(campaign_id), json.dumps(lead_data))

    return initiated
