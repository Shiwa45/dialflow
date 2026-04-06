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


# ── Dynamic CSV import with field mapping ─────────────────────────────────────

@login_required
@supervisor_required
def import_page(request):
    """Render the field-mapping import UI."""
    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(
        status__in=['active','paused','draft']
    ).values('id','name').order_by('name')
    return render(request, 'leads/import_mapping.html', {'campaigns': list(campaigns)})


@login_required
@supervisor_required
@require_POST
def import_mapped(request):
    """
    Accept any CSV with a field mapping (column->dialflow_field) and import.
    FormData: file, mappings (JSON), campaign_id, duplicate_handling, has_header
    """
    import io, csv, json

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    try:
        mappings_raw = request.POST.get('mappings', '{}')
        mappings     = json.loads(mappings_raw)  # {csv_col: dialflow_field}
    except Exception:
        return JsonResponse({'error': 'Invalid mappings JSON'}, status=400)

    campaign_id       = request.POST.get('campaign_id') or None
    duplicate_handling= request.POST.get('duplicate_handling', 'skip')
    has_header        = request.POST.get('has_header', '1') == '1'

    # Decode file content
    try:
        raw = uploaded.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        raw = uploaded.read().decode('latin-1')

    reader  = csv.reader(io.StringIO(raw))
    all_rows = list(reader)
    if not all_rows:
        return JsonResponse({'success': True, 'created': 0, 'skipped': 0, 'errors': 0})

    if has_header:
        csv_headers = all_rows[0]
        data_rows   = all_rows[1:]
    else:
        csv_headers = [f'Column {i+1}' for i in range(len(all_rows[0]))]
        data_rows   = all_rows

    # Build col index -> field mapping
    col_map = {}  # col_index -> dialflow_field
    for idx, header in enumerate(csv_headers):
        field = mappings.get(header, '')
        if field:
            col_map[idx] = field

    # Import rows
    from leads.models import Lead
    from campaigns.models import DNCEntry, Campaign, HopperEntry
    from django.db import IntegrityError

    created = skipped = errors = 0
    leads_to_add_to_campaign = []

    campaign = None
    if campaign_id:
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            pass

    for row in data_rows:
        if not row or all(not c.strip() for c in row):
            continue

        # Build lead data from mapping
        lead_data = {}
        for idx, field in col_map.items():
            if idx < len(row):
                val = row[idx].strip()
                if val:
                    lead_data[field] = val

        phone = lead_data.get('primary_phone', '').strip()
        if not phone or not any(c.isdigit() for c in phone):
            skipped += 1
            continue

        # Clean phone: remove spaces and dashes
        phone = ''.join(c for c in phone if c.isdigit() or c in '+')
        lead_data['primary_phone'] = phone

        # DNC check
        if DNCEntry.is_dnc(phone):
            skipped += 1
            continue

        # Priority validation
        if 'priority' in lead_data:
            try:
                lead_data['priority'] = max(1, min(10, int(lead_data['priority'])))
            except (ValueError, TypeError):
                lead_data['priority'] = 5

        # Valid Lead model fields only
        valid_fields = {
            'first_name','last_name','email','company',
            'primary_phone','alt_phone_1','alt_phone_2',
            'city','state','zip_code','country','timezone',
            'source','external_id','priority',
        }
        clean_data = {k: v for k, v in lead_data.items() if k in valid_fields}

        try:
            if duplicate_handling == 'skip':
                existing = Lead.objects.filter(primary_phone=phone).first()
                if existing:
                    if campaign:
                        existing.campaigns.add(campaign)
                    skipped += 1
                    continue

            elif duplicate_handling == 'update':
                lead, was_created = Lead.objects.update_or_create(
                    primary_phone=phone,
                    defaults=clean_data,
                )
            else:
                was_created = True

            if duplicate_handling != 'update':
                lead = Lead.objects.create(**clean_data)
                was_created = True

            if was_created:
                created += 1

            if campaign:
                lead.campaigns.add(campaign)
                # Add to hopper
                HopperEntry.objects.get_or_create(
                    campaign=campaign,
                    lead=lead,
                    defaults={
                        'phone_number': phone,
                        'status':       'queued',
                        'priority':     clean_data.get('priority', 5),
                    }
                )

        except Exception as exc:
            errors += 1
            continue

    return JsonResponse({'success': True, 'created': created, 'skipped': skipped, 'errors': errors})


# ── Lead recycling ─────────────────────────────────────────────────────────────

@login_required
@supervisor_required
def recycle_page(request):
    """Show leads eligible for recycling with stats."""
    from calls.models import CallLog
    from campaigns.models import Campaign, Disposition
    from django.db.models import Count, Max, OuterRef, Subquery

    campaigns = Campaign.objects.filter(status__in=['active','paused','draft']).order_by('name')

    # Leads that were called but not converted — eligible for recycling
    no_answer_outcomes = ['no_answer', 'busy', 'voicemail', 'failed', 'dropped']
    recyclable_qs = Lead.objects.filter(
        is_active=True,
        do_not_call=False,
    ).annotate(
        attempt_count = Count('attempts'),
        last_called   = Max('call_logs__started_at'),
    ).filter(
        attempt_count__gt=0,
    ).select_related().order_by('-last_called')[:300]

    # Annotate with last outcome
    recyclable = []
    for lead in recyclable_qs:
        last_call = lead.call_logs.order_by('-started_at').first()
        if last_call:
            outcome = last_call.disposition.category if last_call.disposition else last_call.status
            if outcome in ('completed',):
                continue  # already converted — skip
            lead.last_outcome       = outcome
            lead.last_outcome_class = {'no_answer':'break','busy':'warning','voicemail':'offline'}.get(outcome,'stopped')
            lead.last_campaign      = last_call.campaign.name if last_call.campaign else '—'
            lead.last_campaign_id   = last_call.campaign_id or ''
            recyclable.append(lead)

    # Stats
    from calls.models import CallLog
    stats = {
        'no_answer': CallLog.objects.filter(status='no_answer').values('lead').distinct().count(),
        'busy':      CallLog.objects.filter(status='busy').values('lead').distinct().count(),
        'voicemail': CallLog.objects.filter(amd_result__icontains='machine').values('lead').distinct().count(),
        'failed':    CallLog.objects.filter(status='failed').values('lead').distinct().count(),
    }

    return render(request, 'leads/recycle.html', {
        'recyclable':  recyclable,
        'stats':       stats,
        'campaigns':   campaigns,
        'recent_jobs': [],
    })


@login_required
@supervisor_required
@require_POST
def recycle_leads(request):
    """Recycle selected leads back into hopper for a campaign."""
    import json
    from campaigns.models import Campaign, HopperEntry

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    lead_ids    = data.get('lead_ids', [])
    campaign_id = data.get('campaign_id')
    priority    = int(data.get('priority', 5))
    reset_attempts = data.get('reset_attempts', True)

    if not lead_ids or not campaign_id:
        return JsonResponse({'error': 'lead_ids and campaign_id required'}, status=400)

    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return JsonResponse({'error': 'Campaign not found'}, status=404)

    leads = Lead.objects.filter(id__in=lead_ids, is_active=True, do_not_call=False)
    recycled = 0

    for lead in leads:
        # Add to campaign
        lead.campaigns.add(campaign)

        # Reset attempts if requested
        if reset_attempts:
            lead.attempts.all().delete()

        # Add/update hopper entry
        entry, created = HopperEntry.objects.get_or_create(
            campaign = campaign,
            lead     = lead,
            defaults = {
                'phone_number': lead.primary_phone,
                'status':       'queued',
                'priority':     priority,
            }
        )
        if not created:
            entry.status   = 'queued'
            entry.priority = priority
            entry.save(update_fields=['status','priority'])

        recycled += 1

    # Also push to Redis hopper for immediate availability
    from campaigns.hopper import fill_hopper
    fill_hopper(campaign_id)

    return JsonResponse({'success': True, 'recycled': recycled})
