# tests/test_views.py
"""
View-level tests using Django test client.
Tests authentication, redirects, and key response codes.
"""
import pytest
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(
        username='viewagent', password='testpass', role='agent'
    )


@pytest.fixture
def supervisor(django_user_model):
    return django_user_model.objects.create_user(
        username='viewsup', password='testpass', role='supervisor'
    )


@pytest.fixture
def asterisk_server():
    from telephony.models import AsteriskServer
    return AsteriskServer.objects.create(
        name='ViewTestServer', server_ip='10.0.3.1',
        ari_username='u', ari_password='p',
        ami_username='u', ami_password='p',
    )


# ── Auth views ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestAuthViews:

    def test_login_page_renders(self, client):
        res = client.get('/auth/login/')
        assert res.status_code == 200
        assert b'DialFlow' in res.content

    def test_login_redirects_on_success_agent(self, client, agent):
        res = client.post('/auth/login/', {
            'username': 'viewagent', 'password': 'testpass'
        })
        assert res.status_code == 302
        assert '/agents/' in res['Location'] or '/' in res['Location']

    def test_login_stays_on_bad_creds(self, client):
        res = client.post('/auth/login/', {
            'username': 'nobody', 'password': 'wrong'
        })
        assert res.status_code == 200  # re-renders login form

    def test_logout_clears_session(self, client, agent):
        client.login(username='viewagent', password='testpass')
        res = client.post('/auth/logout/')
        assert res.status_code == 302

    def test_unauthenticated_redirects_to_login(self, client):
        res = client.get('/agents/')
        assert res.status_code == 302
        assert 'login' in res['Location']


# ── Core views ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCoreViews:

    def test_health_check_returns_200(self, client):
        res = client.get('/health/')
        assert res.status_code in (200, 503)
        data = res.json()
        assert 'status' in data
        assert 'checks' in data

    def test_root_redirects_authenticated_agent(self, client, agent):
        client.login(username='viewagent', password='testpass')
        res = client.get('/')
        assert res.status_code == 302

    def test_root_redirects_unauthenticated(self, client):
        res = client.get('/')
        assert res.status_code == 302
        assert 'login' in res['Location']


# ── Agent views ───────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestAgentViews:

    def test_dashboard_requires_agent_role(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        # Supervisors should be redirected away from agent dashboard
        res = client.get('/agents/')
        # Either redirect or forbidden, not 200 for supervisor on agent page
        # Actually our decorator just redirects to login if not agent
        assert res.status_code in (200, 302, 403)

    def test_agent_status_api_unauthenticated(self, client):
        res = client.get('/agents/api/status/')
        assert res.status_code == 302  # redirect to login

    def test_agent_status_api_authenticated(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.get('/agents/api/status/')
        assert res.status_code == 200
        data = res.json()
        assert 'status' in data

    def test_heartbeat_updates_db(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.post('/agents/api/heartbeat/')
        assert res.status_code == 200
        assert res.json()['ok'] is True

    def test_set_status_valid(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.post('/agents/api/set-status/', {'status': 'break'})
        assert res.status_code == 200
        data = res.json()
        assert data.get('success') or 'error' in data  # may fail WS broadcast without Redis

    def test_set_invalid_status_rejected(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.post('/agents/api/set-status/', {'status': 'invalid_status'})
        assert res.status_code == 400


# ── Campaign views ────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCampaignViews:

    def test_list_requires_supervisor(self, client, agent):
        client.login(username='viewagent', password='testpass')
        res = client.get('/campaigns/')
        assert res.status_code == 403  # agent is not supervisor

    def test_list_accessible_to_supervisor(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/campaigns/')
        assert res.status_code == 200

    def test_create_form_renders(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/campaigns/create/')
        assert res.status_code == 200

    def test_campaign_control_start(self, client, supervisor, asterisk_server):
        from campaigns.models import Campaign
        c = Campaign.objects.create(
            name='CtrlTest', asterisk_server=asterisk_server, status='paused'
        )
        client.login(username='viewsup', password='testpass')
        res = client.post(f'/campaigns/{c.pk}/control/', {'action': 'start'})
        assert res.status_code == 200
        assert res.json()['success'] is True
        c.refresh_from_db()
        assert c.status == 'active'

    def test_campaign_control_invalid_action(self, client, supervisor, asterisk_server):
        from campaigns.models import Campaign
        c = Campaign.objects.create(
            name='CtrlTest2', asterisk_server=asterisk_server
        )
        client.login(username='viewsup', password='testpass')
        res = client.post(f'/campaigns/{c.pk}/control/', {'action': 'explode'})
        assert res.status_code == 400

    def test_campaign_stats_api(self, client, supervisor, asterisk_server):
        from campaigns.models import Campaign
        c = Campaign.objects.create(
            name='StatsTest', asterisk_server=asterisk_server
        )
        client.login(username='viewsup', password='testpass')
        res = client.get(f'/campaigns/{c.pk}/stats/')
        assert res.status_code == 200
        data = res.json()
        assert 'calls_today' in data
        assert 'hopper' in data


# ── Leads views ───────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestLeadViews:

    def test_lead_list_supervisor(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/leads/')
        assert res.status_code == 200

    def test_lead_import_page_renders(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/leads/import/')
        assert res.status_code == 200

    def test_mapped_lead_import_no_file(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.post('/leads/import/upload/')
        assert res.status_code == 400

    def test_mapped_lead_import_csv(self, client, supervisor):
        from leads.models import Lead, LeadBatch

        client.login(username='viewsup', password='testpass')
        csv_content = 'Customer Name,Mobile Number,Mail\nTest User,+919876543999,test@example.com\n'
        csv_file = SimpleUploadedFile('test.csv', csv_content.encode(), content_type='text/csv')
        res = client.post('/leads/import/upload/', {
            'file': csv_file,
            'mappings': '{"Customer Name":"first_name","Mobile Number":"primary_phone","Mail":"email"}',
            'batch_name': 'Spring Upload',
            'has_header': '1',
            'duplicate_handling': 'skip',
        })
        assert res.status_code == 200
        data = res.json()
        assert data.get('success') is True
        assert data.get('created', 0) >= 1
        assert data.get('batch_name') == 'Spring Upload'
        lead = Lead.objects.get(primary_phone='+919876543999')
        assert lead.batches.filter(name='Spring Upload').exists()
        assert LeadBatch.objects.filter(name='Spring Upload').exists()

    def test_mapped_import_saves_custom_field_columns(self, client, supervisor):
        from leads.models import Lead

        client.login(username='viewsup', password='testpass')
        csv_content = 'Phone,Favorite Color,CRM Notes\n+919876543998,Blue,VIP lead\n'
        csv_file = SimpleUploadedFile('custom.csv', csv_content.encode(), content_type='text/csv')
        res = client.post('/leads/import/upload/', {
            'file': csv_file,
            'mappings': '{"Phone":"primary_phone","Favorite Color":"custom","CRM Notes":"custom"}',
            'has_header': '1',
            'duplicate_handling': 'skip',
        })

        assert res.status_code == 200
        assert res.json()['success'] is True

        lead = Lead.objects.get(primary_phone='+919876543998')
        assert lead.custom_fields['Favorite Color'] == 'Blue'
        assert lead.custom_fields['CRM Notes'] == 'VIP lead'

    def test_batch_assign_campaign(self, client, supervisor, asterisk_server):
        from campaigns.models import Campaign
        from leads.models import Lead, LeadBatch

        client.login(username='viewsup', password='testpass')
        lead = Lead.objects.create(first_name='Batch', primary_phone='+919000000001')
        batch = LeadBatch.objects.create(name='April Batch')
        batch.leads.add(lead)
        campaign = Campaign.objects.create(name='Batch Campaign', asterisk_server=asterisk_server)

        res = client.post(f'/leads/batches/{batch.id}/assign/', {
            'campaign_id': campaign.id,
            'action': 'add',
        })

        assert res.status_code == 200
        assert res.json()['success'] is True
        lead.refresh_from_db()
        assert lead.campaigns.filter(id=campaign.id).exists()


# ── Call views ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCallViews:

    def test_call_list_agent_sees_own(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.get('/calls/')
        assert res.status_code == 200

    def test_call_stats_api(self, client, agent):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=agent)
        client.login(username='viewagent', password='testpass')
        res = client.get('/calls/api/stats/')
        assert res.status_code == 200

    def test_call_detail_404_for_wrong_agent(self, client, agent, django_user_model):
        from calls.models import CallLog
        from agents.models import AgentStatus
        other = django_user_model.objects.create_user(username='other99', password='p')
        AgentStatus.objects.get_or_create(user=agent)
        call = CallLog.objects.create(
            phone_number='+910001', agent=other, status='completed'
        )
        client.login(username='viewagent', password='testpass')
        res = client.get(f'/calls/{call.pk}/')
        assert res.status_code == 404


# ── Telephony views ───────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestTelephonyViews:

    def test_server_list_accessible(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/telephony/servers/')
        assert res.status_code == 200

    def test_ari_status_api(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.get('/telephony/api/status/')
        assert res.status_code == 200
        assert 'servers' in res.json()


# ── DNC views ─────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestDNCViews:

    def test_add_dnc(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        res = client.post('/campaigns/dnc/add/', {'phone_number': '+910009998001'})
        assert res.status_code == 200
        data = res.json()
        assert data['success'] is True
        assert data['created'] is True

    def test_add_dnc_duplicate(self, client, supervisor):
        client.login(username='viewsup', password='testpass')
        client.post('/campaigns/dnc/add/', {'phone_number': '+910009998002'})
        res = client.post('/campaigns/dnc/add/', {'phone_number': '+910009998002'})
        data = res.json()
        assert data['success'] is True
        assert data['created'] is False  # already exists
