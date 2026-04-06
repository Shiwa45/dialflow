# campaigns/predictive.py
"""Vicidial-style Adaptive Predictive Dialing Engine"""
import logging, math
from dataclasses import dataclass
from typing import Optional, List
from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger("dialflow.predictive")


@dataclass
class DialerMetrics:
    agents_ready:     int   = 0
    agents_on_call:   int   = 0
    agents_wrapup:    int   = 0
    avg_talk_time:    float = 120.0
    avg_wrapup_time:  float = 45.0
    answer_rate:      float = 60.0
    abandon_rate:     float = 0.0
    calls_in_flight:  int   = 0
    amd_machine_rate: float = 0.0


def _erlang_c(n_agents: int, intensity: float) -> float:
    if n_agents <= 0 or intensity <= 0: return 1.0
    if intensity >= n_agents: return 1.0
    try:
        erlang_b_sum  = sum((intensity**k)/math.factorial(k) for k in range(n_agents))
        erlang_b_term = (intensity**n_agents)/(math.factorial(n_agents)*(1.0-intensity/n_agents))
        p0 = 1.0/(erlang_b_sum+erlang_b_term)
        ec = ((intensity**n_agents)/(math.factorial(n_agents)*(1.0-intensity/n_agents)))*p0
        return min(ec, 1.0)
    except (ZeroDivisionError, OverflowError):
        return 1.0


def calculate_dial_ratio(metrics: DialerMetrics, campaign) -> float:
    min_ratio  = float(campaign.min_dial_ratio)
    max_ratio  = float(campaign.max_dial_ratio)
    target_abn = float(campaign.abandon_rate)
    n_ready    = metrics.agents_ready

    if n_ready <= 0: return 0.0

    # Governor 1: Hard throttle on abandon rate
    if metrics.abandon_rate > target_abn * 1.5:
        logger.warning(f"Campaign {campaign.id}: abandon throttle -> min ratio")
        return min_ratio

    ar = max(metrics.answer_rate / 100.0, 0.01)

    # Governor 2: Low answer rate (congestion)
    if ar < 0.15:
        return min(min_ratio + 0.3, max_ratio)

    # Erlang-C calculation
    intensity  = n_ready * 0.90   # target 90% utilisation
    ec         = _erlang_c(n_ready, intensity)
    base_ratio = (intensity/n_ready)/ar

    # EC adjustment
    if ec > 0.35:   adjustment = 1.0 - (ec - 0.35)*0.5
    elif ec < 0.05: adjustment = 1.0 + (0.05 - ec)*2.0
    else:           adjustment = 1.0

    ratio = base_ratio * adjustment

    # Abandon rate adjustment
    if metrics.abandon_rate > target_abn:
        overrun = (metrics.abandon_rate - target_abn)/max(target_abn, 1)
        ratio   = ratio*(1.0 - min(overrun*0.3, 0.4))

    # In-flight dampening
    if metrics.calls_in_flight > n_ready:
        ratio = max(ratio - ((metrics.calls_in_flight-n_ready)/n_ready), min_ratio)

    return max(min_ratio, min(max_ratio, round(ratio, 2)))


def _collect_metrics(campaign_id: int, assigned_ids: list) -> DialerMetrics:
    from agents.models import AgentStatus
    from calls.models import CallLog
    from campaigns.hopper import get_hopper_stats

    sc = AgentStatus.objects.filter(user_id__in=assigned_ids).aggregate(
        ready   = Count("id", filter=Q(status="ready")),
        on_call = Count("id", filter=Q(status="on_call")),
        wrapup  = Count("id", filter=Q(status="wrapup")),
    )
    recent = CallLog.objects.filter(campaign_id=campaign_id, started_at__isnull=False).order_by("-started_at")[:200]
    total  = recent.count()
    hopper = get_hopper_stats(campaign_id)

    if total < 5:
        return DialerMetrics(
            agents_ready=sc["ready"] or 0, agents_on_call=sc["on_call"] or 0,
            agents_wrapup=sc["wrapup"] or 0, calls_in_flight=hopper["in_flight"],
        )

    agg = recent.aggregate(
        avg_talk    = Avg("duration", filter=Q(status="completed")),
        answered    = Count("id", filter=Q(status="completed")),
        dropped     = Count("id", filter=Q(status="dropped")),
        amd_machine = Count("id", filter=Q(amd_result__icontains="machine")),
    )
    answered = agg["answered"] or 0
    dropped  = agg["dropped"]  or 0
    amd_m    = agg["amd_machine"] or 0
    ar       = (answered/total*100) if total > 0 else 60.0
    amd_rate = (amd_m/total*100)   if total > 0 else 0.0
    abr      = (dropped/max(answered,1)*100) if answered > 0 else 0.0

    return DialerMetrics(
        agents_ready     = sc["ready"]   or 0,
        agents_on_call   = sc["on_call"] or 0,
        agents_wrapup    = sc["wrapup"]  or 0,
        avg_talk_time    = float(agg["avg_talk"] or 120),
        answer_rate      = ar*(1.0-amd_rate/100.0),
        abandon_rate     = abr,
        amd_machine_rate = amd_rate,
        calls_in_flight  = hopper["in_flight"],
    )


def get_calls_to_dial(campaign_id: int) -> int:
    from campaigns.models import Campaign
    from campaigns.hopper import get_hopper_stats

    try:
        campaign = Campaign.objects.select_related("asterisk_server","carrier").get(
            id=campaign_id, status=Campaign.STATUS_ACTIVE)
    except Campaign.DoesNotExist:
        return 0

    try:
        import pytz
        tz  = pytz.timezone("Asia/Kolkata")
        now = timezone.now().astimezone(tz)
        if not (campaign.call_hour_start <= now.time() <= campaign.call_hour_end):
            return 0
    except Exception:
        pass

    hopper = get_hopper_stats(campaign_id)
    if hopper["queued"] == 0: return 0

    assigned_ids = list(campaign.agents.filter(is_active=True).values_list("agent_id", flat=True))
    if not assigned_ids: return 0

    metrics = _collect_metrics(campaign_id, assigned_ids)
    if metrics.agents_ready == 0: return 0

    if campaign.dial_mode == Campaign.DIAL_MODE_PREVIEW:
        return 0

    elif campaign.dial_mode == Campaign.DIAL_MODE_PROGRESSIVE:
        return max(0, min(metrics.agents_ready - metrics.calls_in_flight, hopper["queued"]))

    elif campaign.dial_mode == Campaign.DIAL_MODE_PREDICTIVE:
        ratio   = calculate_dial_ratio(metrics, campaign)
        if ratio <= 0: return 0
        target  = math.ceil(ratio * metrics.agents_ready)
        to_dial = max(0, min(target - metrics.calls_in_flight, hopper["queued"]))
        logger.info(f"Predictive: campaign={campaign_id} ratio={ratio} ready={metrics.agents_ready} flight={metrics.calls_in_flight} -> {to_dial}")
        return to_dial

    return 0


def get_longest_waiting_agent(campaign_id: int) -> Optional[int]:
    from agents.models import AgentStatus
    from campaigns.models import CampaignAgent
    assigned = CampaignAgent.objects.filter(campaign_id=campaign_id, is_active=True).values_list("agent_id", flat=True)
    agent = AgentStatus.objects.filter(user_id__in=assigned, status="ready").order_by("status_changed_at").first()
    return agent.user_id if agent else None


def get_ready_agents_ordered(campaign_id: int) -> List[int]:
    from agents.models import AgentStatus
    from campaigns.models import CampaignAgent
    assigned = CampaignAgent.objects.filter(campaign_id=campaign_id, is_active=True).values_list("agent_id", flat=True)
    return list(AgentStatus.objects.filter(user_id__in=assigned, status="ready").order_by("status_changed_at").values_list("user_id", flat=True))


def originate_calls(campaign_id: int, count: int) -> int:
    from campaigns.hopper import pop_lead, get_redis, hopper_key
    from campaigns.models import Campaign
    from calls.models import CallLog
    import requests as req_lib, json

    if count <= 0: return 0
    try:
        campaign = Campaign.objects.select_related("asterisk_server","carrier").get(id=campaign_id)
    except Campaign.DoesNotExist:
        return 0

    server    = campaign.asterisk_server
    ari_base  = f"http://{server.ari_host}:{server.ari_port}/ari"
    auth      = (server.ari_username, server.ari_password)
    initiated = 0

    for _ in range(count):
        lead_data = pop_lead(campaign_id)
        if not lead_data: break

        phone   = lead_data["phone"]
        lead_id = lead_data["lead_id"]
        dial_no = f"{campaign.dial_prefix}{phone}" if campaign.dial_prefix else phone
        endpoint = f"PJSIP/{dial_no}@{campaign.carrier.name}" if campaign.carrier else f"PJSIP/{dial_no}@dialout"

        variables = {"CALL_TYPE":"autodial","CAMPAIGN_ID":str(campaign_id),"LEAD_ID":str(lead_id),"CUSTOMER_NUMBER":phone}

        if campaign.amd_enabled:
            variables.update({
                "AMD_ENABLED":"1","AMD_ACTION":campaign.amd_action,
                "AMD_INITIAL_SILENCE":"3000","AMD_GREETING":"1500",
                "AMD_AFTER_GREETING_SILENCE":"800","AMD_TOTAL_ANALYSIS_TIME":"5000",
                "AMD_MIN_WORD_LENGTH":"100","AMD_BETWEEN_WORDS_SILENCE":"50",
                "AMD_MAX_WORDS":"3","AMD_SILENCE_THRESHOLD":"256",
            })

        try:
            resp = req_lib.post(f"{ari_base}/channels",json={
                "endpoint":endpoint,"app":server.ari_app_name,
                "callerId":campaign.caller_id or "","timeout":campaign.dial_timeout,"variables":variables,
            }, auth=auth, timeout=5)
            resp.raise_for_status()
            channel_id = resp.json().get("id","")
            CallLog.objects.create(
                campaign_id=campaign_id, lead_id=lead_id, channel_id=channel_id,
                phone_number=phone, direction="outbound", status="initiated", started_at=timezone.now(),
            )
            initiated += 1
        except Exception as exc:
            logger.error(f"ARI originate failed: lead={lead_id} error={exc}")
            try:
                get_redis().rpush(hopper_key(campaign_id), json.dumps(lead_data))
            except Exception:
                pass

    return initiated
