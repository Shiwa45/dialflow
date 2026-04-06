# reports/views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Count, Sum, Avg, Q


def supervisor_required(fn):
    from functools import wraps
    @wraps(fn)
    def _w(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_supervisor:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden()
        return fn(request, *args, **kwargs)
    return _w


@login_required
@supervisor_required
def report_home(request):
    return render(request, 'reports/home.html')


@login_required
@supervisor_required
def intraday_report(request):
    from calls.models import CallLog
    from campaigns.models import Campaign

    today     = timezone.now().date()
    campaigns = Campaign.objects.filter(status__in=['active', 'paused'])

    rows = []
    for campaign in campaigns:
        agg = CallLog.objects.filter(campaign=campaign, started_at__date=today).aggregate(
            total    = Count('id'),
            answered = Count('id', filter=Q(status='completed')),
            dropped  = Count('id', filter=Q(status='dropped')),
            avg_talk = Avg('duration', filter=Q(status='completed')),
        )
        rows.append({'campaign': campaign, **agg})

    return render(request, 'reports/intraday.html', {'rows': rows, 'today': today})


@login_required
@supervisor_required
def agent_report(request):
    from django.contrib.auth import get_user_model
    from calls.models import CallLog

    User  = get_user_model()
    today = timezone.now().date()
    agents = User.objects.filter(role='agent', is_active=True)

    rows = []
    for agent in agents:
        agg = CallLog.objects.filter(agent=agent, started_at__date=today).aggregate(
            total    = Count('id'),
            answered = Count('id', filter=Q(status='completed')),
            talk_sec = Sum('duration', filter=Q(status='completed')),
        )
        rows.append({'agent': agent, **agg})

    return render(request, 'reports/agents.html', {'rows': rows, 'today': today})


@login_required
@supervisor_required
def campaign_report(request):
    from reports.models import DailySnapshot
    from campaigns.models import Campaign

    days      = int(request.GET.get('days', 7))
    campaign_id = request.GET.get('campaign_id')
    since     = timezone.now().date() - timezone.timedelta(days=days)

    qs = DailySnapshot.objects.filter(date__gte=since).select_related('campaign')
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)

    campaigns = Campaign.objects.all()
    return render(request, 'reports/campaigns.html', {
        'snapshots': qs.order_by('-date'),
        'campaigns': campaigns,
        'days':      days,
    })


@login_required
@supervisor_required
def live_stats_api(request):
    """Real-time stats for supervisor dashboard — fallback if WS not available."""
    from campaigns.models import Campaign
    from agents.models import AgentStatus

    campaigns = list(Campaign.objects.filter(
        status__in=['active', 'paused']
    ).values('id', 'name', 'status', 'stat_calls_today',
             'stat_answered_today', 'stat_abandon_rate', 'stat_agents_active'))

    agents = list(AgentStatus.objects.select_related('user').filter(
        status__in=['ready', 'on_call', 'wrapup', 'break']
    ).values('user_id', 'user__username', 'status', 'active_campaign_id',
             'call_started_at', 'status_changed_at'))

    return JsonResponse({
        'campaigns': campaigns,
        'agents':    agents,
        'ts':        timezone.now().isoformat(),
    })


# ── Real-time Lead Monitor ────────────────────────────────────────────────────

@login_required
@supervisor_required
def lead_monitor(request):
    """Real-time lead monitoring dashboard — shows what leads are being dialed."""
    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(
        status__in=['active','paused']
    ).order_by('name')
    return render(request, 'reports/lead_monitor.html', {
        'campaigns': campaigns,
    })


@login_required
@supervisor_required
def hopper_stats_api(request):
    """
    JSON API: real-time hopper counts for all active campaigns.
    Also returns AMD statistics for the monitoring dashboard.
    """
    from campaigns.models import Campaign
    from campaigns.hopper import get_hopper_stats
    from calls.models import CallLog
    from django.utils import timezone

    campaigns = Campaign.objects.filter(status__in=['active','paused'])
    result    = []

    for c in campaigns:
        stats = get_hopper_stats(c.id)
        result.append({
            'id':       c.id,
            'name':     c.name,
            'status':   c.status,
            'queued':   stats['queued'],
            'in_flight':stats['in_flight'],
        })

    # AMD stats (today)
    today = timezone.now().date()
    amd_qs = CallLog.objects.filter(started_at__date=today).exclude(amd_result='')
    amd_human   = amd_qs.filter(amd_result__icontains='human').count()
    amd_machine = amd_qs.filter(amd_result__icontains='machine').count()
    amd_total   = amd_human + amd_machine
    amd_rate    = (amd_machine / amd_total * 100) if amd_total > 0 else 0

    return JsonResponse({
        'campaigns': result,
        'amd': {
            'human':   amd_human,
            'machine': amd_machine,
            'rate':    round(amd_rate, 1),
        }
    })
