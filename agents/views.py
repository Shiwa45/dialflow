# agents/views.py
import json
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.conf import settings
from .models import AgentStatus, CallDisposition


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
    """
    Main agent dashboard — serves the WebRTC + WS-powered UI.
    All dynamic state is delivered via WebSocket after page load.
    """
    agent_status, _ = AgentStatus.objects.select_related(
        'active_campaign'
    ).get_or_create(user=request.user)

    # WebRTC config — pulled from settings + agent's phone extension
    webrtc_config = _build_webrtc_config(request.user)

    # Campaigns available to this agent
    from campaigns.models import Campaign
    my_campaigns = Campaign.objects.filter(
        agents__agent=request.user,
        agents__is_active=True,
        status__in=['active', 'paused'],
    ).values('id', 'name', 'status')

    return render(request, 'agents/dashboard.html', {
        'agent_status':  agent_status,
        'webrtc_config': webrtc_config,
        'my_campaigns':  list(my_campaigns),
        'ws_url':        '/ws/agent/',
    })


def _build_webrtc_config(user):
    """Build JsSIP config dict for the agent's extension."""
    webrtc = settings.WEBRTC
    phone  = getattr(user, 'phone_extension', None)

    extension = ''
    password  = ''
    domain    = webrtc.get('DOMAIN', '127.0.0.1')

    try:
        from telephony.models import Phone
        p         = Phone.objects.select_related('asterisk_server').get(user=user, is_active=True)
        extension = p.extension
        password  = p.secret
        domain    = p.asterisk_server.server_ip
    except Exception:
        pass

    return {
        'ws_url':      webrtc.get('WS_URL', 'ws://127.0.0.1:8088/ws'),
        'domain':      domain,
        'extension':   extension,
        'password':    password,
        'sip_uri':     f'sip:{extension}@{domain}' if extension else '',
        'display_name': user.get_full_name() or user.username,
        'stun':        webrtc.get('STUN_SERVER', 'stun:stun.l.google.com:19302'),
    }


# ── REST-style JSON endpoints (called by WS fallback or HTMX) ────────────────

@login_required
@agent_required
def get_my_status(request):
    """Return current DB status for this agent."""
    status, _ = AgentStatus.objects.get_or_create(user=request.user)
    return JsonResponse({
        'status':       status.status,
        'display':      status.get_status_display(),
        'since':        status.status_changed_at.isoformat(),
        'campaign_id':  status.active_campaign_id,
        'wrapup_remaining': status.get_wrapup_seconds_remaining(),
    })


@login_required
@agent_required
@require_POST
def set_status(request):
    """Fallback HTTP endpoint for status change (WS preferred)."""
    new_status = request.POST.get('status', '')
    allowed    = ('ready', 'break', 'training')
    if new_status not in allowed:
        return JsonResponse({'error': 'Invalid status'}, status=400)

    status_obj, _ = AgentStatus.objects.get_or_create(user=request.user)
    if status_obj.status in ('on_call', 'wrapup'):
        return JsonResponse({'error': 'Cannot change status while on call or in wrapup'}, status=400)

    getattr(status_obj, f'go_{new_status}', lambda: status_obj.set_status(new_status))()
    return JsonResponse({'success': True, 'status': status_obj.status})


@login_required
@agent_required
@require_POST
def submit_heartbeat(request):
    """HTTP heartbeat fallback (WS heartbeat preferred)."""
    AgentStatus.objects.filter(user=request.user).update(last_heartbeat=timezone.now())
    return JsonResponse({'ok': True})


@login_required
@agent_required
@require_GET
def get_lead_info(request):
    """Return lead details for the agent's current call."""
    lead_id = request.GET.get('lead_id')
    if not lead_id:
        status, _ = AgentStatus.objects.get_or_create(user=request.user)
        lead_id = status.active_lead_id

    if not lead_id:
        return JsonResponse({'error': 'No active lead'}, status=404)

    from leads.models import Lead, LeadNote
    try:
        lead  = Lead.objects.get(id=lead_id)
        notes = list(
            LeadNote.objects.filter(lead=lead)
            .order_by('-created_at')[:5]
            .values('note', 'created_at', 'agent__username')
        )
        return JsonResponse({
            'id':           lead.id,
            'first_name':   lead.first_name,
            'last_name':    lead.last_name,
            'full_name':    lead.full_name,
            'phone':        lead.primary_phone,
            'email':        lead.email,
            'company':      lead.company,
            'city':         lead.city,
            'state':        lead.state,
            'custom_fields': lead.custom_fields,
            'recent_notes': notes,
        })
    except Lead.DoesNotExist:
        return JsonResponse({'error': 'Lead not found'}, status=404)


@login_required
@agent_required
@require_GET
def get_dispositions(request):
    """Return dispositions for agent's current campaign."""
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
    """Agent's recent call history — last 20 calls."""
    from calls.models import CallLog
    calls = CallLog.objects.filter(
        agent=request.user,
    ).select_related('lead', 'campaign', 'disposition').order_by('-started_at')[:20]

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
            'recording':   c.recording_path if c.recording_path else None,
        })
    return JsonResponse({'calls': data})


# ── Supervisor-only agent management ─────────────────────────────────────────

@login_required
def supervisor_dashboard(request):
    if not request.user.is_supervisor:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()

    from agents.models import AgentStatus
    agents = AgentStatus.objects.select_related(
        'user', 'active_campaign'
    ).all().order_by('status', 'user__username')

    return render(request, 'agents/supervisor.html', {
        'agents':  agents,
        'ws_url':  '/ws/supervisor/',
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


# ── Agent call history HTML page ──────────────────────────────────────────────

@login_required
@agent_required
def call_history_page(request):
    """HTML page showing agent's own call history."""
    from calls.models import CallLog
    from django.core.paginator import Paginator

    qs = CallLog.objects.filter(
        agent=request.user,
    ).select_related('lead', 'campaign', 'disposition').order_by('-started_at')

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'agents/call_history.html', {
        'page':  page,
        'total': qs.count(),
    })


# ── Supervisor: silent monitor / barge / whisper ──────────────────────────────

@login_required
@require_POST
def supervisor_monitor(request):
    """
    Supervisor call monitoring via Asterisk AMI.
    Modes:
      listen  — silent monitor (agent cannot hear supervisor)
      whisper — supervisor can speak to agent only
      barge   — full conference (customer hears supervisor too)
    """
    if not request.user.is_supervisor:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    agent_id   = request.POST.get('agent_id')
    mode       = request.POST.get('mode', 'listen')  # listen | whisper | barge
    ext        = request.POST.get('supervisor_ext', '')  # supervisor's own extension

    if not agent_id or not ext:
        return JsonResponse({'error': 'agent_id and supervisor_ext required'}, status=400)

    # Get agent's active channel
    try:
        agent_status = AgentStatus.objects.get(user_id=agent_id)
    except AgentStatus.DoesNotExist:
        return JsonResponse({'error': 'Agent not found'}, status=404)

    channel = agent_status.active_channel_id
    if not channel:
        return JsonResponse({'error': 'Agent not on a call'}, status=400)

    # Use Asterisk AMI ChanSpy via ARI originate
    from telephony.models import AsteriskServer
    import requests as req

    server = AsteriskServer.objects.filter(is_active=True).first()
    if not server:
        return JsonResponse({'error': 'No Asterisk server'}, status=503)

    # ChanSpy modes: q=quiet(listen), w=whisper, B=barge
    spy_opts = {'listen': 'q', 'whisper': 'qw', 'barge': 'B'}.get(mode, 'q')

    try:
        res = req.post(
            f'http://{server.ari_host}:{server.ari_port}/ari/channels',
            auth=(server.ari_username, server.ari_password),
            json={
                'endpoint':  f'PJSIP/{ext}',
                'app':       server.ari_app_name,
                'callerId':  'Supervisor',
                'timeout':   30,
                'variables': {
                    'SPY_CHANNEL': channel,
                    'SPY_OPTS':    spy_opts,
                    'CALL_TYPE':   'supervisor_spy',
                },
            },
            timeout=5,
        )
        res.raise_for_status()
        return JsonResponse({'success': True, 'mode': mode})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
