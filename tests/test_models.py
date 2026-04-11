# tests/test_models.py
"""
Model-level tests.
No Asterisk, no Redis, no Celery required.
"""
import pytest
from django.utils import timezone


@pytest.mark.django_db
class TestUserModel:

    def test_role_properties(self, django_user_model):
        agent = django_user_model.objects.create_user(username='a1', password='p', role='agent')
        sup   = django_user_model.objects.create_user(username='s1', password='p', role='supervisor')
        admin = django_user_model.objects.create_user(username='ad1', password='p', role='admin')

        assert agent.is_agent and not agent.is_supervisor and not agent.is_admin
        assert sup.is_supervisor and not sup.is_agent
        assert admin.is_admin and admin.is_supervisor  # admin is also supervisor

    def test_superuser_is_admin(self, django_user_model):
        su = django_user_model.objects.create_superuser('su1', 'su@x.com', 'p')
        assert su.is_admin
        assert su.is_supervisor

    def test_deactivate(self, django_user_model):
        user = django_user_model.objects.create_user(username='deact', password='p')
        user.deactivate(note='Test deactivation')
        user.refresh_from_db()
        assert not user.is_active
        assert user.deactivated_note == 'Test deactivation'
        assert user.deactivated_at is not None


@pytest.mark.django_db
class TestAsteriskServer:

    def test_ari_urls(self):
        from telephony.models import AsteriskServer
        server = AsteriskServer.objects.create(
            name='Test', server_ip='192.168.1.10',
            ari_host='192.168.1.10', ari_port=8088,
            ari_username='user', ari_password='pass',
            ari_app_name='dialflow',
            ami_username='ami', ami_password='amipass',
        )
        assert server.ari_base_url == 'http://192.168.1.10:8088/ari'
        assert 'api_key=user:pass' in server.ari_ws_url

    def test_str(self):
        from telephony.models import AsteriskServer
        server = AsteriskServer(name='Main', server_ip='10.0.0.1')
        assert 'Main' in str(server)
        assert '10.0.0.1' in str(server)


@pytest.mark.django_db
class TestPhone:

    def test_get_sip_uri(self):
        from telephony.models import AsteriskServer, Phone
        server = AsteriskServer.objects.create(
            name='S', server_ip='10.0.0.5',
            ari_username='u', ari_password='p',
            ami_username='u', ami_password='p',
        )
        phone = Phone(extension='1001', asterisk_server=server)
        assert phone.get_sip_uri() == 'sip:1001@10.0.0.5'

    def test_secret_auto_generated(self):
        from telephony.models import Phone, AsteriskServer
        server = AsteriskServer.objects.create(
            name='S2', server_ip='10.0.0.6',
            ari_username='u', ari_password='p',
            ami_username='u', ami_password='p',
        )
        phone = Phone.objects.create(
            extension='2001', name='Test Phone', asterisk_server=server
        )
        assert len(phone.secret) >= 16


@pytest.mark.django_db
class TestDisposition:

    def test_str(self):
        from campaigns.models import Disposition
        d = Disposition(name='Sale')
        assert str(d) == 'Sale'

    def test_category_choices_valid(self):
        from campaigns.models import Disposition
        d = Disposition.objects.create(
            name='Callback', category='callback', outcome='callback', color='#3B82F6'
        )
        assert d.get_category_display() == 'Callback'
        assert d.get_outcome_display() == 'Schedule callback'


@pytest.mark.django_db
class TestCampaign:
    def _make_server(self):
        from telephony.models import AsteriskServer
        return AsteriskServer.objects.create(
            name='CS', server_ip='127.0.0.1',
            ari_username='u', ari_password='p',
            ami_username='u', ami_password='p',
        )

    def test_status_transitions(self):
        from campaigns.models import Campaign
        c = Campaign.objects.create(name='C1', asterisk_server=self._make_server())
        assert c.status == 'draft'
        assert not c.is_active

        c.start()
        c.refresh_from_db()
        assert c.status == 'active'
        assert c.is_active

        c.pause()
        c.refresh_from_db()
        assert c.status == 'paused'

        c.stop()
        c.refresh_from_db()
        assert c.status == 'stopped'

    def test_str_includes_status(self):
        from campaigns.models import Campaign
        c = Campaign(name='My Campaign', status='active')
        assert 'My Campaign' in str(c)
        assert 'Active' in str(c)


@pytest.mark.django_db
class TestLead:

    def test_full_name(self):
        from leads.models import Lead
        lead = Lead(first_name='Priya', last_name='Singh')
        assert lead.full_name == 'Priya Singh'

    def test_full_name_no_last(self):
        from leads.models import Lead
        lead = Lead(first_name='Raj', last_name='')
        assert lead.full_name == 'Raj'

    def test_get_all_phones(self):
        from leads.models import Lead
        lead = Lead(
            first_name='X',
            primary_phone='+910001',
            alt_phone_1='+910002',
            alt_phone_2='',
        )
        phones = lead.get_all_phones()
        assert phones == ['+910001', '+910002']

    def test_mark_dnc_sets_flag(self):
        from leads.models import Lead
        from campaigns.models import DNCEntry
        lead = Lead.objects.create(
            first_name='DNC', primary_phone='+910000999001'
        )
        lead.mark_dnc(reason='Test DNC')
        lead.refresh_from_db()
        assert lead.do_not_call is True
        assert DNCEntry.objects.filter(phone_number='+910000999001').exists()

    def test_dnc_is_dnc_method(self):
        from campaigns.models import DNCEntry
        DNCEntry.objects.create(phone_number='+910000888001')
        assert DNCEntry.is_dnc('+910000888001') is True
        assert DNCEntry.is_dnc('+910000888002') is False


@pytest.mark.django_db
class TestAgentStatus:
    def _make_agent(self, django_user_model, username='ag_test'):
        return django_user_model.objects.create_user(
            username=username, password='p', role='agent'
        )

    def test_initial_status_offline(self, django_user_model):
        from agents.models import AgentStatus
        user = self._make_agent(django_user_model, 'ag_init')
        # Signal (users/signals.py) creates AgentStatus on user creation
        status, _ = AgentStatus.objects.get_or_create(user=user)
        assert status.status == 'offline'

    def test_wrapup_elapsed_not_in_wrapup(self, django_user_model):
        from agents.models import AgentStatus
        user = self._make_agent(django_user_model, 'ag_wt')
        status = AgentStatus(user=user, status='ready')
        assert status.wrapup_elapsed_seconds == 0

    def test_wrapup_remaining_no_campaign(self, django_user_model):
        from agents.models import AgentStatus
        user = self._make_agent(django_user_model, 'ag_wr')
        status = AgentStatus(user=user, status='wrapup')
        assert status.get_wrapup_seconds_remaining() == -1

    def test_direct_status_save(self, django_user_model):
        from agents.models import AgentStatus
        user = self._make_agent(django_user_model, 'ag_direct')
        # Signal (users/signals.py) creates AgentStatus on user creation
        status, _ = AgentStatus.objects.get_or_create(user=user)
        status.status = 'ready'
        status.save(update_fields=['status'])
        status.refresh_from_db()
        assert status.status == 'ready'


@pytest.mark.django_db
class TestCallLog:

    def test_duration_display_zero(self):
        from calls.models import CallLog
        log = CallLog(duration=0)
        assert log.duration_display == '0:00'

    def test_duration_display_minutes(self):
        from calls.models import CallLog
        log = CallLog(duration=95)
        assert log.duration_display == '1:35'

    def test_duration_display_hours(self):
        from calls.models import CallLog
        log = CallLog(duration=3661)
        assert log.duration_display == '1:01:01'

    def test_recording_url_empty(self):
        from calls.models import CallLog
        assert CallLog(recording_path='').recording_url is None

    def test_recording_url_set(self, settings):
        from calls.models import CallLog
        settings.DIALER = {'RECORDING_PATH': '/tmp/rec', 'RECORDING_URL_PREFIX': '/recordings/'}
        log = CallLog(recording_path='/var/spool/asterisk/monitor/rec_1_2_20241201.wav')
        assert log.recording_url == '/recordings/rec_1_2_20241201.wav'


@pytest.mark.django_db
class TestDNCEntry:

    def test_scope_system_wide(self):
        from campaigns.models import DNCEntry
        entry = DNCEntry(phone_number='+910001234567', campaign=None)
        assert 'SYSTEM' in str(entry)

    def test_is_dnc_campaign_specific(self):
        from campaigns.models import DNCEntry
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign

        server = AsteriskServer.objects.create(
            name='DNS', server_ip='10.0.1.1',
            ari_username='u', ari_password='p',
            ami_username='u', ami_password='p',
        )
        campaign = Campaign.objects.create(name='DNCCamp', asterisk_server=server)
        DNCEntry.objects.create(phone_number='+910005555001', campaign=campaign)

        # Campaign-specific DNC
        assert DNCEntry.is_dnc('+910005555001', campaign_id=campaign.id) is True
        # System-wide check (no campaign filter) — should be False
        assert DNCEntry.is_dnc('+910005555001') is False
