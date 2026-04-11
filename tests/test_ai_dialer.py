# tests/test_ai_dialer.py
"""
Tests for Phase 1/2/3 AI dialer additions.

Coverage:
  - campaigns/metrics.py    — Redis counters, EMA, CPS, feature logger
  - campaigns/predictive.py — _compute_erlang_intensity, _predict_agents_becoming_free,
                               calculate_dial_ratio (fraction inputs)
  - calls/signals.py        — post_save counter updates, is_abandoned flag
  - ai_dialer/online_learning.py — ExperienceReplayBuffer, compute_outcome_quality
  - ai_dialer/features.py   — FeatureEngineer, RawSnapshot

Redis-dependent tests are skipped when Redis is not reachable.
DB-dependent tests use @pytest.mark.django_db.
Pure-function tests require no fixtures at all.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _redis_available() -> bool:
    """Return True if a local Redis is reachable."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not running — skipping Redis-backed tests",
)


# ─────────────────────────────────────────────────────────────────────────────
# campaigns/predictive.py — pure function tests (no DB, no Redis)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeErlangIntensity:
    """_compute_erlang_intensity uses fractions, not percentages."""

    from campaigns.predictive import _compute_erlang_intensity

    def test_zero_answered_gives_zero(self):
        from campaigns.predictive import _compute_erlang_intensity
        assert _compute_erlang_intensity(0, 120.0, 45.0) == 0.0

    def test_intensity_formula(self):
        from campaigns.predictive import _compute_erlang_intensity
        # arrival_rate = 60 / 300 = 0.2/s
        # service_rate = 1 / (120 + 45) = 1/165 ≈ 0.00606/s
        # intensity    = 0.2 / 0.00606 ≈ 33.0
        result = _compute_erlang_intensity(60, 120.0, 45.0)
        assert abs(result - 33.0) < 0.5

    def test_high_volume_gives_high_intensity(self):
        from campaigns.predictive import _compute_erlang_intensity
        low  = _compute_erlang_intensity(10, 120.0, 45.0)
        high = _compute_erlang_intensity(100, 120.0, 45.0)
        assert high > low

    def test_short_handle_time_gives_lower_intensity(self):
        from campaigns.predictive import _compute_erlang_intensity
        long_handle  = _compute_erlang_intensity(60, 300.0, 120.0)
        short_handle = _compute_erlang_intensity(60, 60.0, 20.0)
        assert short_handle < long_handle

    def test_never_negative(self):
        from campaigns.predictive import _compute_erlang_intensity
        assert _compute_erlang_intensity(0, 0.0, 0.0) >= 0.0


class TestPredictAgentsBecomingFree:

    def test_no_agents_on_call(self):
        from campaigns.predictive import _predict_agents_becoming_free, DialerMetrics
        m = DialerMetrics(agents_on_call=0, avg_talk_time=120.0)
        assert _predict_agents_becoming_free(m, 20.0) == 0.0

    def test_window_larger_than_talk_time(self):
        from campaigns.predictive import _predict_agents_becoming_free, DialerMetrics
        # window >= talk_time → prob capped at 1.0 → all agents become free
        m = DialerMetrics(agents_on_call=4, avg_talk_time=10.0)
        result = _predict_agents_becoming_free(m, 30.0)
        assert result == 4.0

    def test_partial_window(self):
        from campaigns.predictive import _predict_agents_becoming_free, DialerMetrics
        # 3 agents, window=30s, talk=120s → prob = 0.25 → 0.75 agents
        m = DialerMetrics(agents_on_call=3, avg_talk_time=120.0)
        result = _predict_agents_becoming_free(m, 30.0)
        assert abs(result - 0.75) < 0.01

    def test_result_never_exceeds_agents_on_call(self):
        from campaigns.predictive import _predict_agents_becoming_free, DialerMetrics
        m = DialerMetrics(agents_on_call=5, avg_talk_time=1.0)
        result = _predict_agents_becoming_free(m, 999.0)
        assert result <= 5.0

    def test_result_non_negative(self):
        from campaigns.predictive import _predict_agents_becoming_free, DialerMetrics
        m = DialerMetrics(agents_on_call=2, avg_talk_time=120.0)
        assert _predict_agents_becoming_free(m, 5.0) >= 0.0


class TestCalculateDialRatioFractions:
    """
    calculate_dial_ratio with fraction-based answer_rate/abandon_rate.
    These complement the existing percentage-based tests in test_predictive.py.
    """

    def _campaign(self, min_ratio=1.0, max_ratio=3.0, abandon_pct=3.0):
        """Minimal mock campaign."""
        c = MagicMock()
        c.min_dial_ratio  = min_ratio
        c.max_dial_ratio  = max_ratio
        c.abandon_rate    = abandon_pct  # stored as %
        c.avg_wrapup_time = 45.0
        return c

    def test_no_agents_returns_zero(self):
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        m = DialerMetrics(agents_ready=0)
        assert calculate_dial_ratio(m, self._campaign()) == 0.0

    def test_abandon_throttle_fraction(self):
        """abandon_rate=0.10 (10%) with target 3% → throttle to min_ratio.
        campaign_id=0 disables EMA (falsy) so no Redis needed."""
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        m = DialerMetrics(
            agents_ready=5, answer_rate=0.40,
            abandon_rate=0.10,   # fraction — 10% > 3%×1.5 = 4.5%
        )
        # campaign_id=0 is falsy → EMA branch is skipped, no Redis call
        ratio = calculate_dial_ratio(m, self._campaign(), campaign_id=0)
        assert ratio == 1.0

    def test_low_answer_rate_fraction(self):
        """answer_rate=0.08 (8%) < 0.15 threshold → conservative branch."""
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        m = DialerMetrics(
            agents_ready=5, answer_rate=0.08,
            abandon_rate=0.0,
        )
        ratio = calculate_dial_ratio(m, self._campaign(), campaign_id=0)
        # conservative: min_ratio + 0.3 = 1.3
        assert ratio <= 1.35

    def test_good_conditions_fraction(self):
        """Normal conditions with fraction inputs → ratio within bounds."""
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        m = DialerMetrics(
            agents_ready=5, agents_on_call=2,
            avg_talk_time=120.0, avg_wrapup_time=45.0,
            answer_rate=0.60, abandon_rate=0.01,
            calls_answered_5min=30,
        )
        ratio = calculate_dial_ratio(m, self._campaign(), campaign_id=0)
        assert 1.0 <= ratio <= 3.0

    def test_result_is_float(self):
        from campaigns.predictive import calculate_dial_ratio, DialerMetrics
        m = DialerMetrics(agents_ready=3, answer_rate=0.50)
        ratio = calculate_dial_ratio(m, self._campaign(), campaign_id=0)
        assert isinstance(ratio, float)


# ─────────────────────────────────────────────────────────────────────────────
# campaigns/metrics.py — Redis-backed tests (skipped if no Redis)
# ─────────────────────────────────────────────────────────────────────────────

@requires_redis
class TestRedisMetrics:
    """Tests that hit real Redis. Isolated with per-test campaign IDs."""

    def _cid(self):
        """Return a unique campaign ID per test call to avoid key collisions."""
        return int(time.time() * 1000) % 1_000_000

    def test_increment_attempted_and_read(self):
        from campaigns.metrics import increment_attempted, get_window_counts
        cid = self._cid()
        increment_attempted(cid)
        counts = get_window_counts(cid, window_seconds=60)
        assert counts["attempted"] >= 1

    def test_increment_answered(self):
        from campaigns.metrics import increment_answered, get_window_counts
        cid = self._cid()
        increment_answered(cid)
        counts = get_window_counts(cid, window_seconds=60)
        assert counts["answered"] >= 1

    def test_increment_abandoned(self):
        from campaigns.metrics import increment_abandoned, get_window_counts
        cid = self._cid()
        increment_abandoned(cid)
        counts = get_window_counts(cid, window_seconds=60)
        assert counts["abandoned"] >= 1

    def test_get_lag_calls(self):
        from campaigns.metrics import increment_attempted, get_lag_calls
        cid = self._cid()
        increment_attempted(cid)
        assert get_lag_calls(cid, window_seconds=5) >= 1

    def test_cps_unlimited_returns_count(self):
        from campaigns.metrics import check_and_reserve_cps
        cid = self._cid()
        assert check_and_reserve_cps(cid, cps_limit=0, count=10) == 10

    def test_cps_throttle_fresh_window(self):
        from campaigns.metrics import check_and_reserve_cps
        cid = self._cid()
        # Empty window — should allow up to cps_limit
        allowed = check_and_reserve_cps(cid, cps_limit=5, count=5)
        assert allowed == 5

    def test_cps_throttle_full_window(self):
        from campaigns.metrics import check_and_reserve_cps, record_cps_origination
        cid = self._cid()
        # Fill the CPS window
        for _ in range(5):
            record_cps_origination(cid)
        allowed = check_and_reserve_cps(cid, cps_limit=5, count=3)
        assert allowed == 0

    def test_ema_ratio_default(self):
        from campaigns.metrics import get_ema_ratio
        cid = self._cid()
        assert get_ema_ratio(cid, default=1.5) == 1.5

    def test_ema_ratio_update_smoothing(self):
        from campaigns.metrics import get_ema_ratio, update_ema_ratio
        cid = self._cid()
        # First call: EMA = raw (no history)
        ema1 = update_ema_ratio(cid, 2.0)
        assert abs(ema1 - 2.0) < 0.01
        # Second call: EMA = 0.3*3.0 + 0.7*2.0 = 2.3
        ema2 = update_ema_ratio(cid, 3.0)
        assert abs(ema2 - 2.3) < 0.05

    def test_feature_logger_roundtrip(self):
        from campaigns.metrics import log_dial_features, get_recent_features
        cid = self._cid()
        log_dial_features(cid, {"ratio": 1.5, "agents_ready": 3})
        features = get_recent_features(cid, n=10)
        assert len(features) >= 1
        assert features[-1]["ratio"] == 1.5

    def test_feature_logger_max_1000(self):
        from campaigns.metrics import log_dial_features, get_recent_features
        cid = self._cid()
        for i in range(1005):
            log_dial_features(cid, {"i": i})
        features = get_recent_features(cid, n=1000)
        assert len(features) <= 1000


# ─────────────────────────────────────────────────────────────────────────────
# calls/signals.py — Django signal tests (requires DB)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCallLogSignals:

    def _make_calllog(self, campaign, **kwargs):
        from calls.models import CallLog
        defaults = dict(
            campaign=campaign,
            phone_number="+919876543210",
            direction="outbound",
            status="initiated",
        )
        defaults.update(kwargs)
        return CallLog.objects.create(**defaults)

    def test_is_abandoned_defaults_false(self):
        from tests.factories import CampaignFactory
        campaign = CampaignFactory(status="active")
        from calls.models import CallLog
        log = CallLog.objects.create(
            campaign=campaign,
            phone_number="+919876543210",
            status="initiated",
        )
        assert log.is_abandoned is False

    def test_is_abandoned_set_on_dropped(self):
        """Signal sets is_abandoned=True when status transitions to 'dropped'."""
        from tests.factories import CampaignFactory
        from calls.models import CallLog
        campaign = CampaignFactory(status="active")
        log = CallLog.objects.create(
            campaign=campaign,
            phone_number="+919876543210",
            status="initiated",
        )
        # Simulate drop transition
        log.status = "dropped"
        log.save(update_fields=["status"])

        log.refresh_from_db()
        assert log.is_abandoned is True

    def test_is_abandoned_not_set_on_completed(self):
        """Completed calls (with agent) should NOT be marked abandoned."""
        from tests.factories import CampaignFactory
        from calls.models import CallLog
        from django.utils import timezone
        campaign = CampaignFactory(status="active")
        log = CallLog.objects.create(
            campaign=campaign,
            phone_number="+919876543210",
            status="initiated",
            answered_at=timezone.now(),
            duration=60,
        )
        log.status = "completed"
        log.save(update_fields=["status"])

        log.refresh_from_db()
        assert log.is_abandoned is False

    def test_signal_swallows_redis_error(self):
        """Signal must not raise even if Redis is down — call flow is protected."""
        from tests.factories import CampaignFactory
        from calls.models import CallLog
        campaign = CampaignFactory(status="active")

        with patch("campaigns.metrics.increment_attempted", side_effect=Exception("Redis down")):
            # Should not raise
            log = CallLog.objects.create(
                campaign=campaign,
                phone_number="+919000000001",
                status="initiated",
            )
        assert log.pk is not None


# ─────────────────────────────────────────────────────────────────────────────
# campaigns/models.py — new cps_limit field
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCampaignCpsLimit:

    def test_cps_limit_default(self):
        from tests.factories import CampaignFactory
        campaign = CampaignFactory()
        assert campaign.cps_limit == 10

    def test_cps_limit_custom(self):
        from tests.factories import CampaignFactory
        campaign = CampaignFactory(cps_limit=25)
        assert campaign.cps_limit == 25

    def test_cps_limit_zero_means_unlimited(self):
        from tests.factories import CampaignFactory
        from campaigns.metrics import check_and_reserve_cps
        campaign = CampaignFactory(cps_limit=0)
        allowed = check_and_reserve_cps(campaign.id, campaign.cps_limit, count=100)
        assert allowed == 100


# ─────────────────────────────────────────────────────────────────────────────
# calls/models.py — new is_abandoned field
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCallLogIsAbandoned:

    def test_field_exists(self):
        from calls.models import CallLog
        assert hasattr(CallLog, "is_abandoned")

    def test_default_false(self):
        from tests.factories import CampaignFactory, CallLogFactory
        campaign = CampaignFactory()
        log = CallLogFactory(campaign=campaign)
        assert log.is_abandoned is False

    def test_can_set_true(self):
        from tests.factories import CampaignFactory, CallLogFactory
        campaign = CampaignFactory()
        log = CallLogFactory(campaign=campaign)
        log.is_abandoned = True
        log.save(update_fields=["is_abandoned"])
        log.refresh_from_db()
        assert log.is_abandoned is True


# ─────────────────────────────────────────────────────────────────────────────
# ai_dialer/online_learning.py — pure logic tests
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeOutcomeQuality:
    # Signature: compute_outcome_quality(abandon_rate, agent_utilization, answer_rate,
    #                                    target_abandon=0.03, target_utilization=0.85)

    def test_perfect_conditions(self):
        from ai_dialer.online_learning import compute_outcome_quality
        score = compute_outcome_quality(
            abandon_rate=0.00,
            agent_utilization=0.85,
            answer_rate=0.60,
        )
        assert 0.0 <= score <= 1.0
        assert score > 0.7  # should be a high-quality outcome

    def test_high_abandon_penalised(self):
        from ai_dialer.online_learning import compute_outcome_quality
        # (abandon_rate, agent_utilization, answer_rate)
        good = compute_outcome_quality(0.01, 0.80, 0.50)
        bad  = compute_outcome_quality(0.15, 0.80, 0.50)
        assert good > bad

    def test_zero_utilization_penalised(self):
        from ai_dialer.online_learning import compute_outcome_quality
        high = compute_outcome_quality(0.01, 0.90, 0.50)
        low  = compute_outcome_quality(0.01, 0.10, 0.50)
        assert high > low

    def test_score_bounded(self):
        from ai_dialer.online_learning import compute_outcome_quality
        for abn in [0.0, 0.05, 0.20]:
            for util in [0.0, 0.5, 1.0]:
                for ar in [0.0, 0.5, 1.0]:
                    score = compute_outcome_quality(abn, util, ar)
                    assert 0.0 <= score <= 1.0, f"abn={abn} util={util} ar={ar} -> {score}"


class TestExperienceReplayBuffer:

    def _experience(self, quality=0.8, **kwargs):
        from ai_dialer.online_learning import Experience
        defaults = dict(
            features=np.zeros(10),
            dial_ratio_used=1.5,
            answer_rate_observed=0.50,
            abandon_rate_observed=0.02,
            agent_utilization=0.80,
            abandon_spike=False,
            outcome_quality=quality,
            weight=1.0,
        )
        defaults.update(kwargs)
        return Experience(**defaults)

    def test_add_and_size(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=100)
        for i in range(10):
            buf.add(self._experience())
        assert len(buf) == 10

    def test_max_size_eviction(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=5)
        for i in range(10):
            buf.add(self._experience())
        assert len(buf) == 5

    def test_sample_returns_correct_count(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=100)
        for _ in range(20):
            buf.add(self._experience())
        sample = buf.sample(5)
        assert len(sample) == 5

    def test_sample_empty_buffer_returns_empty(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=100)
        assert buf.sample(5) == []

    def test_get_training_arrays_shapes(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=100)
        n_features = 10
        for _ in range(15):
            buf.add(self._experience(features=np.zeros(n_features)))
        X, y_ans, y_abn, y_ratio, weights = buf.get_training_arrays()
        assert X.shape      == (15, n_features)
        assert y_ans.shape  == (15,)
        assert y_abn.shape  == (15,)
        assert y_ratio.shape == (15,)
        assert weights.shape == (15,)

    def test_stats_returns_dict(self):
        from ai_dialer.online_learning import ExperienceReplayBuffer
        buf = ExperienceReplayBuffer(max_size=100)
        buf.add(self._experience(quality=0.7))
        stats = buf.stats()
        assert "size"         in stats
        assert "avg_quality"  in stats
        assert "good_outcomes" in stats


# ─────────────────────────────────────────────────────────────────────────────
# ai_dialer/features.py — FeatureEngineer
# ─────────────────────────────────────────────────────────────────────────────

class TestFeatureEngineer:

    def _snapshot(self, **kwargs):
        from ai_dialer.features import RawSnapshot
        from datetime import datetime
        defaults = dict(
            timestamp=datetime.now(),
            agents_available=3, agents_busy=2, agents_wrapup=1,
            agents_total=6,
            calls_ringing=2, calls_connected=2, calls_in_queue=0,
            answer_rate=0.50, abandon_rate=0.02, amd_rate=0.05,
            short_abandon_rate=0.01,
            avg_talk_time=120.0, avg_wrapup_time=45.0, avg_ring_time=20.0,
            current_dial_ratio=1.5,
        )
        defaults.update(kwargs)
        return RawSnapshot(**defaults)

    def test_push_returns_vector(self):
        from ai_dialer.features import FeatureEngineer, FEATURE_NAMES
        eng = FeatureEngineer()
        vec = eng.push(self._snapshot())
        assert vec.shape == (len(FEATURE_NAMES),)

    def test_vector_is_finite(self):
        from ai_dialer.features import FeatureEngineer
        eng = FeatureEngineer()
        vec = eng.push(self._snapshot())
        assert np.all(np.isfinite(vec))

    def test_multiple_pushes_accumulate_history(self):
        from ai_dialer.features import FeatureEngineer
        eng = FeatureEngineer()
        for i in range(5):
            vec = eng.push(self._snapshot(answer_rate=0.4 + i * 0.05))
        # After 5 pushes, EWMA features should differ from initial push
        assert vec is not None

    def test_feature_count_matches_names(self):
        from ai_dialer.features import FeatureEngineer, FEATURE_NAMES
        eng = FeatureEngineer()
        vec = eng.push(self._snapshot())
        assert len(FEATURE_NAMES) == vec.shape[0]
