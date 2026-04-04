# reports/tasks.py
import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('dialflow')


@shared_task(name='reports.tasks.generate_daily_snapshot')
def generate_daily_snapshot():
    """Generate daily rollup snapshot for all campaigns. Runs at midnight."""
    from campaigns.models import Campaign
    from calls.models import CallLog
    from reports.models import DailySnapshot
    from django.db.models import Count, Sum, Avg, Q

    yesterday = (timezone.now() - timezone.timedelta(days=1)).date()
    created   = 0

    for campaign in Campaign.objects.all():
        qs = CallLog.objects.filter(campaign=campaign, started_at__date=yesterday)
        agg = qs.aggregate(
            total      = Count('id'),
            answered   = Count('id', filter=Q(status='completed')),
            dropped    = Count('id', filter=Q(status='dropped')),
            no_answer  = Count('id', filter=Q(status='no_answer')),
            avg_talk   = Avg('duration', filter=Q(status='completed')),
            total_talk = Sum('duration', filter=Q(status='completed')),
        )
        total    = agg['total']    or 0
        dropped  = agg['dropped']  or 0
        abandon  = round((dropped / total * 100), 2) if total > 0 else 0

        DailySnapshot.objects.update_or_create(
            date=yesterday, campaign=campaign,
            defaults={
                'calls_total':     total,
                'calls_answered':  agg['answered']  or 0,
                'calls_dropped':   dropped,
                'calls_no_answer': agg['no_answer'] or 0,
                'avg_talk_time':   int(agg['avg_talk']   or 0),
                'total_talk_time': int(agg['total_talk'] or 0),
                'abandon_rate':    abandon,
            }
        )
        created += 1

    logger.info(f'Daily snapshot generated for {created} campaigns on {yesterday}')
    return {'snapshots': created, 'date': str(yesterday)}
