# leads/importer.py
"""
CSV Lead Importer
=================
Flexible CSV importer. Accepts any column order.

Required column: primary_phone (or phone / phone_number)
Optional columns: first_name, last_name, email, company,
                  city, state, country, timezone, source,
                  external_id, priority, alt_phone_1, alt_phone_2

Skips:
  - Rows with blank phone numbers
  - Numbers on system-wide DNC list
  - Duplicates (phone already in DB for this campaign)

Returns: {'created': N, 'skipped': N, 'errors': [...]}
"""
import csv
import io
import logging
from typing import Dict, List, Optional

from django.db import transaction

from .models import Lead

logger = logging.getLogger('dialflow')

# Map of recognised header aliases → canonical field name
FIELD_ALIASES = {
    'phone':         'primary_phone',
    'phone_number':  'primary_phone',
    'mobile':        'primary_phone',
    'cell':          'primary_phone',
    'contact':       'primary_phone',
    'name':          'first_name',
    'full_name':     'first_name',
    'fname':         'first_name',
    'lname':         'last_name',
    'surname':       'last_name',
    'mail':          'email',
    'email_address': 'email',
}

ALLOWED_FIELDS = {
    'first_name', 'last_name', 'primary_phone', 'alt_phone_1', 'alt_phone_2',
    'email', 'company', 'address', 'city', 'state', 'zip_code', 'country',
    'timezone', 'source', 'external_id', 'priority',
}


def _normalise_header(raw: str) -> str:
    # Strip whitespace AND the UTF-8 BOM that Excel adds to the first column
    key = raw.strip().lstrip('\ufeff').lower().replace(' ', '_').replace('-', '_')
    return FIELD_ALIASES.get(key, key)


def import_leads_from_csv(
    content: str,
    campaign_id: Optional[int] = None,
    source: str = 'csv_import',
) -> Dict:
    from campaigns.models import DNCEntry, Campaign

    reader   = csv.DictReader(io.StringIO(content))
    headers  = [_normalise_header(h) for h in (reader.fieldnames or [])]

    created   = 0
    skipped   = 0
    errors: List[str] = []

    # Pre-fetch system DNC numbers for fast lookup
    dnc_set = set(DNCEntry.objects.filter(campaign__isnull=True).values_list('phone_number', flat=True))

    # Campaign DNC
    if campaign_id:
        dnc_set |= set(DNCEntry.objects.filter(campaign_id=campaign_id).values_list('phone_number', flat=True))

    # Existing phones in this campaign (skip duplicates)
    existing_phones: set = set()
    if campaign_id:
        existing_phones = set(
            Lead.objects.filter(campaigns__id=campaign_id).values_list('primary_phone', flat=True)
        )

    with transaction.atomic():
        for row_num, raw_row in enumerate(reader, start=2):
            # Remap headers
            row = {}
            for raw_key, value in raw_row.items():
                key = _normalise_header(raw_key)
                if key in ALLOWED_FIELDS:
                    row[key] = (value or '').strip()

            phone = row.get('primary_phone', '').strip()
            if not phone:
                skipped += 1
                continue

            if phone in dnc_set:
                skipped += 1
                continue

            if phone in existing_phones:
                skipped += 1
                continue

            # Set defaults
            row.setdefault('first_name', 'Unknown')
            row.setdefault('source', source)
            row.setdefault('country', 'IN')
            row.setdefault('timezone', 'Asia/Kolkata')

            # Coerce priority to int
            try:
                row['priority'] = int(row.get('priority', 5))
            except (ValueError, TypeError):
                row['priority'] = 5

            try:
                lead, lead_created = Lead.objects.get_or_create(
                    primary_phone=phone,
                    defaults={k: v for k, v in row.items() if k in ALLOWED_FIELDS},
                )

                if campaign_id:
                    lead.campaigns.add(campaign_id)

                if lead_created:
                    created += 1
                    existing_phones.add(phone)
                else:
                    skipped += 1  # duplicate by phone globally

            except Exception as e:
                errors.append(f'Row {row_num}: {e}')
                skipped += 1
                if len(errors) > 50:
                    errors.append('Too many errors — stopping early.')
                    break

    logger.info(f'Lead import: created={created} skipped={skipped} errors={len(errors)}')
    return {'created': created, 'skipped': skipped, 'errors': errors}
