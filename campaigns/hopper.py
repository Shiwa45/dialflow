# campaigns/hopper.py
"""
Hopper Engine
=============
The hopper is the dial queue. It has two layers:

  1. PostgreSQL  — HopperEntry rows (durable, auditable)
  2. Redis list  — fast ordered queue (campaign:{id}:hopper)

Fill flow:
  Celery beat calls fill_all_hoppers() every 30s.
  For each active campaign below target, pull leads from DB → push to Redis.

Dequeue flow:
  Predictive dialer calls pop_lead(campaign_id) → gets next lead atomically.
"""
import json
import logging
import time
from typing import Optional, Dict

import redis as redis_lib
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger('dialflow')

# ── Redis connection (singleton) ──────────────────────────────────────────────
_redis: Optional[redis_lib.Redis] = None


def get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        _redis = redis_lib.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis


def hopper_key(campaign_id: int) -> str:
    return f'campaign:{campaign_id}:hopper'


def dialing_key(campaign_id: int) -> str:
    return f'campaign:{campaign_id}:dialing'


# ── Fill ──────────────────────────────────────────────────────────────────────

def fill_hopper(campaign_id: int, target: int = None) -> int:
    """
    Fill the Redis hopper for a campaign up to its target level.
    Returns the number of leads added.
    """
    from campaigns.models import Campaign, HopperEntry, DNCEntry
    from leads.models import Lead, LeadAttempt

    try:
        campaign = Campaign.objects.select_related('asterisk_server').get(
            id=campaign_id, status=Campaign.STATUS_ACTIVE
        )
    except Campaign.DoesNotExist:
        return 0

    r   = get_redis()
    key = hopper_key(campaign_id)

    current_count = r.llen(key)
    target_count  = target or campaign.hopper_level

    if current_count >= target_count:
        return 0

    needed = target_count - current_count

    # IDs already in hopper or currently dialing — skip them
    existing_ids = set()
    for raw in r.lrange(key, 0, -1):
        try:
            existing_ids.add(json.loads(raw)['lead_id'])
        except Exception:
            pass
    for raw in r.hvals(dialing_key(campaign_id)):
        try:
            existing_ids.add(json.loads(raw)['lead_id'])
        except Exception:
            pass

    # Build lead query
    lead_qs = Lead.objects.filter(
        campaigns=campaign,
        is_active=True,
        do_not_call=False,
    ).exclude(
        id__in=existing_ids,
    )

    # Exclude leads that hit max attempts
    if campaign.max_attempts > 0:
        over_limit = LeadAttempt.objects.filter(
            campaign=campaign,
            attempt_number__gte=campaign.max_attempts,
        ).values_list('lead_id', flat=True)
        lead_qs = lead_qs.exclude(id__in=over_limit)

    # Exclude DNC
    dnc_numbers = DNCEntry.objects.filter(
        Q(campaign__isnull=True) | Q(campaign_id=campaign_id)
    ).values_list('phone_number', flat=True)
    lead_qs = lead_qs.exclude(primary_phone__in=dnc_numbers)

    # Order
    order_map = {
        'sequential': ['id'],
        'random':     ['?'],
        'priority':   ['-priority', 'id'],
        'newest':     ['-created_at'],
        'oldest':     ['created_at'],
    }
    lead_qs = lead_qs.order_by(*order_map.get(campaign.lead_order, ['id']))

    # Push to Redis
    added = 0
    pipe  = r.pipeline()
    for lead in lead_qs[:needed]:
        slot = json.dumps({
            'lead_id':    lead.id,
            'phone':      lead.primary_phone,
            'first_name': lead.first_name,
            'last_name':  lead.last_name,
            'queued_at':  time.time(),
        })
        pipe.rpush(key, slot)

        # Create HopperEntry row
        HopperEntry.objects.get_or_create(
            campaign=campaign,
            lead=lead,
            status=HopperEntry.STATUS_QUEUED,
            defaults={'phone_number': lead.primary_phone},
        )
        added += 1

    pipe.execute()

    if added:
        logger.debug(f'Hopper fill: campaign {campaign_id} +{added} leads (total={current_count + added})')

    return added


def fill_all_hoppers() -> Dict[int, int]:
    """Fill hoppers for all active campaigns. Called by Celery beat."""
    from campaigns.models import Campaign
    results = {}
    for campaign in Campaign.objects.filter(status=Campaign.STATUS_ACTIVE):
        try:
            results[campaign.id] = fill_hopper(campaign.id)
        except Exception as e:
            logger.error(f'Hopper fill failed for campaign {campaign.id}: {e}')
    return results


# ── Dequeue ───────────────────────────────────────────────────────────────────

def pop_lead(campaign_id: int) -> Optional[Dict]:
    """
    Pop the next lead from the Redis hopper atomically.
    Pushes it into the dialing hash so we know it's in-flight.
    Returns lead dict or None if hopper is empty.
    """
    r   = get_redis()
    key = hopper_key(campaign_id)
    raw = r.lpop(key)
    if not raw:
        return None

    data = json.loads(raw)
    # Track as in-flight
    r.hset(dialing_key(campaign_id), data['lead_id'], raw)
    # TTL safety: auto-clean after 5 minutes if ARI never reports completion
    r.expire(dialing_key(campaign_id), 300)

    logger.debug(f'Hopper pop: campaign {campaign_id} → lead {data["lead_id"]}')
    return data


def complete_lead(campaign_id: int, lead_id: int):
    """Remove lead from the in-flight dialing set after call ends."""
    r = get_redis()
    r.hdel(dialing_key(campaign_id), lead_id)


def get_hopper_stats(campaign_id: int) -> Dict:
    r         = get_redis()
    queued    = r.llen(hopper_key(campaign_id))
    in_flight = r.hlen(dialing_key(campaign_id))
    return {'queued': queued, 'in_flight': in_flight, 'total': queued + in_flight}


def reset_stale_dialing(campaign_id: int, max_age_seconds: int = 300):
    """
    Move leads stuck in 'dialing' back to the hopper if ARI never
    reported completion (crash recovery).
    """
    r   = get_redis()
    now = time.time()
    stale = []

    for lead_id, raw in r.hgetall(dialing_key(campaign_id)).items():
        try:
            data = json.loads(raw)
            age  = now - data.get('queued_at', now)
            if age > max_age_seconds:
                stale.append((lead_id, raw))
        except Exception:
            stale.append((lead_id, raw))

    if stale:
        pipe = r.pipeline()
        for lead_id, raw in stale:
            pipe.hdel(dialing_key(campaign_id), lead_id)
            pipe.rpush(hopper_key(campaign_id), raw)  # re-queue at end
        pipe.execute()
        logger.warning(f'Reset {len(stale)} stale dialing entries for campaign {campaign_id}')

    return len(stale)
