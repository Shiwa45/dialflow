# reports/views.py
import csv
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Count, Sum, Avg, Q
from django.core.paginator import Paginator


def supervisor_required(fn):
    from functools import wraps
    @wraps(fn)
    def _w(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_supervisor:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden()
        return fn(request, *args, **kwargs)
    return _w


def _fmt_dur(seconds):
    if not seconds: return '0h 0m'
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    return f'{h}h {m}m'


@login_required
@supervisor_required
def report_home(request):
    return render(request, 'reports/home.html')


@login_required
@supervisor_required
def intraday_report(request):
    from calls.models import CallLog
    from campaigns.models import Campaign
    today = timezone.now().date()
    campaigns = Campaign.objects.filter(status__in=['active', 'paused'])
    rows = []
    for campaign in campaigns:
        agg = CallLog.objects.filter(campaign=campaign, started_at__date=today).aggregate(
            total=Count('id'),
            answered=Count('id', filter=Q(status='completed')),
            dropped=Count('id', filter=Q(status='dropped')),
            no_answer=Count('id', filter=Q(status='no_answer')),
            avg_talk=Avg('duration', filter=Q(status='completed')),
            total_talk=Sum('duration', filter=Q(status='completed')),
        )
        rows.append({'campaign': campaign, **agg})
    return render(request, 'reports/intraday.html', {'rows': rows, 'today': today})


@login_required
@supervisor_required
def agent_report(request):
    from django.contrib.auth import get_user_model
    from calls.models import CallLog
    from agents.models import AgentLoginLog
    User = get_user_model()
    date_from = request.GET.get('date_from', str(timezone.now().date()))
    date_to = request.GET.get('date_to', str(timezone.now().date()))
    campaign_id = request.GET.get('campaign_id')
    agents = User.objects.filter(role='agent', is_active=True)
    rows = []
    for agent in agents:
        qs = CallLog.objects.filter(agent=agent, started_at__date__gte=date_from, started_at__date__lte=date_to)
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        agg = qs.aggregate(
            total=Count('id'),
            answered=Count('id', filter=Q(status='completed')),
            dropped=Count('id', filter=Q(status='dropped')),
            no_answer=Count('id', filter=Q(status='no_answer')),
            talk_sec=Sum('duration', filter=Q(status='completed')),
            avg_talk=Avg('duration', filter=Q(status='completed')),
        )
        login_time = AgentLoginLog.get_today_login_time(agent)
        rows.append({
            'agent': agent, 'login_time': login_time,
            'login_time_display': _fmt_dur(login_time), **agg,
        })
    from campaigns.models import Campaign
    campaigns = Campaign.objects.all().values('id', 'name')
    if request.GET.get('download') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="agent_report_{date_from}.csv"'
        w = csv.writer(resp)
        w.writerow(['Agent', 'Login Time', 'Total', 'Answered', 'Dropped', 'No Answer', 'Talk (s)', 'Avg Talk (s)'])
        for r in rows:
            w.writerow([r['agent'].get_full_name() or r['agent'].username, r['login_time_display'],
                        r.get('total',0), r.get('answered',0), r.get('dropped',0), r.get('no_answer',0),
                        r.get('talk_sec') or 0, round(r.get('avg_talk') or 0, 1)])
        return resp
    return render(request, 'reports/agents.html', {
        'rows': rows, 'date_from': date_from, 'date_to': date_to,
        'campaigns': campaigns, 'campaign_id': campaign_id,
    })


@login_required
@supervisor_required
def campaign_report(request):
    from reports.models import DailySnapshot
    from campaigns.models import Campaign
    days = int(request.GET.get('days', 7))
    campaign_id = request.GET.get('campaign_id')
    since = timezone.now().date() - timezone.timedelta(days=days)
    qs = DailySnapshot.objects.filter(date__gte=since).select_related('campaign')
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)
    campaigns = Campaign.objects.all()
    if request.GET.get('download') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="campaign_report.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'Campaign', 'Total', 'Answered', 'Dropped', 'No Answer', 'Avg Talk', 'Total Talk', 'Abandon Rate'])
        for s in qs.order_by('-date'):
            w.writerow([s.date, s.campaign.name, s.calls_total, s.calls_answered,
                        s.calls_dropped, s.calls_no_answer, s.avg_talk_time, s.total_talk_time, s.abandon_rate])
        return resp
    return render(request, 'reports/campaigns.html', {
        'snapshots': qs.order_by('-date'), 'campaigns': campaigns, 'days': days,
    })


@login_required
@supervisor_required
def disposition_report(request):
    from agents.models import CallDisposition
    from campaigns.models import Campaign
    date_from = request.GET.get('date_from', str(timezone.now().date()))
    date_to = request.GET.get('date_to', str(timezone.now().date()))
    campaign_id = request.GET.get('campaign_id')
    qs = CallDisposition.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)
    summary = qs.values('disposition__name', 'disposition__category', 'disposition__color').annotate(count=Count('id')).order_by('-count')
    by_agent = qs.values('agent__username', 'agent__first_name', 'agent__last_name').annotate(
        total=Count('id'), sales=Count('id', filter=Q(disposition__category='sale')),
        callbacks=Count('id', filter=Q(disposition__outcome='callback')),
        dnc=Count('id', filter=Q(disposition__category='dnc')),
    ).order_by('-total')
    campaigns = Campaign.objects.all().values('id', 'name')
    if request.GET.get('download') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="disposition_report_{date_from}.csv"'
        w = csv.writer(resp)
        w.writerow(['Disposition', 'Category', 'Count'])
        for r in summary:
            w.writerow([r['disposition__name'], r['disposition__category'], r['count']])
        w.writerow([])
        w.writerow(['Agent', 'Total', 'Sales', 'Callbacks', 'DNC'])
        for r in by_agent:
            w.writerow([f"{r.get('agent__first_name','')} {r.get('agent__last_name','')}".strip() or r['agent__username'],
                        r['total'], r['sales'], r['callbacks'], r['dnc']])
        return resp
    return render(request, 'reports/dispositions.html', {
        'summary': summary, 'by_agent': by_agent, 'date_from': date_from,
        'date_to': date_to, 'campaigns': campaigns, 'campaign_id': campaign_id,
        'total_dispositions': qs.count(),
    })


@login_required
@supervisor_required
def hourly_report(request):
    from calls.models import CallLog
    from campaigns.models import Campaign
    date_str = request.GET.get('date', str(timezone.now().date()))
    campaign_id = request.GET.get('campaign_id')
    qs = CallLog.objects.filter(started_at__date=date_str)
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)
    hours = []
    for h in range(24):
        agg = qs.filter(started_at__hour=h).aggregate(
            total=Count('id'), answered=Count('id', filter=Q(status='completed')),
            dropped=Count('id', filter=Q(status='dropped')),
            avg_talk=Avg('duration', filter=Q(status='completed')),
        )
        hours.append({'hour': h, 'label': f'{h:02d}:00', **agg})
    campaigns = Campaign.objects.all().values('id', 'name')
    return render(request, 'reports/hourly.html', {
        'hours': hours, 'date': date_str, 'campaigns': campaigns, 'campaign_id': campaign_id,
    })


@login_required
@supervisor_required
def cdr_report(request):
    from calls.models import CallLog
    from campaigns.models import Campaign
    qs = CallLog.objects.select_related('lead', 'campaign', 'agent', 'disposition')
    date_from = request.GET.get('date_from', str(timezone.now().date()))
    date_to = request.GET.get('date_to', str(timezone.now().date()))
    campaign_id = request.GET.get('campaign_id')
    status = request.GET.get('status')
    agent_id = request.GET.get('agent_id')
    search = request.GET.get('q', '').strip()
    qs = qs.filter(started_at__date__gte=date_from, started_at__date__lte=date_to)
    if campaign_id: qs = qs.filter(campaign_id=campaign_id)
    if status: qs = qs.filter(status=status)
    if agent_id: qs = qs.filter(agent_id=agent_id)
    if search: qs = qs.filter(Q(phone_number__icontains=search)|Q(lead__first_name__icontains=search)|Q(lead__last_name__icontains=search))
    if request.GET.get('download') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="cdr_report.csv"'
        w = csv.writer(resp)
        w.writerow(['Date/Time','Phone','Lead','Campaign','Agent','Status','Duration','Disposition','AMD','Recording'])
        for c in qs.order_by('-started_at')[:50000]:
            w.writerow([c.started_at.strftime('%Y-%m-%d %H:%M:%S') if c.started_at else '',
                        c.phone_number, c.lead.full_name if c.lead else '', c.campaign.name if c.campaign else '',
                        c.agent.username if c.agent else '', c.status, c.duration,
                        c.disposition.name if c.disposition else '', c.amd_result, 'Yes' if c.recording_path else ''])
        return resp
    paginator = Paginator(qs.order_by('-started_at'), 100)
    page = paginator.get_page(request.GET.get('page'))
    campaigns = Campaign.objects.all().values('id', 'name')
    from django.contrib.auth import get_user_model
    agents_list = get_user_model().objects.filter(role='agent', is_active=True).values('id', 'username', 'first_name', 'last_name')
    return render(request, 'reports/cdr.html', {
        'page': page, 'total': qs.count(), 'date_from': date_from, 'date_to': date_to,
        'campaigns': campaigns, 'agents_list': agents_list,
        'filters': {'campaign_id': campaign_id, 'status': status, 'agent_id': agent_id, 'q': search},
    })


@login_required
@supervisor_required
def dnc_report(request):
    from campaigns.models import DNCEntry
    qs = DNCEntry.objects.select_related('campaign', 'added_by').order_by('-created_at')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if date_from: qs = qs.filter(created_at__date__gte=date_from)
    if date_to: qs = qs.filter(created_at__date__lte=date_to)
    if request.GET.get('download') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="dnc_report.csv"'
        w = csv.writer(resp)
        w.writerow(['Phone', 'Campaign', 'Reason', 'Added By', 'Date'])
        for d in qs[:10000]:
            w.writerow([d.phone_number, d.campaign.name if d.campaign else 'System', d.reason,
                        d.added_by.username if d.added_by else '', d.created_at.strftime('%Y-%m-%d %H:%M')])
        return resp
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'reports/dnc.html', {'page': page, 'total': qs.count()})


@login_required
@supervisor_required
def live_stats_api(request):
    from campaigns.models import Campaign
    from agents.models import AgentStatus
    campaigns = list(Campaign.objects.filter(status__in=['active','paused']).values(
        'id','name','status','stat_calls_today','stat_answered_today','stat_abandon_rate','stat_agents_active'))
    agents = list(AgentStatus.objects.select_related('user','pause_code').filter(
        status__in=['ready','on_call','wrapup','break']).values(
        'user_id','user__username','status','active_campaign_id','call_started_at','status_changed_at','pause_code__name'))
    return JsonResponse({'campaigns': campaigns, 'agents': agents, 'ts': timezone.now().isoformat()})


@login_required
@supervisor_required
def lead_monitor(request):
    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(status__in=['active','paused']).order_by('name')
    return render(request, 'reports/lead_monitor.html', {'campaigns': campaigns})


@login_required
@supervisor_required
def hopper_stats_api(request):
    from campaigns.models import Campaign
    from campaigns.hopper import get_hopper_stats
    campaigns = Campaign.objects.filter(status__in=['active','paused'])
    result = [{'id': c.id, 'name': c.name, 'status': c.status, **get_hopper_stats(c.id)} for c in campaigns]
    return JsonResponse({'campaigns': result})
