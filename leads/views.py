# leads/views.py
import csv
import io
from pathlib import Path
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from .models import Lead, LeadNote, LeadBatch
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
    qs = Lead.objects.prefetch_related('campaigns', 'batches').filter(is_active=True)

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

    batch_id = request.GET.get('batch')
    if batch_id:
        qs = qs.filter(batches__id=batch_id)

    dnc_filter = request.GET.get('dnc')
    if dnc_filter == '1':
        qs = qs.filter(do_not_call=True)
    elif dnc_filter == '0':
        qs = qs.filter(do_not_call=False)

    qs = qs.distinct()

    paginator = Paginator(qs.order_by('-created_at'), 50)
    page = paginator.get_page(request.GET.get('page'))

    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(
        status__in=['active', 'paused', 'draft']
    ).values('id', 'name').order_by('name')
    batches = LeadBatch.objects.annotate(
        lead_count=Count('leads', distinct=True)
    ).values('id', 'name', 'lead_count').order_by('-created_at')

    return render(request, 'leads/list.html', {
        'page':      page,
        'search':    search,
        'total':     qs.count(),
        'campaigns': campaigns,
        'batches': batches,
        'batch_id': batch_id,
        'campaign_id': campaign_id,
        'dnc_filter': dnc_filter,
    })


@login_required
@supervisor_required
def lead_detail(request, pk):
    lead     = get_object_or_404(Lead, pk=pk)
    notes    = lead.notes.select_related('agent', 'campaign').all()
    attempts = lead.attempts.select_related('campaign', 'call_log').all()[:20]
    calls    = lead.call_logs.select_related('agent', 'campaign', 'disposition').order_by('-started_at')[:20]
    return render(request, 'leads/detail.html', {
        'lead': lead, 'notes': notes, 'attempts': attempts, 'calls': calls,
    })


# ── DNC Toggle ───────────────────────────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def toggle_dnc(request, pk):
    """Toggle DNC status for a lead."""
    lead = get_object_or_404(Lead, pk=pk)

    if lead.do_not_call:
        # Remove from DNC
        lead.do_not_call = False
        lead.save(update_fields=['do_not_call', 'updated_at'])
        from campaigns.models import DNCEntry
        DNCEntry.objects.filter(phone_number=lead.primary_phone).delete()
        return JsonResponse({'success': True, 'dnc': False, 'message': 'Removed from DNC'})
    else:
        # Add to DNC
        reason = request.POST.get('reason', 'Manual DNC from lead management')
        lead.mark_dnc(added_by=request.user, reason=reason)
        return JsonResponse({'success': True, 'dnc': True, 'message': 'Added to DNC'})


@login_required
@supervisor_required
@require_POST
def lead_bulk_dnc(request):
    """Bulk DNC: mark multiple leads as DNC."""
    import json
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    lead_ids = data.get('lead_ids', [])
    action = data.get('action', 'add')  # 'add' or 'remove'

    if not lead_ids:
        return JsonResponse({'error': 'No leads selected'}, status=400)

    leads = Lead.objects.filter(id__in=lead_ids)
    count = 0

    if action == 'add':
        for lead in leads:
            if not lead.do_not_call:
                lead.mark_dnc(added_by=request.user, reason='Bulk DNC action')
                count += 1
    else:
        from campaigns.models import DNCEntry
        for lead in leads:
            if lead.do_not_call:
                lead.do_not_call = False
                lead.save(update_fields=['do_not_call', 'updated_at'])
                DNCEntry.objects.filter(phone_number=lead.primary_phone).delete()
                count += 1

    return JsonResponse({'success': True, 'count': count})


# ── CSV Import ───────────────────────────────────────────────────────────────

@login_required
@supervisor_required
@require_POST
def lead_import(request):
    uploaded = request.FILES.get('file')
    campaign_id = request.POST.get('campaign_id')

    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    try:
        content = uploaded.read().decode('utf-8-sig')
        result = import_leads_from_csv(content, campaign_id=campaign_id)
        messages.success(request, f'Imported {result["created"]} leads, skipped {result["skipped"]}.')
        return JsonResponse({'success': True, **result})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@supervisor_required
def lead_export(request):
    campaign_id = request.GET.get('campaign_id')
    qs = Lead.objects.filter(is_active=True)
    if campaign_id:
        qs = qs.filter(campaigns__id=campaign_id)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="leads.csv"'

    writer = csv.writer(response)
    writer.writerow(['first_name', 'last_name', 'primary_phone', 'email', 'company',
                     'city', 'state', 'country', 'source', 'external_id', 'do_not_call'])
    for lead in qs.values_list('first_name', 'last_name', 'primary_phone', 'email',
                                'company', 'city', 'state', 'country', 'source',
                                'external_id', 'do_not_call'):
        writer.writerow(lead)
    return response


@login_required
@require_POST
def lead_add_note(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    note_text = request.POST.get('note', '').strip()
    camp_id = request.POST.get('campaign_id')

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
    from campaigns.models import Campaign
    campaigns = Campaign.objects.filter(
        status__in=['active', 'paused', 'draft']
    ).values('id', 'name').order_by('name')
    return render(request, 'leads/import_mapping.html', {'campaigns': list(campaigns)})


@login_required
@supervisor_required
@require_POST
def import_mapped(request):
    """Accept any CSV with a field mapping (column->dialflow_field) and import."""
    import io, csv, json

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    try:
        mappings_raw = request.POST.get('mappings', '{}')
        mappings = json.loads(mappings_raw)
    except Exception:
        return JsonResponse({'error': 'Invalid mappings JSON'}, status=400)

    campaign_id = request.POST.get('campaign_id') or None
    batch_name = (request.POST.get('batch_name') or '').strip()
    batch_description = (request.POST.get('batch_description') or '').strip()
    duplicate_handling = request.POST.get('duplicate_handling', 'skip')
    has_header = request.POST.get('has_header', '1') == '1'

    if not batch_name:
        batch_name = Path(uploaded.name).stem.replace('_', ' ').replace('-', ' ').strip() or 'Imported Batch'

    batch = LeadBatch.objects.create(
        name=batch_name,
        description=batch_description,
        source_file=uploaded.name,
        created_by=request.user,
    )

    try:
        raw = uploaded.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        raw = uploaded.read().decode('latin-1')

    reader = csv.reader(io.StringIO(raw))
    all_rows = list(reader)
    if not all_rows:
        return JsonResponse({'success': True, 'created': 0, 'skipped': 0, 'errors': 0})

    if has_header:
        csv_headers = all_rows[0]
        data_rows = all_rows[1:]
    else:
        csv_headers = [f'Column {i+1}' for i in range(len(all_rows[0]))]
        data_rows = all_rows

    col_map = {}
    for idx, header in enumerate(csv_headers):
        field = mappings.get(header, '')
        if field:
            col_map[idx] = field

    from campaigns.models import DNCEntry, Campaign, HopperEntry
    created = skipped = errors = attached = 0

    campaign = None
    if campaign_id:
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            pass

    def attach_existing_lead(existing_lead):
        nonlocal attached
        if batch and not existing_lead.batches.filter(id=batch.id).exists():
            existing_lead.batches.add(batch)
            attached += 1
        if campaign and not existing_lead.campaigns.filter(id=campaign.id).exists():
            existing_lead.campaigns.add(campaign)

    for row in data_rows:
        if not row or all(not c.strip() for c in row):
            continue

        lead_data = {}
        custom_fields = {}
        for idx, field in col_map.items():
            if idx < len(row):
                val = row[idx].strip()
                if val:
                    if field == 'custom':
                        custom_fields[csv_headers[idx] if idx < len(csv_headers) else f'field_{idx}'] = val
                    else:
                        lead_data[field] = val

        phone = lead_data.get('primary_phone', '').strip()
        if not phone or not any(c.isdigit() for c in phone):
            skipped += 1
            continue

        phone = ''.join(c for c in phone if c.isdigit() or c in '+')
        lead_data['primary_phone'] = phone

        if DNCEntry.is_dnc(phone):
            skipped += 1
            continue

        if 'priority' in lead_data:
            try:
                lead_data['priority'] = max(1, min(10, int(lead_data['priority'])))
            except (ValueError, TypeError):
                lead_data['priority'] = 5

        valid_fields = {
            'first_name', 'last_name', 'email', 'company',
            'primary_phone', 'alt_phone_1', 'alt_phone_2',
            'city', 'state', 'zip_code', 'country', 'timezone',
            'source', 'external_id', 'priority',
        }
        clean_data = {k: v for k, v in lead_data.items() if k in valid_fields}

        existing_any = Lead.objects.filter(primary_phone=phone).first()

        if duplicate_handling == 'skip':
            existing = Lead.objects.filter(primary_phone=phone)
            if campaign:
                existing = existing.filter(campaigns=campaign)
            if existing.exists():
                if existing_any:
                    attach_existing_lead(existing_any)
                skipped += 1
                continue

        if duplicate_handling == 'update' and existing_any:
            lead = existing_any
            for k, v in clean_data.items():
                if k != 'primary_phone':
                    setattr(lead, k, v)
            if custom_fields:
                lead.custom_fields = {**(lead.custom_fields or {}), **custom_fields}
            lead.save()
            attach_existing_lead(lead)
            created += 1
            continue

        clean_data.setdefault('source', 'csv_import')
        if custom_fields:
            clean_data['custom_fields'] = custom_fields

        try:
            lead = Lead.objects.create(**clean_data)
            if campaign:
                lead.campaigns.add(campaign)
            lead.batches.add(batch)
            created += 1
        except Exception:
            errors += 1

    return JsonResponse({
        'success': True,
        'created': created,
        'skipped': skipped,
        'errors': errors,
        'attached': attached,
        'batch_id': batch.id,
        'batch_name': batch.name,
    })


@login_required
@supervisor_required
def batch_list(request):
    from campaigns.models import Campaign

    batches = LeadBatch.objects.annotate(
        lead_count=Count('leads', distinct=True),
        campaign_count=Count('leads__campaigns', distinct=True),
    ).order_by('-created_at')
    campaigns = Campaign.objects.filter(
        status__in=['active', 'paused', 'draft']
    ).values('id', 'name').order_by('name')
    return render(request, 'leads/batches.html', {
        'batches': batches,
        'campaigns': campaigns,
    })


@login_required
@supervisor_required
@require_POST
def batch_assign_campaign(request, pk):
    from campaigns.models import Campaign

    batch = get_object_or_404(LeadBatch, pk=pk)
    campaign_id = request.POST.get('campaign_id')
    action = request.POST.get('action', 'add')
    if not campaign_id:
        return JsonResponse({'error': 'Campaign required'}, status=400)

    campaign = get_object_or_404(Campaign, pk=campaign_id)
    leads = batch.leads.all()
    lead_ids = list(leads.values_list('id', flat=True))
    if not lead_ids:
        return JsonResponse({'error': 'This batch has no leads'}, status=400)

    if action == 'remove':
        removed = campaign.leads.filter(id__in=lead_ids).count()
        campaign.leads.remove(*lead_ids)
        return JsonResponse({'success': True, 'count': removed, 'action': 'remove'})

    to_add = list(batch.leads.exclude(campaigns=campaign).values_list('id', flat=True))
    if to_add:
        campaign.leads.add(*to_add)
    return JsonResponse({'success': True, 'count': len(to_add), 'action': 'add'})


# ── Recycle ──────────────────────────────────────────────────────────────────

@login_required
@supervisor_required
def recycle_page(request):
    from campaigns.models import Campaign, Disposition
    campaigns = Campaign.objects.filter(
        status__in=['active', 'paused', 'draft']
    ).values('id', 'name').order_by('name')
    dispositions = Disposition.objects.filter(
        is_active=True, outcome='recycle'
    ).values('id', 'name')
    return render(request, 'leads/recycle.html', {
        'campaigns': campaigns, 'dispositions': dispositions,
    })


@login_required
@supervisor_required
@require_POST
def recycle_leads(request):
    from campaigns.models import Campaign, HopperEntry
    from calls.models import CallLog

    campaign_id = request.POST.get('campaign_id')
    if not campaign_id:
        return JsonResponse({'error': 'Campaign required'}, status=400)

    campaign = get_object_or_404(Campaign, pk=campaign_id)
    disposition_ids = request.POST.getlist('dispositions')
    max_attempts = int(request.POST.get('max_attempts', campaign.max_attempts))

    recycled = 0
    leads = Lead.objects.filter(
        campaigns=campaign,
        is_active=True,
        do_not_call=False,
        call_logs__disposition_id__in=disposition_ids,
    ).distinct()

    for lead in leads:
        attempts = lead.attempts.filter(campaign=campaign).count()
        if attempts < max_attempts:
            _, created = HopperEntry.objects.get_or_create(
                campaign=campaign, lead=lead,
                defaults={'phone_number': lead.primary_phone, 'priority': lead.priority}
            )
            if created:
                recycled += 1

    return JsonResponse({'success': True, 'recycled': recycled})
