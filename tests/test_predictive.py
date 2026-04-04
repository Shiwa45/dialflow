# tests/test_predictive.py
"""
Tests for the predictive dialing algorithm.
All pure-function tests — no DB, no Asterisk required.
"""
import pytest
from campaigns.predictive import _erlang_c, calculate_dial_ratio, DialerMetrics


# ── Erlang-C formula ─────────────────────────────────────────────────────────

class TestErlangC:

    def test_no_agents_returns_one(self):
        assert _erlang_c(0, 1.0) == 1.0

    def test_zero_intensity_returns_one(self):
        assert _erlang_c(5, 0) == 1.0

    def test_overloaded_returns_one(self):
        # intensity >= agents → system is overloaded
        assert _erlang_c(3, 5.0) == 1.0
        assert _erlang_c(2, 2.0) == 1.0

    def test_single_agent_moderate_load(self):
        # 1 agent, 0.5 Erlang — some waiting expected
        result = _erlang_c(1, 0.5)
        assert 0.0 < result < 1.0

    def test_many_agents_light_load(self):
        # 10 agents, 1 Erlang — very low wait probability
        result = _erlang_c(10, 1.0)
        assert result < 0.05

    def test_symmetry_with_utilisation(self):
        # More agents for same traffic → lower wait probability
        assert _erlang_c(5, 2.0) < _erlang_c(3, 2.0)

    def test_result_is_probability(self):
        for n in range(1, 8):
            for intensity in [0.5, 1.0, 2.0, float(n) - 0.1]:
                result = _erlang_c(n, intensity)
                assert 0.0 <= result <= 1.0, f"n={n} intensity={intensity} got {result}"


# ── Dial ratio calculation ────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCalculateDialRatio:
    def _make_campaign(self, **kwargs):
        from telephony.models import AsteriskServer
        from campaigns.models import Campaign
        server, _ = AsteriskServer.objects.get_or_create(
            name='AlgoServer',
            defaults={
                'server_ip': '10.0.2.1',
                'ari_username': 'u', 'ari_password': 'p',
                'ami_username': 'u', 'ami_password': 'p',
            }
        )
        defaults = {
            'asterisk_server': server,
            'status': 'active',
            'min_dial_ratio': '1.0',
            'max_dial_ratio': '3.0',
            'abandon_rate': '3.0',
        }
        defaults.update(kwargs)
        name = kwargs.get('name', 'AlgoCampaign')
        c, _ = Campaign.objects.get_or_create(name=name, defaults=defaults)
        return c

    def test_ratio_within_campaign_limits(self):
        campaign = self._make_campaign(name='C_limits')
        metrics = DialerMetrics(
            agents_ready=5, agents_on_call=2, avg_talk_time=120,
            answer_rate=60.0, abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        assert 1.0 <= ratio <= 3.0

    def test_high_abandon_throttles_to_min(self):
        campaign = self._make_campaign(name='C_abandon')
        metrics = DialerMetrics(
            agents_ready=5, agents_on_call=2, avg_talk_time=120,
            answer_rate=60.0, abandon_rate=15.0,  # way above 3% target
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        assert ratio == 1.0  # throttled to minimum

    def test_no_agents_returns_zero(self):
        campaign = self._make_campaign(name='C_noagents')
        metrics = DialerMetrics(
            agents_ready=0, agents_on_call=3, avg_talk_time=120,
            answer_rate=60.0, abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        assert ratio == 0.0

    def test_low_answer_rate_conservative(self):
        campaign = self._make_campaign(name='C_lowar')
        metrics = DialerMetrics(
            agents_ready=5, avg_talk_time=120,
            answer_rate=10.0,  # very low answer rate
            abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        # Should be conservative — capped at min + 0.5
        assert ratio <= 1.6

    def test_custom_min_max_respected(self):
        campaign = self._make_campaign(
            name='C_minmax',
            min_dial_ratio='2.0',
            max_dial_ratio='2.5',
        )
        metrics = DialerMetrics(
            agents_ready=5, avg_talk_time=120,
            answer_rate=70.0, abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(metrics, campaign)
        assert 2.0 <= ratio <= 2.5

    def test_ratio_is_float(self):
        campaign = self._make_campaign(name='C_float')
        metrics = DialerMetrics(agents_ready=3, avg_talk_time=90, answer_rate=55.0)
        ratio = calculate_dial_ratio(metrics, campaign)
        assert isinstance(ratio, float)


# ── Hopper helpers ────────────────────────────────────────────────────────────

class TestHopperKeys:
    def test_hopper_key_format(self):
        from campaigns.hopper import hopper_key, dialing_key
        assert hopper_key(42)  == 'campaign:42:hopper'
        assert dialing_key(42) == 'campaign:42:dialing'

    def test_hopper_key_unique_per_campaign(self):
        from campaigns.hopper import hopper_key
        assert hopper_key(1) != hopper_key(2)
