# leads/views.py
import csv
import io
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.paginator import Paginator
from .models import Lead, LeadNote
from .importer import import_leads_from_csv


def supervisor_required(view_func):
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
def lead_list(request):
    qs = Lead.objects.prefetch_related('campaigns').filter(is_active=True)

    # Filters
    search = request.GET.get('q', '').strip()
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(primary_phone__icontains=search) |
            Q(email__icontains=search)
        )

    campaign_id = request.GET.get('campaign')
    if campaign_id:
        qs = qs.filter(campaigns__id=campaign_id)

    paginator = Paginator(qs.order_by('-created_at'), 50)
    page      = paginator.get_page(request.GET.get('page'))

    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(
        status__in=['active', 'paused', 'draft']
    ).values('id', 'name').order_by('name')

    return render(request, 'leads/list.html', {
        'page':      page,
        'search':    search,
        'total':     qs.count(),
        'campaigns': campaigns,
    })


@login_required
@supervisor_required
def lead_detail(request, pk):
    lead     = get_object_or_404(Lead, pk=pk)
    notes    = lead.notes.select_related('agent', 'campaign').all()
    attempts = lead.attempts.select_related('campaign', 'call_log').all()[:20]
    return render(request, 'leads/detail.html', {
        'lead': lead, 'notes': notes, 'attempts': attempts
    })


@login_required
@supervisor_required
@require_POST
def lead_import(request):
    """CSV import: first row = headers, subsequent rows = lead data."""
    uploaded = request.FILES.get('file')
    campaign_id = request.POST.get('campaign_id')

    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    try:
        content  = uploaded.read().decode('utf-8-sig')
        result   = import_leads_from_csv(content, campaign_id=campaign_id)
        messages.success(request, f'Imported {result["created"]} leads, skipped {result["skipped"]}.')
        return JsonResponse({'success': True, **result})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@supervisor_required
def lead_export(request):
    """Export leads as CSV."""
    campaign_id = request.GET.get('campaign_id')
    qs = Lead.objects.filter(is_active=True)
    if campaign_id:
        qs = qs.filter(campaigns__id=campaign_id)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="leads.csv"'

    writer = csv.writer(response)
    writer.writerow(['first_name','last_name','primary_phone','email','company',
                     'city','state','country','source','external_id'])
    for lead in qs.values_list('first_name','last_name','primary_phone','email',
                                'company','city','state','country','source','external_id'):
        writer.writerow(lead)
    return response


@login_required
@require_POST
def lead_add_note(request, pk):
    lead    = get_object_or_404(Lead, pk=pk)
    note_text = request.POST.get('note', '').strip()
    camp_id   = request.POST.get('campaign_id')

    if not note_text:
        return JsonResponse({'error': 'Empty note'}, status=400)

    note = LeadNote.objects.create(
        lead=lead, agent=request.user,
        campaign_id=camp_id or None,
        note=note_text,
    )
    return JsonResponse({
        'success': True,
        'id':      note.id,
        'note':    note.note,
        'agent':   request.user.get_full_name() or request.user.username,
        'created': note.created_at.isoformat(),
    })
