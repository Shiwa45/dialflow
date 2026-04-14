# agents/views.py
import json
import requests
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.conf import settings
from django.core.paginator import Paginator
from .models import AgentStatus, AgentLoginLog, CallDisposition, PauseCode


def _ensure_active_campaign(agent_status):
    from campaigns.models import Campaign

    assigned_campaigns = list(
        Campaign.objects.filter(
            agents__agent=agent_status.user,
            agents__is_active=True,
            status__in=['active', 'paused'],
        ).order_by('id')
    )

    valid_ids = {campaign.id for campaign in assigned_campaigns}
    if agent_status.active_campaign_id in valid_ids:
        return assigned_campaigns

    new_campaign = assigned_campaigns[0] if len(assigned_campaigns) == 1 else None
    if agent_status.active_campaign_id != (new_campaign.id if new_campaign else None):
        agent_status.active_campaign = new_campaign
        agent_status.save(update_fields=['active_campaign', 'updated_at'])
    return assigned_campaigns


def _fetch_live_ari_channel_ids():
    cfg = settings.ASTERISK
    url = f"http://{cfg['ARI_HOST']}:{cfg['ARI_PORT']}/ari/channels"
    try:
        r = requests.get(
            url,
            auth=(cfg['ARI_USERNAME'], cfg['ARI_PASSWORD']),
            timeout=3,
        )
        if r.status_code != 200:
            return None
        return {
            ch.get('id') for ch in (r.json() or [])
            if isinstance(ch, dict) and ch.get('id')
        }
    except Exception:
        return None


def _heal_transient_agent_state(status_obj):
    """
    Heal stale states that can remain after missed ARI channel-destroy events.
    """
    # Wrapup without a call log should never block the dashboard.
    if status_obj.status == 'wrapup' and not (
        status_obj.wrapup_call_log_id or status_obj.active_call_log_id
    ):
        status_obj.go_ready()
        return status_obj

    if status_obj.status not in ('ringing', 'on_call'):
        return status_obj
    if not status_obj.active_channel_id:
        status_obj.go_ready()
        return status_obj

    live_ids = _fetch_live_ari_channel_ids()
    if live_ids is None:
        return status_obj  # ARI temporarily unavailable; avoid false recovery

    if status_obj.active_channel_id in live_ids:
        return status_obj

    # Channel is gone: recover to wrapup if we have a call log, else ready.
    if status_obj.active_call_log_id:
        now = timezone.now()
        status_obj.status = 'wrapup'
        status_obj.status_changed_at = now
        status_obj.wrapup_started_at = now
        status_obj.wrapup_call_log_id = status_obj.active_call_log_id
        status_obj.active_channel_id = None
        status_obj.active_lead_id = None
        status_obj.call_started_at = None
        status_obj.save(update_fields=[
            'status',
            'status_changed_at',
            'wrapup_started_at',
            'wrapup_call_log_id',
            'active_channel_id',
            'active_lead_id',
            'call_started_at',
            'updated_at',
        ])
        return status_obj

    status_obj.go_ready()
    return status_obj


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
    agent_status = _heal_transient_agent_state(agent_status)
    assigned_campaigns = _ensure_active_campaign(agent_status)

    webrtc_config = _build_webrtc_config(request.user)

    # Today's login time
    today_login_time = AgentLoginLog.get_today_login_time_display(request.user)

    # Pause codes for break menu
    pause_codes = list(PauseCode.objects.filter(is_active=True).values('id', 'code', 'name'))

    return render(request, 'agents/dashboard.html', {
        'agent_status':    agent_status,
        'webrtc_config':   webrtc_config,
        'my_campaigns':    list(
            campaign for campaign in assigned_campaigns
        ),
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
        'disable_stun': bool(webrtc.get('DISABLE_STUN', True)),
    }


# ── JSON endpoints ────────────────────────────────────────────────────────────

@login_required
@agent_required
def get_my_status(request):
    status, _ = AgentStatus.objects.get_or_create(user=request.user)
    status = _heal_transient_agent_state(status)
    _ensure_active_campaign(status)
    return JsonResponse({
        'status':            status.status,
        'display':           status.get_status_display(),
        'since':             status.status_changed_at.isoformat(),
        'campaign_id':       status.active_campaign_id,
        'wrapup_remaining':  status.get_wrapup_seconds_remaining(),
        'active_lead_id':    status.active_lead_id,
        'active_channel_id': status.active_channel_id,
        'wrapup_call_log_id': status.wrapup_call_log_id,
        'active_call_log_id': status.active_call_log_id,
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
    if status_obj.status in ('ringing', 'on_call', 'wrapup'):
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
    _ensure_active_campaign(status)
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


# ── Dispose API ───────────────────────────────────────────────────────────────

@login_required
@agent_required
@require_POST
def submit_disposition(request):
    """
    Submit a disposition for a completed call and return agent to ready state.
    Expected POST fields: disposition_id, call_log_id, notes (optional), callback_at (optional).
    """
    from calls.models import CallLog
    from campaigns.models import Disposition

    try:
        disposition_id = int(request.POST['disposition_id'])
        call_log_id    = int(request.POST['call_log_id'])
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'error': 'disposition_id and call_log_id are required integers'}, status=400)

    try:
        call_log = CallLog.objects.select_related('campaign', 'lead').get(
            pk=call_log_id, agent=request.user
        )
    except CallLog.DoesNotExist:
        return JsonResponse({'error': 'Call log not found'}, status=404)

    try:
        disposition = Disposition.objects.get(pk=disposition_id)
    except Disposition.DoesNotExist:
        return JsonResponse({'error': 'Disposition not found'}, status=404)

    notes       = (request.POST.get('notes') or '').strip()
    callback_at = request.POST.get('callback_at') or None

    # Update the CallLog FK
    call_log.disposition = disposition
    update_fields = ['disposition']
    if notes:
        call_log.agent_notes = notes
        update_fields.append('agent_notes')
    call_log.save(update_fields=update_fields)

    # Create CallDisposition record
    CallDisposition.objects.update_or_create(
        call_log=call_log,
        defaults={
            'agent':       request.user,
            'campaign':    call_log.campaign,
            'lead':        call_log.lead,
            'disposition': disposition,
            'notes':       notes,
            'callback_at': callback_at,
        },
    )

    # Handle DNC outcome
    if disposition.outcome == 'dnc' and call_log.lead:
        call_log.lead.mark_dnc(reason=f'Agent disposition: {disposition.name}')

    # Move agent from wrapup → ready
    status_obj, _ = AgentStatus.objects.get_or_create(user=request.user)
    if status_obj.status == 'wrapup':
        status_obj.go_ready()

    # Push dispose_ok event via WebSocket so other tabs know
    try:
        from core.ws_utils import send_to_agent
        send_to_agent(request.user.id, {'type': 'dispose_ok', 'call_log_id': call_log_id})
    except Exception:
        pass

    return JsonResponse({
        'success':         True,
        'disposition':     disposition.name,
        'outcome':         disposition.outcome,
        'agent_status':    status_obj.status,
    })


# ── Pause codes API ──────────────────────────────────────────────────────────

@login_required
@require_GET
def get_pause_codes(request):
    codes = list(PauseCode.objects.filter(is_active=True).values('id', 'code', 'name'))
    return JsonResponse({'pause_codes': codes})
