# tests/test_importer.py
"""
Tests for the CSV lead importer.
"""
import pytest
from leads.importer import import_leads_from_csv, _normalise_header, FIELD_ALIASES


class TestNormaliseHeader:
    def test_lowercase_and_strip(self):
        assert _normalise_header('  Phone  ') == 'primary_phone'
        assert _normalise_header('FIRST NAME') == 'first_name'

    def test_alias_resolution(self):
        assert _normalise_header('phone') == 'primary_phone'
        assert _normalise_header('mobile') == 'primary_phone'
        assert _normalise_header('fname') == 'first_name'
        assert _normalise_header('surname') == 'last_name'
        assert _normalise_header('mail') == 'email'

    def test_unknown_header_passed_through(self):
        assert _normalise_header('custom_field_x') == 'custom_field_x'

    def test_hyphenated(self):
        assert _normalise_header('alt-phone-1') == 'alt_phone_1'


@pytest.mark.django_db
class TestImportLeadsFromCSV:

    def _csv(self, rows, header='first_name,last_name,primary_phone'):
        lines = [header] + rows
        return '\n'.join(lines)

    def test_basic_import(self):
        csv = self._csv([
            'Raj,Sharma,+919876541001',
            'Priya,Singh,+919876541002',
        ])
        result = import_leads_from_csv(csv)
        assert result['created'] == 2
        assert result['skipped'] == 0
        assert result['errors'] == []

    def test_alias_headers(self):
        csv = 'phone,fname,lname\n+919876541010,Ali,Khan\n'
        result = import_leads_from_csv(csv)
        assert result['created'] == 1
        from leads.models import Lead
        lead = Lead.objects.get(primary_phone='+919876541010')
        assert lead.first_name == 'Ali'
        assert lead.last_name == 'Khan'

    def test_skips_blank_phone(self):
        csv = self._csv([',NoPhone,'])
        result = import_leads_from_csv(csv)
        assert result['created'] == 0
        assert result['skipped'] == 1

    def test_skips_dnc_numbers(self):
        from campaigns.models import DNCEntry
        DNCEntry.objects.create(phone_number='+919876541020')
        csv = self._csv([
            'Test,DNC,+919876541020',
            'Test,OK,+919876541021',
        ])
        result = import_leads_from_csv(csv)
        assert result['created'] == 1  # only the non-DNC one
        assert result['skipped'] == 1

    def test_deduplicates_by_phone(self):
        csv = self._csv([
            'First,Import,+919876541030',
            'Second,Import,+919876541030',  # same phone
        ])
        result = import_leads_from_csv(csv)
        assert result['created'] == 1
        assert result['skipped'] == 1

    def test_global_dedup_existing_lead(self):
        from leads.models import Lead
        Lead.objects.create(first_name='Existing', primary_phone='+919876541040')
        csv = self._csv(['New,Lead,+919876541040'])
        result = import_leads_from_csv(csv)
        assert result['created'] == 0
        assert result['skipped'] == 1

    def test_assigns_to_campaign(self):
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign
        server = AsteriskServer.objects.create(
            name='ImportSrv', server_ip='10.0.4.1',
            ari_username='u', ari_password='p',
            ami_username='u', ami_password='p',
        )
        campaign = Campaign.objects.create(name='ImportCampaign', asterisk_server=server)
        csv = self._csv(['Import,CampTest,+919876541050'])
        result = import_leads_from_csv(csv, campaign_id=campaign.id)
        assert result['created'] == 1
        from leads.models import Lead
        lead = Lead.objects.get(primary_phone='+919876541050')
        assert campaign in lead.campaigns.all()

    def test_priority_coercion(self):
        csv = self._csv(
            ['Hi,Pri,+919876541060,1'],
            header='first_name,last_name,primary_phone,priority'
        )
        result = import_leads_from_csv(csv)
        assert result['created'] == 1
        from leads.models import Lead
        lead = Lead.objects.get(primary_phone='+919876541060')
        assert lead.priority == 1

    def test_invalid_priority_defaults_to_5(self):
        csv = self._csv(
            ['Bad,Pri,+919876541070,notanumber'],
            header='first_name,last_name,primary_phone,priority'
        )
        result = import_leads_from_csv(csv)
        assert result['created'] == 1
        from leads.models import Lead
        lead = Lead.objects.get(primary_phone='+919876541070')
        assert lead.priority == 5

    def test_utf8_bom_handled(self):
        # Excel exports often add BOM
        csv = '\ufeffphone,first_name\n+919876541080,BOM_Test\n'
        result = import_leads_from_csv(csv)
        assert result['created'] == 1
        from leads.models import Lead
        assert Lead.objects.filter(primary_phone='+919876541080').exists()

    def test_empty_csv_no_crash(self):
        result = import_leads_from_csv('first_name,primary_phone\n')
        assert result['created'] == 0
        assert result['skipped'] == 0
