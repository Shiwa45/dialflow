# tests/factories.py
"""
Factory Boy factories for creating test objects.

Usage:
    from tests.factories import UserFactory, CampaignFactory, LeadFactory

    agent   = UserFactory(role='agent')
    campaign = CampaignFactory()
    lead    = LeadFactory()
"""
import factory
from factory.django import DjangoModelFactory
from django.contrib.auth import get_user_model

User = get_user_model()


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    username   = factory.Sequence(lambda n: f'user{n}')
    first_name = factory.Faker('first_name')
    last_name  = factory.Faker('last_name')
    email      = factory.LazyAttribute(lambda o: f'{o.username}@example.com')
    password   = factory.PostGenerationMethodCall('set_password', 'testpass123')
    role       = 'agent'
    is_active  = True


class AgentFactory(UserFactory):
    role = 'agent'


class SupervisorFactory(UserFactory):
    role = 'supervisor'


class AsteriskServerFactory(DjangoModelFactory):
    class Meta:
        model = 'telephony.AsteriskServer'
        django_get_or_create = ('name',)

    name         = factory.Sequence(lambda n: f'TestServer{n}')
    server_ip    = '127.0.0.1'
    ari_host     = '127.0.0.1'
    ari_port     = 8088
    ari_username = 'asterisk'
    ari_password = 'asterisk_ari_password'
    ari_app_name = 'dialflow'
    ami_host     = '127.0.0.1'
    ami_port     = 5038
    ami_username = 'admin'
    ami_password = 'admin'
    is_active    = True


class DispositionFactory(DjangoModelFactory):
    class Meta:
        model = 'campaigns.Disposition'

    name     = factory.Sequence(lambda n: f'Disposition {n}')
    category = 'other'
    outcome  = 'recycle'
    color    = '#6B7280'
    is_active = True


class CampaignFactory(DjangoModelFactory):
    class Meta:
        model = 'campaigns.Campaign'

    name            = factory.Sequence(lambda n: f'Campaign {n}')
    status          = 'draft'
    dial_mode       = 'predictive'
    asterisk_server = factory.SubFactory(AsteriskServerFactory)
    dial_ratio      = 1.5
    min_dial_ratio  = 1.0
    max_dial_ratio  = 3.0
    dial_timeout    = 30
    abandon_rate    = 3.0
    hopper_level    = 100
    max_attempts    = 3


class ActiveCampaignFactory(CampaignFactory):
    status = 'active'


class LeadFactory(DjangoModelFactory):
    class Meta:
        model = 'leads.Lead'

    first_name    = factory.Faker('first_name')
    last_name     = factory.Faker('last_name')
    primary_phone = factory.Sequence(lambda n: f'+9198765{n:05d}')
    email         = factory.Faker('email')
    is_active     = True
    do_not_call   = False
    priority      = 5


class PhoneFactory(DjangoModelFactory):
    class Meta:
        model = 'telephony.Phone'

    extension       = factory.Sequence(lambda n: str(1000 + n))
    name            = factory.LazyAttribute(lambda o: f'Ext {o.extension}')
    phone_type      = 'webrtc'
    asterisk_server = factory.SubFactory(AsteriskServerFactory)
    is_active       = True


class AgentStatusFactory(DjangoModelFactory):
    class Meta:
        model = 'agents.AgentStatus'

    user   = factory.SubFactory(AgentFactory)
    status = 'offline'


class CallLogFactory(DjangoModelFactory):
    class Meta:
        model = 'calls.CallLog'

    phone_number = factory.Sequence(lambda n: f'+9187654{n:05d}')
    direction    = 'outbound'
    status       = 'initiated'
    duration     = 0
