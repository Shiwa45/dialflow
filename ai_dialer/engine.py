"""
ai_dialer/engine.py

Main AI Predictive Dialer Engine.
Combines all XGBoost models into unified decision system.
"""

import math
import numpy as np
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from django.utils import timezone

from .features import FeatureEngineer, RawSnapshot, FEATURE_NAMES
from .models import (
    AnswerRatePredictor,
    AbandonRiskClassifier,
    OptimalRatioRegressor,
)
from .online_learning import (
    ExperienceReplayBuffer,
    Experience,
    compute_outcome_quality,
)

logger = logging.getLogger(__name__)


@dataclass
class AIDialerConfig:
    """Complete AI dialer configuration"""
    # Regulatory
    target_abandon_rate: float = 0.02    # 2% internal target
    max_abandon_rate: float = 0.03       # 3% FCC hard limit

    # Ratio bounds
    min_dial_ratio: float = 1.0
    max_dial_ratio: float = 4.0
    initial_dial_ratio: float = 1.5

    # AI model settings
    model_confidence_threshold: float = 0.6  # Min confidence to use AI
    abandon_risk_threshold: float = 0.4      # Risk prob → reduce ratio
    uncertainty_penalty: float = 0.5         # Reduce ratio if uncertain

    # Online learning
    retrain_interval_minutes: int = 30       # Retrain every 30 min
    min_samples_to_retrain: int = 500        # Need 500+ samples
    experience_buffer_size: int = 50_000

    # Fallback (Erlang-C + PID from v2)
    use_fallback_when_cold: bool = True
    cold_start_samples: int = 100            # Use AI after 100 experiences

    # Safety
    max_burst_per_tick: int = 10
    adaptive_pacing: bool = True
    metrics_cache_ttl: int = 3              # seconds


@dataclass
class AIDecision:
    """Complete AI dialing decision with full explainability"""
    # Core output
    calls_to_dial: int
    dial_ratio: float

    # Model predictions
    predicted_answer_rate: float
    abandon_risk_probability: float
    is_abandon_risk: bool
    model_confidence: float

    # Explainability
    ratio_source: str          # "xgboost" | "fallback_erlang" | "safety_clamp"
    top_features: Dict[str, float]  # SHAP or importance-weighted features
    explanation: str

    # Outcome tracking (filled in later)
    outcome_id: Optional[str] = None

    # Status
    health: str = "good"
    warnings: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


class AIDialer:
    """
    XGBoost-powered Predictive Dialer Engine.

    Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │  Every Tick (1 second):                                 │
    │                                                         │
    │  1. Fetch raw metrics from DB                           │
    │  2. Engineer features (EWMA, lags, Erlang-C)            │
    │  3. Run 3 XGBoost models in parallel                    │
    │  4. Blend predictions with confidence weighting         │
    │  5. Apply safety constraints                            │
    │  6. Store experience for online learning                │
    │  7. Return AIDecision                                   │
    │                                                         │
    │  Every 30 minutes:                                      │
    │  8. Retrain models on experience buffer                 │
    └─────────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        campaign_id: int,
        config: Optional[AIDialerConfig] = None,
    ):
        self.campaign_id = campaign_id
        self.config = config or AIDialerConfig()

        # XGBoost models
        self.answer_model   = AnswerRatePredictor()
        self.abandon_model  = AbandonRiskClassifier(
            risk_threshold=self.config.target_abandon_rate
        )
        self.ratio_model    = OptimalRatioRegressor(
            ratio_min=self.config.min_dial_ratio,
            ratio_max=self.config.max_dial_ratio,
        )

        # Feature engineering
        self.feature_engineer = FeatureEngineer(history_size=200)

        # Experience replay for online learning
        self.replay_buffer = ExperienceReplayBuffer(
            max_size=self.config.experience_buffer_size
        )

        # State tracking
        self._current_ratio: float = self.config.initial_dial_ratio
        self._last_features: Optional[np.ndarray] = None
        self._last_decision: Optional[AIDecision] = None
        self._experience_count: int = 0
        self._last_retrain: float = time.time()
        self._lock = threading.RLock()

        # Metric cache
        self._raw_cache: Optional[RawSnapshot] = None
        self._cache_expiry: float = 0.0

        # Throttle cold-start log spam: warn once per 60 ticks (~1 min at 1 Hz)
        self._cold_start_tick: int = 0

        # Load pretrained models if available
        self._load_models()

        logger.info(
            f"AIDialer initialized for campaign {campaign_id} | "
            f"models_loaded={self._models_loaded()}"
        )

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def decide(self) -> AIDecision:
        """
        Main decision method. Call every second.

        Returns complete AI decision with full explainability.
        """
        with self._lock:
            try:
                return self._decide_internal()
            except Exception as e:
                logger.error(
                    f"AIDialer decision error campaign {self.campaign_id}: {e}",
                    exc_info=True,
                )
                return self._safe_fallback_decision()

    def record_outcome(
        self,
        answer_rate: float,
        abandon_rate: float,
        agent_utilization: float,
    ):
        """
        Record actual outcome after a tick.
        Called ~30s after decide() to capture what actually happened.

        This is the feedback loop that makes AI improve over time.
        """
        with self._lock:
            if self._last_features is None:
                return

            abandon_spike = abandon_rate > self.config.max_abandon_rate
            quality = compute_outcome_quality(
                abandon_rate=abandon_rate,
                agent_utilization=agent_utilization,
                answer_rate=answer_rate,
                target_abandon=self.config.target_abandon_rate,
                target_utilization=0.85,
            )

            # Weight recent experiences higher
            weight = 1.0 + quality  # 1.0-2.0 range

            exp = Experience(
                features=self._last_features.copy(),
                dial_ratio_used=self._current_ratio,
                answer_rate_observed=answer_rate,
                abandon_rate_observed=abandon_rate,
                agent_utilization=agent_utilization,
                abandon_spike=abandon_spike,
                outcome_quality=quality,
                campaign_id=self.campaign_id,
                weight=weight,
            )

            self.replay_buffer.add(exp)
            self._experience_count += 1

            # Check if we should retrain
            self._maybe_retrain()

            logger.debug(
                f"Campaign {self.campaign_id} outcome recorded: "
                f"answer={answer_rate:.2%} "
                f"abandon={abandon_rate:.2%} "
                f"util={agent_utilization:.2%} "
                f"quality={quality:.2f} "
                f"buffer_size={len(self.replay_buffer)}"
            )

    def get_status(self) -> Dict:
        """Full status for monitoring"""
        with self._lock:
            d = self._last_decision
            buf_stats = self.replay_buffer.stats()

            return {
                "campaign_id": self.campaign_id,
                "timestamp": timezone.now().isoformat(),
                "ai_active": self._is_ai_active(),
                "experience_count": self._experience_count,
                "buffer": buf_stats,
                "models": {
                    "answer_rate": self.answer_model._is_trained,
                    "abandon_risk": self.abandon_model._is_trained,
                    "optimal_ratio": self.ratio_model._is_trained,
                },
                "last_decision": {
                    "calls_to_dial": d.calls_to_dial if d else 0,
                    "dial_ratio": d.dial_ratio if d else 0,
                    "predicted_answer_rate": d.predicted_answer_rate if d else 0,
                    "abandon_risk": d.abandon_risk_probability if d else 0,
                    "confidence": d.model_confidence if d else 0,
                    "ratio_source": d.ratio_source if d else "none",
                    "explanation": d.explanation if d else "",
                    "top_features": d.top_features if d else {},
                    "warnings": d.warnings if d else [],
                } if d else {},
                "config": {
                    "target_abandon_pct": self.config.target_abandon_rate * 100,
                    "max_abandon_pct": self.config.max_abandon_rate * 100,
                    "min_ratio": self.config.min_dial_ratio,
                    "max_ratio": self.config.max_dial_ratio,
                    "retrain_interval_min": self.config.retrain_interval_minutes,
                },
            }

    def retrain(self, force: bool = False) -> Optional[Dict]:
        """
        Manually trigger model retraining.

        Args:
            force: Train even with insufficient data

        Returns:
            dict: Training metrics or None if skipped
        """
        with self._lock:
            return self._retrain_models(force=force)

    # ----------------------------------------------------------------
    # Internal Decision Logic
    # ----------------------------------------------------------------

    def _decide_internal(self) -> AIDecision:
        """Core decision logic"""
        # 1. Get raw metrics
        raw = self._get_raw_snapshot()
        if raw is None:
            return self._safe_fallback_decision()

        # 2. Engineer features
        features = self.feature_engineer.push(raw)
        self._last_features = features

        # 3. Assess warnings and health
        warnings = self._check_warnings(raw)
        health = self._assess_health(raw)

        # Emergency pause: no agents
        if raw.agents_total == 0:
            return self._zero_decision(
                "No agents logged in", warnings, health
            )

        # 4. Run AI models (or fallback if cold)
        if not self._is_ai_active():
            return self._erlang_fallback_decision(raw, features, warnings, health)

        return self._ai_decision(raw, features, warnings, health)

    def _ai_decision(
        self,
        raw: RawSnapshot,
        features: np.ndarray,
        warnings: List[str],
        health: str,
    ) -> AIDecision:
        """
        Full AI-powered decision using all 3 XGBoost models.
        """
        # ── Model 1: Predict answer rate ─────────────────────────
        predicted_answer_rate = self.answer_model.predict(features)

        # ── Model 2: Predict abandon risk ─────────────────────────
        abandon_prob, is_abandon_risk = self.abandon_model.predict_risk(features)

        # ── Model 3: Predict optimal ratio ────────────────────────
        ai_ratio, ratio_uncertainty = self.ratio_model.predict(
            features, uncertainty=True
        )

        # ── Confidence calculation ─────────────────────────────────
        data_confidence = min(1.0, self._experience_count / 1000)
        uncertainty_confidence = max(0.0, 1.0 - ratio_uncertainty)
        model_confidence = (data_confidence + uncertainty_confidence) / 2

        # ── Blend AI ratio with constraints ───────────────────────
        ratio_source = "xgboost"
        final_ratio = ai_ratio

        # If abandon risk is high → reduce ratio aggressively
        if is_abandon_risk:
            risk_reduction = 0.5 + (1 - abandon_prob) * 0.5  # 0.5-1.0
            final_ratio = final_ratio * risk_reduction
            ratio_source = "xgboost_risk_reduced"
            warnings.append(
                f"Abandon risk {abandon_prob:.0%} detected - "
                f"ratio reduced by {(1-risk_reduction):.0%}"
            )

        # If uncertainty is high → be conservative
        if ratio_uncertainty > 0.5:
            uncertainty_reduction = 1.0 - (
                ratio_uncertainty * self.config.uncertainty_penalty
            )
            final_ratio = final_ratio * uncertainty_reduction
            if ratio_source == "xgboost":
                ratio_source = "xgboost_uncertainty_adjusted"

        # If confidence below threshold → blend with conservative value
        if model_confidence < self.config.model_confidence_threshold:
            conservative = self.config.initial_dial_ratio
            blend = model_confidence / self.config.model_confidence_threshold
            final_ratio = blend * final_ratio + (1 - blend) * conservative
            ratio_source = f"xgboost_blended_{blend:.2f}"

        # Hard safety clamps
        final_ratio = float(np.clip(
            final_ratio,
            self.config.min_dial_ratio,
            self.config.max_dial_ratio,
        ))

        # Emergency override: current abandon rate critical
        if raw.abandon_rate > self.config.max_abandon_rate:
            final_ratio = self.config.min_dial_ratio
            ratio_source = "safety_clamp_abandon"
            warnings.append("CRITICAL: abandon rate at limit - minimum ratio")
            health = "critical"

        self._current_ratio = final_ratio

        # ── Calculate calls to dial ────────────────────────────────
        calls_to_dial = self._ratio_to_calls(raw, final_ratio)

        # ── Build explainability ───────────────────────────────────
        top_features = self._get_top_features_for_decision(features)

        explanation = (
            f"XGBoost predicts answer_rate={predicted_answer_rate:.1%} "
            f"abandon_risk={abandon_prob:.0%} "
            f"optimal_ratio={ai_ratio:.2f}±{ratio_uncertainty:.2f} "
            f"confidence={model_confidence:.0%} "
            f"→ final_ratio={final_ratio:.2f}"
        )

        decision = AIDecision(
            calls_to_dial=calls_to_dial,
            dial_ratio=round(final_ratio, 3),
            predicted_answer_rate=round(predicted_answer_rate, 4),
            abandon_risk_probability=round(abandon_prob, 4),
            is_abandon_risk=is_abandon_risk,
            model_confidence=round(model_confidence, 3),
            ratio_source=ratio_source,
            top_features=top_features,
            explanation=explanation,
            health=health,
            warnings=warnings,
        )

        self._last_decision = decision
        self._log_decision(decision, raw)
        return decision

    def _erlang_fallback_decision(
        self,
        raw: RawSnapshot,
        features: np.ndarray,
        warnings: List[str],
        health: str,
    ) -> AIDecision:
        """
        Fallback to Erlang-C when AI models aren't ready.
        Uses erlang_optimal_ratio from feature set.
        """
        self._cold_start_tick += 1
        cold_msg = (
            f"AI cold start ({self._experience_count}/"
            f"{self.config.cold_start_samples} samples) - "
            "using Erlang-C fallback"
        )
        # Include in decision.warnings only on the ticks we actually want to log it
        # (tick 1 = first occurrence, then every 60 ticks ≈ once per minute at 1 Hz).
        if self._cold_start_tick == 1 or self._cold_start_tick % 60 == 0:
            warnings.append(cold_msg)
        # else: suppress — the caller (_predictive_decision) will log decision.warnings

        ratio = float(np.clip(
            raw.erlang_optimal_ratio,
            self.config.min_dial_ratio,
            self.config.max_dial_ratio,
        ))

        # Still run abandon risk if that model is trained
        if self.abandon_model._is_trained:
            abandon_prob, is_abandon_risk = self.abandon_model.predict_risk(features)
            if is_abandon_risk:
                ratio = max(self.config.min_dial_ratio, ratio * 0.7)
        else:
            abandon_prob, is_abandon_risk = 0.0, False

        self._current_ratio = ratio
        calls_to_dial = self._ratio_to_calls(raw, ratio)

        decision = AIDecision(
            calls_to_dial=calls_to_dial,
            dial_ratio=round(ratio, 3),
            predicted_answer_rate=raw.answer_rate,
            abandon_risk_probability=abandon_prob,
            is_abandon_risk=is_abandon_risk,
            model_confidence=0.0,
            ratio_source="erlang_fallback",
            top_features={},
            explanation=(
                f"Erlang-C fallback: ratio={ratio:.2f} "
                f"(collecting data: {self._experience_count}/"
                f"{self.config.cold_start_samples})"
            ),
            health=health,
            warnings=warnings,
        )

        self._last_decision = decision
        return decision

    # ----------------------------------------------------------------
    # Raw Snapshot — collects live metrics from project DB/models
    # ----------------------------------------------------------------

    def _get_raw_snapshot(self) -> Optional[RawSnapshot]:
        """
        Collect raw system metrics from the project's DB for feature engineering.
        Uses AgentStatus, CallLog, CampaignAgent, Campaign, and hopper utilities.
        Results are cached for metrics_cache_ttl seconds to avoid hammering DB.
        """
        now_ts = time.time()
        if self._raw_cache is not None and now_ts < self._cache_expiry:
            return self._raw_cache

        try:
            from django.db.models import Avg, Count, Q

            from agents.models import AgentStatus
            from calls.models import CallLog
            from campaigns.hopper import get_hopper_stats
            from campaigns.models import Campaign, CampaignAgent
            from campaigns.predictive import _erlang_c

            # ── Campaign ─────────────────────────────────────────
            try:
                campaign = Campaign.objects.get(id=self.campaign_id)
            except Campaign.DoesNotExist:
                return None

            now = timezone.now()

            # ── Agent counts ──────────────────────────────────────
            assigned_ids = list(
                CampaignAgent.objects.filter(
                    campaign_id=self.campaign_id, is_active=True
                ).values_list("agent_id", flat=True)
            )

            agent_agg = AgentStatus.objects.filter(
                user_id__in=assigned_ids
            ).aggregate(
                available=Count("id", filter=Q(status="ready")),
                busy=Count("id", filter=Q(status="on_call")),
                wrapup=Count("id", filter=Q(status="wrapup")),
                total=Count("id", filter=~Q(status="offline")),
            )

            agents_available = agent_agg["available"] or 0
            agents_busy      = agent_agg["busy"] or 0
            agents_wrapup    = agent_agg["wrapup"] or 0
            agents_total     = agent_agg["total"] or 0

            # ── Call state ────────────────────────────────────────
            hopper = get_hopper_stats(self.campaign_id)
            calls_ringing   = hopper.get("in_flight", 0)  # placed but unanswered
            calls_connected = agents_busy                  # live bridged calls
            calls_in_queue  = hopper.get("queued", 0)

            # ── Rate & timing (last 200 calls) ────────────────────
            recent = CallLog.objects.filter(
                campaign_id=self.campaign_id
            ).order_by("-started_at")[:200]
            total_recent = recent.count()

            if total_recent >= 5:
                agg = recent.aggregate(
                    avg_talk=Avg("duration", filter=Q(status="completed")),
                    avg_ring=Avg("ring_duration"),
                    answered=Count("id", filter=Q(status="completed")),
                    dropped=Count("id", filter=Q(status="dropped")),
                    amd_machine=Count("id", filter=Q(amd_result__icontains="machine")),
                )
                answered_cnt = agg["answered"] or 0
                dropped_cnt  = agg["dropped"] or 0
                amd_cnt      = agg["amd_machine"] or 0

                answer_rate   = answered_cnt / total_recent
                abandon_rate  = dropped_cnt / max(answered_cnt, 1)
                amd_rate      = amd_cnt / total_recent
                avg_talk_time = float(agg["avg_talk"] or 120.0)
                avg_ring_time = float(agg["avg_ring"] or 15.0)
            else:
                answer_rate   = 0.30
                abandon_rate  = 0.0
                amd_rate      = 0.0
                avg_talk_time = 120.0
                avg_ring_time = 15.0

            # Use campaign wrapup setting as average wrapup time
            avg_wrapup_time = float(campaign.auto_wrapup_timeout or 45)

            # ── Short abandon rate (drops < 2s, last 30 min) ──────
            win30 = now - timedelta(minutes=30)
            recent_30_total = CallLog.objects.filter(
                campaign_id=self.campaign_id,
                started_at__gte=win30,
            ).count()
            short_abandons = CallLog.objects.filter(
                campaign_id=self.campaign_id,
                status="dropped",
                duration__lt=2,
                started_at__gte=win30,
            ).count()
            short_abandon_rate = short_abandons / max(recent_30_total, 1)

            # ── Rolling window stats ──────────────────────────────
            def _window(minutes):
                since = now - timedelta(minutes=minutes)
                qs    = CallLog.objects.filter(
                    campaign_id=self.campaign_id,
                    started_at__gte=since,
                )
                tot = qs.count()
                ans = qs.filter(status="completed").count()
                drp = qs.filter(status="dropped").count()
                ar  = ans / max(tot, 1)
                abr = drp / max(ans, 1) if ans > 0 else 0.0
                return tot, ar, abr

            calls_5,  ar_5,  abr_5  = _window(5)
            calls_15, ar_15, abr_15 = _window(15)
            calls_30, _,     _      = _window(30)

            # ── Time since last answered call ─────────────────────
            last_call = (
                CallLog.objects.filter(
                    campaign_id=self.campaign_id,
                    status="completed",
                    answered_at__isnull=False,
                )
                .order_by("-answered_at")
                .first()
            )
            time_since_last = (
                (now - last_call.answered_at).total_seconds()
                if last_call else 0.0
            )

            # ── Campaign context ──────────────────────────────────
            leads_remaining = calls_in_queue
            leads_total     = max(leads_remaining, 1)

            # ── Erlang-C physics features ─────────────────────────
            call_rate         = calls_5 / 300.0  # calls/sec over 5 min window
            mu                = 1.0 / max(avg_talk_time, 1.0)
            n_agents          = max(agents_total, 1)
            traffic_intensity = min(
                call_rate / max(mu, 1e-9),
                float(n_agents) - 0.01,
            )
            erlang_util = min(traffic_intensity / n_agents, 0.99)

            ec     = _erlang_c(n_agents, traffic_intensity)
            denom  = max(n_agents * mu - traffic_intensity, 1e-9)
            erlang_wait = min(ec / denom, 300.0)
            erlang_sl   = max(0.0, min(1.0, 1.0 - ec * math.exp(-denom * 20.0)))
            erlang_ratio = float(np.clip(
                (traffic_intensity / n_agents) / max(answer_rate, 0.01),
                float(campaign.min_dial_ratio),
                float(campaign.max_dial_ratio),
            ))

            snapshot = RawSnapshot(
                timestamp              = now,
                agents_available       = agents_available,
                agents_busy            = agents_busy,
                agents_wrapup          = agents_wrapup,
                agents_total           = agents_total,
                calls_ringing          = calls_ringing,
                calls_connected        = calls_connected,
                calls_in_queue         = calls_in_queue,
                answer_rate            = answer_rate,
                abandon_rate           = abandon_rate,
                amd_rate               = amd_rate,
                short_abandon_rate     = short_abandon_rate,
                avg_talk_time          = avg_talk_time,
                avg_wrapup_time        = avg_wrapup_time,
                avg_ring_time          = avg_ring_time,
                calls_last_5min        = calls_5,
                calls_last_15min       = calls_15,
                calls_last_30min       = calls_30,
                answer_rate_5min       = ar_5,
                answer_rate_15min      = ar_15,
                abandon_rate_5min      = abr_5,
                abandon_rate_15min     = abr_15,
                avg_wait_5min          = 0.0,
                dial_ratio_5min        = self._current_ratio,
                dial_ratio_15min       = self._current_ratio,
                leads_remaining        = leads_remaining,
                leads_total            = leads_total,
                campaign_start_time    = None,
                time_since_last_answer = min(time_since_last, 300.0),
                historical_best_ratio  = float(campaign.dial_ratio or 1.5),
                erlang_traffic_intensity = traffic_intensity,
                erlang_utilization       = erlang_util,
                erlang_wait_estimate     = erlang_wait,
                erlang_service_level     = erlang_sl,
                erlang_optimal_ratio     = erlang_ratio,
                current_dial_ratio       = self._current_ratio,
            )

            self._raw_cache  = snapshot
            self._cache_expiry = now_ts + self.config.metrics_cache_ttl
            return snapshot

        except Exception as e:
            logger.error(
                f"Error collecting snapshot for campaign {self.campaign_id}: {e}",
                exc_info=True,
            )
            return None

    # ----------------------------------------------------------------
    # Training & Online Learning
    # ----------------------------------------------------------------

    def _maybe_retrain(self):
        """Check if retraining is due"""
        now = time.time()
        interval = self.config.retrain_interval_minutes * 60
        min_samples = self.config.min_samples_to_retrain

        if (now - self._last_retrain >= interval
                and len(self.replay_buffer) >= min_samples):
            # Run in background thread — don't block the dial tick
            t = threading.Thread(
                target=self._retrain_models,
                kwargs={"force": False},
                daemon=True,
            )
            t.start()

    def _retrain_models(self, force: bool = False) -> Optional[Dict]:
        """
        Retrain all XGBoost models from experience buffer.
        Called in background thread.
        """
        min_samples = self.config.min_samples_to_retrain
        buffer_size = len(self.replay_buffer)

        if not force and buffer_size < min_samples:
            logger.info(
                f"Skip retrain: {buffer_size}/{min_samples} samples"
            )
            return None

        logger.info(
            f"Campaign {self.campaign_id}: Retraining models "
            f"on {buffer_size} experiences..."
        )
        t0 = time.time()

        # Sample from buffer
        n_samples = min(buffer_size, 20_000)
        X, y_answer, y_abandon, y_ratio, weights = (
            self.replay_buffer.get_training_arrays(
                n=n_samples, strategy="prioritized"
            )
        )

        if len(X) < 50:
            return None

        # 80/20 train/val split
        split = int(len(X) * 0.8)
        X_train, X_val = X[:split], X[split:]
        w_train = weights[:split]

        results = {}

        # ── Train Model 1: Answer Rate ──────────────────────────
        try:
            metrics = self.answer_model.train(
                X_train, y_answer[:split],
                X_val,   y_answer[split:],
            )
            self.answer_model.save()
            results["answer_rate"] = metrics
        except Exception as e:
            logger.error(f"Answer rate model training failed: {e}")

        # ── Train Model 2: Abandon Risk ─────────────────────────
        try:
            metrics = self.abandon_model.train(
                X_train, y_abandon[:split],
                X_val,   y_abandon[split:],
            )
            # Calibrate on validation set
            self.abandon_model.calibrate(X_val, y_abandon[split:])
            self.abandon_model.save()
            results["abandon_risk"] = metrics
        except Exception as e:
            logger.error(f"Abandon risk model training failed: {e}")

        # ── Train Model 3: Optimal Ratio ────────────────────────
        # Only use experiences with known good outcomes
        good_mask = ~np.isnan(y_ratio)
        if good_mask.sum() >= 50:
            try:
                X_good = X[good_mask]
                y_good = y_ratio[good_mask]
                w_good = weights[good_mask]
                split_g = int(len(X_good) * 0.8)

                metrics = self.ratio_model.train(
                    X_good[:split_g], y_good[:split_g],
                    X_good[split_g:], y_good[split_g:],
                    sample_weights=w_good[:split_g],
                )
                self.ratio_model.save()
                results["optimal_ratio"] = metrics
            except Exception as e:
                logger.error(f"Optimal ratio model training failed: {e}")

        elapsed = time.time() - t0
        self._last_retrain = time.time()

        logger.info(
            f"Campaign {self.campaign_id}: Retrain complete "
            f"in {elapsed:.1f}s | results={results}"
        )

        return results

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _is_ai_active(self) -> bool:
        """Check if AI models are ready for production use"""
        if not self.config.use_fallback_when_cold:
            return True
        return (
            self._experience_count >= self.config.cold_start_samples
            and self.ratio_model._is_trained
        )

    def _ratio_to_calls(self, raw: RawSnapshot, ratio: float) -> int:
        """Convert dial ratio to concrete call count.

        ``calls_ringing`` = calls already originated but not yet answered (in-flight).
        ``calls_in_queue`` = leads sitting in the Redis hopper waiting to be dialled —
            NOT active calls.  Including them in ``current`` would make target − current
            negative whenever the hopper has any leads, permanently zeroing to_dial.
        """
        effective_agents = raw.agents_available + raw.agents_wrapup * 0.5

        if effective_agents <= 0:
            return 0

        target  = effective_agents * ratio
        current = raw.calls_ringing   # only calls actually placed and in-flight
        needed  = max(0, int(math.ceil(target - current)))

        if self.config.adaptive_pacing:
            max_burst = max(2, int(raw.agents_available * 2))
            needed = min(needed, max_burst)

        return min(needed, self.config.max_burst_per_tick)

    def _check_warnings(self, raw: RawSnapshot) -> List[str]:
        """Generate system warnings"""
        warnings = []
        if raw.abandon_rate > self.config.max_abandon_rate:
            warnings.append(
                f"CRITICAL abandon rate {raw.abandon_rate:.1%} "
                f">= limit {self.config.max_abandon_rate:.1%}"
            )
        elif raw.abandon_rate > self.config.target_abandon_rate:
            warnings.append(
                f"Abandon {raw.abandon_rate:.1%} above target "
                f"{self.config.target_abandon_rate:.1%}"
            )
        if raw.answer_rate < 0.10:
            warnings.append(f"Very low answer rate {raw.answer_rate:.1%}")
        if raw.agents_available == 0 and raw.agents_wrapup == 0:
            warnings.append("No available or wrapping agents")
        return warnings

    def _assess_health(self, raw: RawSnapshot) -> str:
        if raw.agents_total == 0:
            return "paused"
        if raw.abandon_rate > self.config.max_abandon_rate:
            return "critical"
        if raw.abandon_rate > self.config.target_abandon_rate:
            return "warning"
        return "good"

    def _get_top_features_for_decision(
        self, features: np.ndarray, n: int = 5
    ) -> Dict[str, float]:
        """
        Get most influential features for this specific decision.
        Uses ratio model's feature importance × current feature values.
        """
        if not self.ratio_model._feature_importance:
            return {}

        importance = self.ratio_model._feature_importance
        top_names = list(importance.keys())[:n]

        return {
            name: float(features[FEATURE_NAMES.index(name)])
            for name in top_names
            if name in FEATURE_NAMES
        }

    def _load_models(self):
        """Load pretrained models from disk"""
        self.answer_model.load()
        self.abandon_model.load()
        self.ratio_model.load()

    def _models_loaded(self) -> bool:
        return (
            self.answer_model._is_trained
            and self.abandon_model._is_trained
            and self.ratio_model._is_trained
        )

    def _safe_fallback_decision(self) -> AIDecision:
        """Ultra-conservative fallback on error"""
        return AIDecision(
            calls_to_dial=0,
            dial_ratio=self.config.min_dial_ratio,
            predicted_answer_rate=0.0,
            abandon_risk_probability=0.0,
            is_abandon_risk=False,
            model_confidence=0.0,
            ratio_source="error_fallback",
            top_features={},
            explanation="Error in decision engine - holding",
            health="error",
            warnings=["Decision engine error - check logs"],
        )

    def _zero_decision(
        self, reason: str, warnings: List[str], health: str
    ) -> AIDecision:
        return AIDecision(
            calls_to_dial=0,
            dial_ratio=0.0,
            predicted_answer_rate=0.0,
            abandon_risk_probability=0.0,
            is_abandon_risk=False,
            model_confidence=1.0,
            ratio_source="paused",
            top_features={},
            explanation=reason,
            health=health,
            warnings=warnings,
        )

    def _log_decision(self, decision: AIDecision, raw: RawSnapshot):
        logger.info(
            "AI decision | "
            "campaign=%(cid)s dial=%(dial)d ratio=%(ratio).3f "
            "source=%(src)s confidence=%(conf).2f "
            "pred_answer=%(pa).1%% abandon_risk=%(ar).0%% "
            "agents=%(av)d/%(bu)d/%(wu)d",
            {
                "cid":  self.campaign_id,
                "dial": decision.calls_to_dial,
                "ratio": decision.dial_ratio,
                "src":  decision.ratio_source,
                "conf": decision.model_confidence,
                "pa":   decision.predicted_answer_rate,
                "ar":   decision.abandon_risk_probability,
                "av":   raw.agents_available,
                "bu":   raw.agents_busy,
                "wu":   raw.agents_wrapup,
            }
        )


# ============================================================================
# AIDialerManager — per-process singleton that manages per-campaign dialers
# ============================================================================

class AIDialerManager:
    """
    Manages AIDialer instances for all active campaigns.
    Thread-safe process-level singleton.
    Instantiates dialers on demand and caches them.
    """

    def __init__(self):
        self._dialers: Dict[int, AIDialer] = {}
        self._lock = threading.Lock()

    def get_dialer(self, campaign_id: int) -> AIDialer:
        """Get (or create) the AIDialer for a campaign."""
        with self._lock:
            if campaign_id not in self._dialers:
                config = self._build_config(campaign_id)
                self._dialers[campaign_id] = AIDialer(campaign_id, config)
            return self._dialers[campaign_id]

    def remove_dialer(self, campaign_id: int):
        """Remove dialer when campaign stops (frees memory)."""
        with self._lock:
            self._dialers.pop(campaign_id, None)

    def get_all_status(self) -> List[Dict]:
        """Return status dicts for all managed dialers."""
        from campaigns.models import Campaign

        names = dict(
            Campaign.objects.filter(
                id__in=list(self._dialers.keys())
            ).values_list("id", "name")
        )
        statuses = []
        for cid, dialer in list(self._dialers.items()):
            try:
                status = dialer.get_status()
                status["campaign_name"] = names.get(cid, f"Campaign {cid}")
                statuses.append(status)
            except Exception as e:
                logger.error(f"Status error campaign {cid}: {e}")
        return statuses

    @staticmethod
    def _build_config(campaign_id: int) -> AIDialerConfig:
        """Build AIDialerConfig from campaign DB settings."""
        try:
            from campaigns.models import Campaign
            campaign = Campaign.objects.get(id=campaign_id)
            target_abn = float(campaign.abandon_rate) / 100.0
            return AIDialerConfig(
                target_abandon_rate  = target_abn,
                max_abandon_rate     = min(0.03, target_abn * 1.5),
                min_dial_ratio       = float(campaign.min_dial_ratio),
                max_dial_ratio       = float(campaign.max_dial_ratio),
                initial_dial_ratio   = float(campaign.dial_ratio),
            )
        except Exception:
            return AIDialerConfig()


# Module-level singleton — created once per worker process
_ai_manager: Optional[AIDialerManager] = None
_manager_lock = threading.Lock()


def get_ai_manager() -> AIDialerManager:
    """Return the process-level AIDialerManager singleton."""
    global _ai_manager
    if _ai_manager is None:
        with _manager_lock:
            if _ai_manager is None:
                _ai_manager = AIDialerManager()
    return _ai_manager
