# campaigns/tasks.py
import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('dialflow.dialer')


@shared_task(name='campaigns.tasks.predictive_dial_tick', max_retries=0)
def predictive_dial_tick():
    """
    Runs every second via Celery beat.
    For each active campaign: calculate how many calls to dial → originate.
    """
    from campaigns.models import Campaign
    from campaigns.predictive import get_calls_to_dial, originate_calls

    total = 0
    for campaign in Campaign.objects.filter(status=Campaign.STATUS_ACTIVE):
        try:
            count = get_calls_to_dial(campaign.id)
            logger.debug(f'Predictive tick: campaign={campaign.id} computed_to_dial={count}')
            if count > 0:
                initiated = originate_calls(campaign.id, count)
                total += initiated
        except Exception as e:
            logger.error(f'Predictive dial tick failed for campaign {campaign.id}: {e}')

    logger.info(f'Predictive tick summary: initiated={total}')
    return {'initiated': total, 'ts': timezone.now().isoformat()}


@shared_task(name='campaigns.tasks.fill_all_hoppers')
def fill_all_hoppers():
    """Fill Redis hopper for all active campaigns (every 30s)."""
    from campaigns.hopper import fill_all_hoppers as _fill
    results = _fill()
    total   = sum(results.values())
    if total:
        logger.info(f'Hopper fill: +{total} leads across {len(results)} campaigns')
    else:
        logger.debug('Hopper fill summary: no leads added')
    return results


@shared_task(name='campaigns.tasks.reset_stale_hopper_entries')
def reset_stale_hopper_entries():
    """Reset leads stuck in dialing state (crash recovery, every 2 min)."""
    from campaigns.models import Campaign
    from campaigns.hopper import reset_stale_dialing
    total = 0
    for campaign in Campaign.objects.filter(status=Campaign.STATUS_ACTIVE):
        try:
            total += reset_stale_dialing(campaign.id)
        except Exception as e:
            logger.error(f'Stale reset failed for campaign {campaign.id}: {e}')
    return {'reset': total}


@shared_task(name='campaigns.tasks.update_campaign_stats')
def update_campaign_stats():
    """
    Update campaign stat columns (calls_today, answered, abandon_rate, agents_active).
    Runs every 30s. Supervisor WebSocket will pick up via broadcast.
    """
    from campaigns.models import Campaign
    from calls.models import CallLog
    from django.db.models import Count, Q
    from core.ws_utils import broadcast_supervisor

    today = timezone.now().date()

    for campaign in Campaign.objects.filter(status__in=[Campaign.STATUS_ACTIVE, Campaign.STATUS_PAUSED]):
        try:
            agg = CallLog.objects.filter(
                campaign=campaign,
                started_at__date=today,
            ).aggregate(
                total    = Count('id'),
                answered = Count('id', filter=Q(status='completed')),
                dropped  = Count('id', filter=Q(status='dropped')),
            )
            total    = agg['total']    or 0
            answered = agg['answered'] or 0
            dropped  = agg['dropped']  or 0
            abandon  = round((dropped / total * 100), 2) if total > 0 else 0

            active_agents = campaign.get_active_agents_count()

            Campaign.objects.filter(id=campaign.id).update(
                stat_calls_today    = total,
                stat_answered_today = answered,
                stat_abandon_rate   = abandon,
                stat_agents_active  = active_agents,
            )

            # Push live stats to supervisor dashboard via WS
            broadcast_supervisor({
                'type':        'campaign_stats',
                'campaign_id': campaign.id,
                'calls_today': total,
                'answered':    answered,
                'abandon_rate': abandon,
                'agents_active': active_agents,
            })

        except Exception as e:
            logger.error(f'Stats update failed for campaign {campaign.id}: {e}')

    return {'updated': timezone.now().isoformat()}


@shared_task(name='campaigns.tasks.recycle_failed_calls')
def recycle_failed_calls():
    """
    Re-queue leads whose calls failed (no-answer / busy) and
    whose retry delay has elapsed. Runs every 5 minutes.
    """
    from campaigns.models import Campaign, HopperEntry
    from campaigns.hopper import get_redis, hopper_key
    import json, time

    now   = timezone.now()
    total = 0

    for campaign in Campaign.objects.filter(status=Campaign.STATUS_ACTIVE):
        delay_seconds = campaign.retry_delay_minutes * 60

        recyclable = HopperEntry.objects.filter(
            campaign=campaign,
            status__in=[HopperEntry.STATUS_FAILED, HopperEntry.STATUS_DROPPED],
            completed_at__isnull=False,
        ).select_related('lead')

        r   = get_redis()
        key = hopper_key(campaign.id)

        for entry in recyclable:
            age = (now - entry.completed_at).total_seconds()
            if age < delay_seconds:
                continue

            # Check max attempts
            attempt_count = HopperEntry.objects.filter(
                campaign=campaign, lead=entry.lead,
            ).count()
            if attempt_count >= campaign.max_attempts:
                entry.status = HopperEntry.STATUS_EXPIRED
                entry.save(update_fields=['status', 'updated_at'])
                continue

            # Re-queue
            slot = json.dumps({
                'lead_id':    entry.lead_id,
                'phone':      entry.phone_number,
                'first_name': entry.lead.first_name,
                'last_name':  entry.lead.last_name,
                'queued_at':  time.time(),
            })
            r.rpush(key, slot)
            entry.status = HopperEntry.STATUS_QUEUED
            entry.save(update_fields=['status', 'updated_at'])
            total += 1

    if total:
        logger.info(f'Recycled {total} failed calls back to hopper.')
    return {'recycled': total}
