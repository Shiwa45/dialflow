# campaigns/views.py
import json
import logging
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db.models import Count, Q
from .models import Campaign, CampaignAgent, HopperEntry, DNCEntry, Disposition
from .hopper import get_hopper_stats

logger = logging.getLogger('dialflow.dialer')


def supervisor_required(view_func):
    """Decorator: requires admin or supervisor role."""
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_supervisor:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden()
        return view_func(request, *args, **kwargs)
    return _wrapped


@login_required
@supervisor_required
def campaign_list(request):
    campaigns = Campaign.objects.select_related(
        'asterisk_server', 'carrier', 'created_by'
    ).annotate(
        assigned_agents_count=Count('agents', filter=Q(agents__is_active=True), distinct=True)
    ).all()
    return render(request, 'campaigns/list.html', {'campaigns': campaigns})


@login_required
@supervisor_required
def campaign_detail(request, pk):
    campaign     = get_object_or_404(Campaign, pk=pk)
    agents       = CampaignAgent.objects.filter(campaign=campaign, is_active=True).select_related('agent')
    dispositions = campaign.dispositions.filter(is_active=True).order_by('sort_order', 'name')
    hopper_stats = get_hopper_stats(campaign.id)
    return render(request, 'campaigns/detail.html', {
        'campaign':     campaign,
        'agents':       agents,
        'dispositions': dispositions,
        'hopper_stats': hopper_stats,
    })


@login_required
@supervisor_required
def campaign_create(request):
    from .forms import CampaignForm
    form = CampaignForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        campaign = form.save(commit=False)
        campaign.created_by = request.user
        campaign.save()
        form.save_m2m()
        messages.success(request, f'Campaign "{campaign.name}" created.')
        return redirect('campaigns:detail', pk=campaign.pk)
    tabs = ['Basic', 'Dial Settings', 'Hours & Wrapup', 'Leads & DNC']
    return render(request, 'campaigns/form.html', {'form': form, 'title': 'Create Campaign', 'tabs': tabs})


@login_required
@supervisor_required
def campaign_edit(request, pk):
    from .forms import CampaignForm
    campaign = get_object_or_404(Campaign, pk=pk)
    form     = CampaignForm(request.POST or None, instance=campaign)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Campaign settings saved.')
        return redirect('campaigns:detail', pk=campaign.pk)
    tabs = ['Basic', 'Dial Settings', 'Hours & Wrapup', 'Leads & DNC']
    return render(request, 'campaigns/form.html', {'form': form, 'campaign': campaign, 'title': 'Edit Campaign', 'tabs': tabs})


# ── Campaign control API (AJAX / HTMX) ────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def campaign_control(request, pk):
    """Start / pause / stop a campaign. Returns JSON."""
    campaign = get_object_or_404(Campaign, pk=pk)
    action   = request.POST.get('action')

    actions = {
        'start': campaign.start,
        'pause': campaign.pause,
        'stop':  campaign.stop,
    }
    if action not in actions:
        return JsonResponse({'error': 'Invalid action'}, status=400)

    actions[action]()
    return JsonResponse({
        'success': True,
        'status':  campaign.status,
        'display': campaign.get_status_display(),
    })


@login_required
@supervisor_required
def campaign_stats_api(request, pk):
    """Real-time stats for a campaign — polled by HTMX if WS not available."""
    campaign     = get_object_or_404(Campaign, pk=pk)
    hopper_stats = get_hopper_stats(campaign.id)
    return JsonResponse({
        'calls_today':    campaign.stat_calls_today,
        'answered_today': campaign.stat_answered_today,
        'abandon_rate':   float(campaign.stat_abandon_rate),
        'agents_active':  campaign.stat_agents_active,
        'hopper':         hopper_stats,
        'status':         campaign.status,
    })


# ── Disposition management ────────────────────────────────────────────────────

@login_required
@supervisor_required
def disposition_list(request):
    dispositions = Disposition.objects.all()
    return render(request, 'campaigns/dispositions.html', {'dispositions': dispositions})


@login_required
@supervisor_required
@require_POST
def disposition_create(request):
    from .forms import DispositionForm
    form = DispositionForm(request.POST)
    if form.is_valid():
        disp = form.save()
        return JsonResponse({'success': True, 'id': disp.id, 'name': disp.name})
    return JsonResponse({'success': False, 'errors': form.errors}, status=400)


@login_required
@supervisor_required
@require_POST
def disposition_delete(request, pk):
    disp = get_object_or_404(Disposition, pk=pk)
    if disp.is_system:
        return JsonResponse({'error': 'Cannot delete system disposition'}, status=400)
    disp.delete()
    return JsonResponse({'success': True})


# ── DNC management ────────────────────────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def dnc_add(request):
    phone    = request.POST.get('phone_number', '').strip()
    camp_id  = request.POST.get('campaign_id')
    reason   = request.POST.get('reason', '')

    if not phone:
        return JsonResponse({'error': 'Phone number required'}, status=400)

    entry, created = DNCEntry.objects.get_or_create(
        phone_number=phone,
        campaign_id=camp_id or None,
        defaults={'added_by': request.user, 'reason': reason},
    )
    return JsonResponse({'success': True, 'created': created})


@login_required
@supervisor_required
def dnc_list(request):
    entries   = DNCEntry.objects.select_related('campaign', 'added_by').order_by('-created_at')[:500]
    campaigns = Campaign.objects.filter(status__in=['active','paused','draft']).order_by('name')
    return render(request, 'campaigns/dnc.html', {'entries': entries, 'campaigns': campaigns})


# ── Campaign Agent Assignment ─────────────────────────────────────────────────

@login_required
@supervisor_required
def agent_assignment(request, pk):
    """View and manage agent assignments for a campaign."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    campaign = get_object_or_404(Campaign, pk=pk)
    assigned = CampaignAgent.objects.filter(campaign=campaign).select_related('agent')
    assigned_ids = assigned.values_list('agent_id', flat=True)
    available_agents = User.objects.filter(role='agent', is_active=True).exclude(id__in=assigned_ids)

    return render(request, 'campaigns/agents.html', {
        'campaign':        campaign,
        'assigned':        assigned,
        'available_agents': available_agents,
    })


@login_required
@supervisor_required
@require_POST
def agent_assign(request, pk):
    """Add an agent to a campaign."""
    campaign = get_object_or_404(Campaign, pk=pk)
    agent_id = request.POST.get('agent_id')
    if not agent_id:
        return JsonResponse({'error': 'agent_id required'}, status=400)
    ca, created = CampaignAgent.objects.get_or_create(
        campaign=campaign,
        agent_id=agent_id,
        defaults={'is_active': True},
    )
    if not created:
        ca.is_active = True
        ca.save(update_fields=['is_active'])
    return JsonResponse({'success': True, 'created': created})


@login_required
@supervisor_required
@require_POST
def agent_unassign(request, pk):
    """Remove an agent from a campaign."""
    campaign = get_object_or_404(Campaign, pk=pk)
    agent_id = request.POST.get('agent_id')
    CampaignAgent.objects.filter(campaign=campaign, agent_id=agent_id).update(is_active=False)
    return JsonResponse({'success': True})


# ── Hopper Manual Refill ──────────────────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def hopper_refill(request, pk):
    """Force-fill the hopper for a campaign immediately."""
    from campaigns.hopper import fill_hopper
    campaign = get_object_or_404(Campaign, pk=pk)
    added = fill_hopper(campaign.id, target=campaign.hopper_size)
    return JsonResponse({'success': True, 'added': added,
                         'message': f'Added {added} leads to hopper'})


@login_required
@supervisor_required
@require_POST
def hopper_reset_stale(request, pk):
    """Reset stale in-flight dialing entries back into the hopper."""
    from campaigns.hopper import force_reset_dialing, get_hopper_stats
    campaign = get_object_or_404(Campaign, pk=pk)
    reset = force_reset_dialing(campaign.id)
    stats = get_hopper_stats(campaign.id)
    logger.info(
        f'Manual stale reset: campaign={campaign.id} user={request.user.username} '
        f'reset={reset} queued={stats["queued"]} in_flight={stats["in_flight"]}'
    )
    return JsonResponse({
        'success': True,
        'reset': reset,
        'hopper': stats,
        'message': f'Reset {reset} stale dialing entr{"y" if reset == 1 else "ies"}',
    })


# ── DNC Bulk CSV Import ───────────────────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def dnc_import(request):
    """Import DNC numbers from a CSV file (one number per line or column)."""
    uploaded   = request.FILES.get('file')
    campaign_id = request.POST.get('campaign_id') or None
    if not uploaded:
        return JsonResponse({'error': 'No file'}, status=400)

    content = uploaded.read().decode('utf-8-sig').strip()
    lines   = [l.strip() for l in content.replace(',', '\n').split('\n') if l.strip()]

    added = skipped = 0
    for line in lines:
        # Take the first token (handles CSV rows with extra columns)
        phone = line.split(',')[0].strip().split()[0]
        if not phone or not any(c.isdigit() for c in phone):
            skipped += 1
            continue
        _, created = DNCEntry.objects.get_or_create(
            phone_number=phone,
            campaign_id=campaign_id,
            defaults={'added_by': request.user, 'reason': 'Bulk CSV import'},
        )
        if created:
            added += 1
        else:
            skipped += 1

    return JsonResponse({'success': True, 'added': added, 'skipped': skipped})


# ── Callbacks List ────────────────────────────────────────────────────────────

@login_required
@supervisor_required
def callback_list(request):
    """Leads scheduled for callback."""
    from agents.models import CallDisposition
    from django.utils import timezone

    upcoming = CallDisposition.objects.filter(
        callback_at__isnull=False,
        callback_at__gte=timezone.now(),
    ).select_related('lead', 'campaign', 'agent', 'disposition').order_by('callback_at')

    past_due = CallDisposition.objects.filter(
        callback_at__isnull=False,
        callback_at__lt=timezone.now(),
        call_log__disposition__isnull=True,
    ).select_related('lead', 'campaign', 'agent').order_by('-callback_at')[:50]

    return render(request, 'campaigns/callbacks.html', {
        'upcoming': upcoming,
        'past_due': past_due,
    })


# ── Hopper stats API (for lead monitor) ──────────────────────────────────────

@login_required
def hopper_stats_api_all(request):
    """Return hopper stats for all active campaigns (for lead monitor dashboard)."""
    campaigns = Campaign.objects.filter(status__in=['active','paused'])
    from campaigns.hopper import get_hopper_stats
    result = [
        {'id': c.id, 'name': c.name, 'status': c.status, **get_hopper_stats(c.id)}
        for c in campaigns
    ]
    return JsonResponse({'campaigns': result})
