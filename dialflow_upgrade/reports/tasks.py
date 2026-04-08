# reports/tasks.py
import logging
from celery import shared_task
from django.utils import timezone
from django.db.models import Count, Sum, Q

logger = logging.getLogger('dialflow')


@shared_task
def generate_daily_snapshots():
    from campaigns.models import Campaign
    from calls.models import CallLog
    from reports.models import DailySnapshot
    yesterday = timezone.now().date() - timezone.timedelta(days=1)
    for campaign in Campaign.objects.all():
        agg = CallLog.objects.filter(campaign=campaign, started_at__date=yesterday).aggregate(
            total=Count('id'), answered=Count('id', filter=Q(status='completed')),
            dropped=Count('id', filter=Q(status='dropped')), no_answer=Count('id', filter=Q(status='no_answer')),
            total_talk=Sum('duration', filter=Q(status='completed')),
        )
        total = agg['total'] or 0; answered = agg['answered'] or 0; dropped = agg['dropped'] or 0
        DailySnapshot.objects.update_or_create(date=yesterday, campaign=campaign, defaults={
            'calls_total': total, 'calls_answered': answered, 'calls_dropped': dropped,
            'calls_no_answer': agg['no_answer'] or 0,
            'avg_talk_time': int((agg['total_talk'] or 0) / max(answered, 1)),
            'total_talk_time': agg['total_talk'] or 0,
            'abandon_rate': round(dropped / max(total, 1) * 100, 2),
        })
    logger.info(f'Generated daily snapshots for {yesterday}')


@shared_task
def generate_agent_daily_logs():
    from django.contrib.auth import get_user_model
    from calls.models import CallLog
    from agents.models import AgentLoginLog, CallDisposition
    from reports.models import AgentDailyLog
    User = get_user_model()
    yesterday = timezone.now().date() - timezone.timedelta(days=1)
    for agent in User.objects.filter(role='agent', is_active=True):
        calls = CallLog.objects.filter(agent=agent, started_at__date=yesterday)
        disps = CallDisposition.objects.filter(agent=agent, created_at__date=yesterday)
        agg = calls.aggregate(dialed=Count('id'), answered=Count('id', filter=Q(status='completed')),
                              talk_time=Sum('duration', filter=Q(status='completed')))
        sessions = AgentLoginLog.objects.filter(user=agent, login_at__date__lte=yesterday).filter(
            Q(logout_at__isnull=True) | Q(logout_at__date__gte=yesterday))
        login_time = sum(s.duration_for_date(yesterday) for s in sessions)
        disp_agg = disps.aggregate(
            sales=Count('id', filter=Q(disposition__category='sale')),
            dnc=Count('id', filter=Q(disposition__category='dnc')),
            callbacks=Count('id', filter=Q(disposition__outcome='callback')),
            other=Count('id', filter=~Q(disposition__category__in=['sale', 'dnc'])),
        )
        AgentDailyLog.objects.update_or_create(date=yesterday, agent=agent, campaign=None, defaults={
            'login_time': login_time, 'talk_time': agg['talk_time'] or 0,
            'calls_dialed': agg['dialed'] or 0, 'calls_answered': agg['answered'] or 0,
            'dispositions_sale': disp_agg['sales'] or 0, 'dispositions_dnc': disp_agg['dnc'] or 0,
            'dispositions_callback': disp_agg['callbacks'] or 0, 'dispositions_other': disp_agg['other'] or 0,
        })
    logger.info(f'Generated agent daily logs for {yesterday}')


@shared_task
def close_stale_login_sessions():
    from agents.models import AgentLoginLog, AgentStatus
    offline_agents = AgentStatus.objects.filter(status='offline').values_list('user_id', flat=True)
    closed = AgentLoginLog.objects.filter(user_id__in=offline_agents, logout_at__isnull=True).update(logout_at=timezone.now())
    if closed: logger.info(f'Closed {closed} stale login sessions')
