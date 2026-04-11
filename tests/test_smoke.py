# tests/test_smoke.py
"""
Smoke tests — verify the app structure is coherent without an Asterisk server.
Run with: pytest tests/ -v
"""
import pytest


@pytest.mark.django_db
class TestUserModel:
    def test_create_agent(self, django_user_model):
        user = django_user_model.objects.create_user(
            username='testagent', password='pass1234', role='agent'
        )
        assert user.is_agent
        assert not user.is_supervisor
        assert not user.is_admin

    def test_create_supervisor(self, django_user_model):
        user = django_user_model.objects.create_user(
            username='testsup', password='pass1234', role='supervisor'
        )
        assert user.is_supervisor
        assert not user.is_agent

    def test_admin_is_supervisor(self, django_user_model):
        user = django_user_model.objects.create_user(
            username='testadmin', password='pass1234', role='admin'
        )
        assert user.is_admin
        assert user.is_supervisor


@pytest.mark.django_db
class TestDisposition:
    def test_create_disposition(self):
        from campaigns.models import Disposition
        d = Disposition.objects.create(
            name='Test Sale', category='sale', outcome='complete', color='#10B981'
        )
        assert d.pk
        assert str(d) == 'Test Sale'

    def test_system_disposition_not_deletable_via_flag(self):
        from campaigns.models import Disposition
        d = Disposition.objects.create(
            name='System DNC', category='dnc', outcome='dnc',
            color='#EF4444', is_system=True
        )
        assert d.is_system is True


@pytest.mark.django_db
class TestLead:
    def test_create_lead(self):
        from leads.models import Lead
        lead = Lead.objects.create(
            first_name='Raj', last_name='Sharma',
            primary_phone='+919876543210',
        )
        assert lead.full_name == 'Raj Sharma'
        assert lead.get_all_phones() == ['+919876543210']

    def test_dnc_check(self):
        from leads.models import Lead
        from campaigns.models import DNCEntry
        lead = Lead.objects.create(
            first_name='DNC', primary_phone='+910000000001',
        )
        DNCEntry.objects.create(phone_number='+910000000001')
        assert DNCEntry.is_dnc('+910000000001') is True
        assert DNCEntry.is_dnc('+919999999999') is False


@pytest.mark.django_db
class TestHopper:
    def test_hopper_fill_empty_when_no_leads(self):
        from unittest.mock import MagicMock, patch
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign
        from campaigns.hopper import fill_hopper

        server = AsteriskServer.objects.create(
            name='Test', server_ip='127.0.0.1',
            ari_username='test', ari_password='test',
            ami_username='test', ami_password='test',
        )
        campaign = Campaign.objects.create(
            name='Test Campaign', asterisk_server=server, status='active'
        )

        mock_r = MagicMock()
        mock_r.llen.return_value = 0   # empty hopper

        with patch('campaigns.hopper.get_redis', return_value=mock_r):
            result = fill_hopper(campaign.id)
        assert result == 0  # No leads assigned


@pytest.mark.django_db
class TestAgentStatus:
    def test_status_transitions(self, django_user_model):
        from agents.models import AgentStatus

        user = django_user_model.objects.create_user(
            username='agent1', password='pass', role='agent'
        )
        # Signal (users/signals.py) creates AgentStatus on user creation
        status, _ = AgentStatus.objects.get_or_create(user=user)

        assert status.status == 'offline'
        assert status.wrapup_elapsed_seconds == 0
        assert status.get_wrapup_seconds_remaining() == -1

    def test_go_ready(self, django_user_model):
        from agents.models import AgentStatus

        user = django_user_model.objects.create_user(
            username='agent2', password='pass', role='agent'
        )
        # Signal (users/signals.py) creates AgentStatus on user creation
        status, _ = AgentStatus.objects.get_or_create(user=user)
        status.status = 'ready'
        status.save(update_fields=['status'])
        status.refresh_from_db()
        assert status.status == 'ready'


@pytest.mark.django_db
class TestPredictiveAlgo:
    def test_erlang_c_no_agents(self):
        from campaigns.predictive import _erlang_c
        assert _erlang_c(0, 1.0) == 1.0

    def test_erlang_c_overloaded(self):
        from campaigns.predictive import _erlang_c
        # More traffic than agents → max wait probability
        assert _erlang_c(2, 3.0) == 1.0

    def test_erlang_c_lightly_loaded(self):
        from campaigns.predictive import _erlang_c
        # 5 agents, 1 Erlang of traffic → low wait prob
        assert _erlang_c(5, 1.0) < 0.1

    def test_dial_ratio_clamped(self):
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign

        server = AsteriskServer.objects.create(
            name='T2', server_ip='127.0.0.2',
            ari_username='t', ari_password='t',
            ami_username='t', ami_password='t',
        )
        campaign = Campaign.objects.create(
            name='Algo Test', asterisk_server=server, status='active',
            min_dial_ratio='1.0', max_dial_ratio='3.0', abandon_rate='3.0',
        )
        metrics = DialerMetrics(
            agents_ready=5, agents_on_call=2,
            avg_talk_time=120, answer_rate=60.0, abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        assert 1.0 <= ratio <= 3.0


@pytest.mark.django_db
class TestCallLog:
    def test_duration_display(self):
        from calls.models import CallLog
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign

        server = AsteriskServer.objects.create(
            name='T3', server_ip='127.0.0.3',
            ari_username='t', ari_password='t',
            ami_username='t', ami_password='t',
        )
        campaign = Campaign.objects.create(
            name='CallLog Test', asterisk_server=server, status='active'
        )
        log = CallLog(duration=95, campaign=campaign, phone_number='+911234567890')
        assert log.duration_display == '1:35'

    def test_recording_url_none_when_empty(self):
        from calls.models import CallLog
        log = CallLog(recording_path='')
        assert log.recording_url is None
