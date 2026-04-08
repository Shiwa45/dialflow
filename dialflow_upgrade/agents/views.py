# agents/views.py
import json
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.conf import settings
from django.core.paginator import Paginator
from .models import AgentStatus, AgentLoginLog, CallDisposition, PauseCode


def agent_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_agent:
            from django.shortcuts import redirect
            return redirect('users:login')
        return view_func(request, *args, **kwargs)
    return _wrapped


@login_required
@agent_required
def dashboard(request):
    """Main agent dashboard — serves the WebRTC + WS-powered UI."""
    agent_status, _ = AgentStatus.objects.select_related(
        'active_campaign', 'pause_code'
    ).get_or_create(user=request.user)

    webrtc_config = _build_webrtc_config(request.user)

    from campaigns.models import Campaign
    my_campaigns = Campaign.objects.filter(
        agents__agent=request.user,
        agents__is_active=True,
        status__in=['active', 'paused'],
    ).values('id', 'name', 'status')

    # Today's login time
    today_login_time = AgentLoginLog.get_today_login_time_display(request.user)

    # Pause codes for break menu
    pause_codes = list(PauseCode.objects.filter(is_active=True).values('id', 'code', 'name'))

    return render(request, 'agents/dashboard.html', {
        'agent_status':    agent_status,
        'webrtc_config':   webrtc_config,
        'my_campaigns':    list(my_campaigns),
        'ws_url':          '/ws/agent/',
        'today_login_time': today_login_time,
        'pause_codes':     pause_codes,
    })


def _build_webrtc_config(user):
    webrtc = settings.WEBRTC
    phone = getattr(user, 'phone_extension', None)
    extension = ''
    password = ''
    domain = webrtc.get('DOMAIN', '127.0.0.1')

    try:
        from telephony.models import Phone
        p = Phone.objects.select_related('asterisk_server').get(user=user, is_active=True)
        extension = p.extension
        password = p.secret
        domain = p.asterisk_server.server_ip
    except Exception:
        pass

    return {
        'ws_url':       webrtc.get('WS_URL', 'ws://127.0.0.1:8088/ws'),
        'domain':       domain,
        'extension':    extension,
        'password':     password,
        'sip_uri':      f'sip:{extension}@{domain}' if extension else '',
        'display_name': user.get_full_name() or user.username,
        'stun':         webrtc.get('STUN_SERVER', 'stun:stun.l.google.com:19302'),
    }


# ── JSON endpoints ────────────────────────────────────────────────────────────

@login_required
@agent_required
def get_my_status(request):
    status, _ = AgentStatus.objects.get_or_create(user=request.user)
    return JsonResponse({
        'status':            status.status,
        'display':           status.get_status_display(),
        'since':             status.status_changed_at.isoformat(),
        'campaign_id':       status.active_campaign_id,
        'wrapup_remaining':  status.get_wrapup_seconds_remaining(),
        'today_login_time':  AgentLoginLog.get_today_login_time_display(request.user),
        'pause_code':        status.pause_code.name if status.pause_code else None,
    })


@login_required
@agent_required
@require_POST
def set_status(request):
    new_status = request.POST.get('status', '')
    allowed = ('ready', 'break', 'training')
    if new_status not in allowed:
        return JsonResponse({'error': 'Invalid status'}, status=400)

    status_obj, _ = AgentStatus.objects.get_or_create(user=request.user)
    if status_obj.status in ('on_call', 'wrapup'):
        return JsonResponse({'error': 'Cannot change status while on call or in wrapup'}, status=400)

    if new_status == 'break':
        pause_code_id = request.POST.get('pause_code_id')
        pause_code = None
        if pause_code_id:
            try:
                pause_code = PauseCode.objects.get(id=pause_code_id)
            except PauseCode.DoesNotExist:
                pass
        status_obj.go_break(pause_code=pause_code)
    elif new_status == 'ready':
        status_obj.go_ready()
    elif new_status == 'training':
        status_obj.go_training()

    return JsonResponse({
        'success': True,
        'status': status_obj.status,
        'today_login_time': AgentLoginLog.get_today_login_time_display(request.user),
    })


@login_required
@agent_required
@require_POST
def submit_heartbeat(request):
    AgentStatus.objects.filter(user=request.user).update(last_heartbeat=timezone.now())
    return JsonResponse({'ok': True})


@login_required
@agent_required
@require_GET
def get_lead_info(request):
    lead_id = request.GET.get('lead_id')
    if not lead_id:
        status, _ = AgentStatus.objects.get_or_create(user=request.user)
        lead_id = status.active_lead_id

    if not lead_id:
        return JsonResponse({'error': 'No active lead'}, status=404)

    from leads.models import Lead, LeadNote
    try:
        lead = Lead.objects.get(id=lead_id)
        notes = list(
            LeadNote.objects.filter(lead=lead)
            .order_by('-created_at')[:5]
            .values('note', 'created_at', 'agent__username')
        )
        return JsonResponse({
            'id':            lead.id,
            'first_name':    lead.first_name,
            'last_name':     lead.last_name,
            'full_name':     lead.full_name,
            'phone':         lead.primary_phone,
            'email':         lead.email,
            'company':       lead.company,
            'city':          lead.city,
            'state':         lead.state,
            'custom_fields': lead.custom_fields,
            'recent_notes':  notes,
        })
    except Lead.DoesNotExist:
        return JsonResponse({'error': 'Lead not found'}, status=404)


@login_required
@agent_required
@require_GET
def get_dispositions(request):
    from campaigns.models import Disposition
    status, _ = AgentStatus.objects.get_or_create(user=request.user)
    campaign_id = request.GET.get('campaign_id') or status.active_campaign_id
    if not campaign_id:
        return JsonResponse({'dispositions': []})
    disps = list(
        Disposition.objects.filter(
            campaigns__id=campaign_id, is_active=True
        ).order_by('sort_order', 'name').values('id', 'name', 'category', 'outcome', 'color', 'hotkey')
    )
    return JsonResponse({'dispositions': disps})


@login_required
@agent_required
@require_GET
def call_history(request):
    """Agent's recent call history — last 50 calls."""
    from calls.models import CallLog
    calls = CallLog.objects.filter(
        agent=request.user,
    ).select_related('lead', 'campaign', 'disposition').order_by('-started_at')[:50]

    data = []
    for c in calls:
        data.append({
            'id':          c.id,
            'lead_name':   c.lead.full_name if c.lead else c.phone_number,
            'phone':       c.phone_number,
            'campaign':    c.campaign.name if c.campaign else '',
            'status':      c.status,
            'duration':    c.duration,
            'started_at':  c.started_at.isoformat() if c.started_at else None,
            'disposition': c.disposition.name if c.disposition else None,
            'recording':   bool(c.recording_path),
            'recording_url': c.recording_url,
        })
    return JsonResponse({'calls': data})


# ── Supervisor views ──────────────────────────────────────────────────────────

@login_required
def supervisor_dashboard(request):
    if not request.user.is_supervisor:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()

    agents = AgentStatus.objects.select_related(
        'user', 'active_campaign', 'pause_code'
    ).all().order_by('status', 'user__username')

    return render(request, 'agents/supervisor.html', {
        'agents': agents,
        'ws_url': '/ws/supervisor/',
    })


@login_required
@require_POST
def force_logout_agent(request, agent_id):
    if not request.user.is_supervisor:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    from django.contrib.auth import get_user_model
    User = get_user_model()
    target = get_object_or_404(User, pk=agent_id)
    status, _ = AgentStatus.objects.get_or_create(user=target)
    status.go_offline()

    from core.ws_utils import send_to_agent
    send_to_agent(target.id, {'type': 'force_logout', 'reason': 'Supervisor action'})
    return JsonResponse({'success': True})


# ── Agent call history page ──────────────────────────────────────────────────

@login_required
@agent_required
def call_history_page(request):
    """HTML page showing agent's own call history with filters."""
    from calls.models import CallLog

    qs = CallLog.objects.filter(agent=request.user).select_related(
        'lead', 'campaign', 'disposition'
    )

    # Filters
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    status_filter = request.GET.get('status')
    search = request.GET.get('q', '').strip()

    if date_from:
        qs = qs.filter(started_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(started_at__date__lte=date_to)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(phone_number__icontains=search) |
            Q(lead__first_name__icontains=search) |
            Q(lead__last_name__icontains=search)
        )

    paginator = Paginator(qs.order_by('-started_at'), 50)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'agents/call_history.html', {
        'page': page,
        'total': qs.count(),
        'filters': {
            'date_from': date_from, 'date_to': date_to,
            'status': status_filter, 'q': search,
        },
    })


# ── Supervisor monitor API ────────────────────────────────────────────────────

@login_required
def supervisor_monitor(request):
    if not request.user.is_supervisor:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    agents = AgentStatus.objects.select_related(
        'user', 'active_campaign', 'pause_code'
    ).exclude(status='offline')

    data = []
    for a in agents:
        data.append({
            'id':         a.user_id,
            'username':   a.user.username,
            'name':       a.user.get_full_name() or a.user.username,
            'status':     a.status,
            'campaign':   a.active_campaign.name if a.active_campaign else None,
            'since':      a.status_changed_at.isoformat(),
            'calls':      a.calls_today,
            'talk_time':  a.talk_time_today,
            'pause_code': a.pause_code.name if a.pause_code else None,
            'login_time': AgentLoginLog.get_today_login_time_display(a.user),
        })
    return JsonResponse({'agents': data})


# ── Pause codes API ──────────────────────────────────────────────────────────

@login_required
@require_GET
def get_pause_codes(request):
    codes = list(PauseCode.objects.filter(is_active=True).values('id', 'code', 'name'))
    return JsonResponse({'pause_codes': codes})
