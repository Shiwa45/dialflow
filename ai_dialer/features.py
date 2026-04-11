"""
ai_dialer/features.py

Feature engineering for XGBoost predictive dialer.
Builds rich feature vectors from raw dialer metrics.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Feature Groups
# ============================================================================

FEATURE_NAMES = [
    # ── Agent State Features ──────────────────────────────────────
    "agents_available",           # Current available agents
    "agents_busy",                # Agents on calls
    "agents_wrapup",              # Agents in wrap-up
    "agents_total",               # Total logged-in agents
    "agent_utilization",          # busy / total
    "agent_availability_ratio",   # available / total
    "wrapup_pressure",            # wrapup / total (agents about to be free)

    # ── Call State Features ───────────────────────────────────────
    "calls_ringing",              # Outbound calls placed, not answered
    "calls_connected",            # Live calls with agents
    "calls_in_queue",             # Waiting for agent
    "calls_per_agent",            # total_calls / agents_total
    "ring_pressure",              # calls_ringing / agents_available
    "queue_pressure",             # calls_in_queue / agents_available

    # ── Rate Features ─────────────────────────────────────────────
    "answer_rate",                # % of placed calls answered
    "abandon_rate",               # % of answered calls abandoned
    "amd_rate",                   # % detected as answering machine
    "human_answer_rate",          # answer_rate * (1 - amd_rate)
    "short_abandon_rate",         # Abandons < 2s (nuisance indicator)
    "connect_rate",               # calls connected / calls placed

    # ── Timing Features ──────────────────────────────────────────
    "avg_talk_time",              # Average talk duration (seconds)
    "avg_wrapup_time",            # Average wrap-up duration
    "avg_ring_time",              # Average time to answer
    "avg_handle_time",            # talk + wrapup
    "talk_time_variance",         # Variance in talk times
    "occupancy",                  # talk / (talk + wrapup)

    # ── EWMA Trend Features ───────────────────────────────────────
    "ewma_answer_rate_fast",      # Alpha=0.3 fast EWMA
    "ewma_answer_rate_slow",      # Alpha=0.1 slow EWMA
    "ewma_abandon_rate_fast",
    "ewma_abandon_rate_slow",
    "answer_rate_momentum",       # fast - slow (trend direction)
    "abandon_rate_momentum",

    # ── Time-of-Day Features ──────────────────────────────────────
    "hour_of_day",                # 0-23
    "minute_of_hour",             # 0-59
    "day_of_week",                # 0=Monday, 6=Sunday
    "is_morning",                 # 8-12
    "is_afternoon",               # 12-17
    "is_evening",                 # 17-21
    "is_monday",
    "is_friday",
    "time_sin",                   # Cyclical encoding sin(hour * 2π/24)
    "time_cos",                   # Cyclical encoding cos(hour * 2π/24)
    "dow_sin",                    # Cyclical day-of-week
    "dow_cos",

    # ── Rolling Window Features ───────────────────────────────────
    "calls_last_5min",            # Call volume last 5 minutes
    "calls_last_15min",           # Call volume last 15 minutes
    "calls_last_30min",           # Call volume last 30 minutes
    "answer_rate_5min",           # Answer rate last 5 minutes
    "answer_rate_15min",          # Answer rate last 15 minutes
    "abandon_rate_5min",
    "abandon_rate_15min",
    "avg_wait_5min",              # Average queue wait last 5 min
    "dial_ratio_5min",            # Recent dial ratio used
    "dial_ratio_15min",

    # ── Lag Features (recent history) ─────────────────────────────
    "answer_rate_lag_1",          # Answer rate 1 tick ago
    "answer_rate_lag_3",          # 3 ticks ago
    "answer_rate_lag_5",
    "abandon_rate_lag_1",
    "abandon_rate_lag_3",
    "dial_ratio_lag_1",
    "dial_ratio_lag_3",
    "agents_available_lag_1",

    # ── Erlang-C Features (physics-based) ─────────────────────────
    "erlang_traffic_intensity",   # A = λ/μ
    "erlang_utilization",         # A / c
    "erlang_wait_estimate",       # Expected wait time
    "erlang_service_level",       # P(wait < 20s)
    "erlang_optimal_ratio",       # Erlang-recommended ratio

    # ── Campaign Features ─────────────────────────────────────────
    "leads_remaining",            # Leads left in hopper
    "leads_depleted_pct",         # % of list called
    "campaign_age_hours",         # Hours since campaign started
    "time_since_last_answer",     # Seconds since last answered call
    "historical_best_ratio",      # Best ratio from past similar periods
]

NUM_FEATURES = len(FEATURE_NAMES)
FEATURE_INDEX = {name: i for i, name in enumerate(FEATURE_NAMES)}


@dataclass
class RawSnapshot:
    """
    Complete raw snapshot for feature engineering.
    Collected every tick from DB.
    """
    # Time
    timestamp: datetime

    # Agents
    agents_available: int
    agents_busy: int
    agents_wrapup: int
    agents_total: int

    # Calls
    calls_ringing: int
    calls_connected: int
    calls_in_queue: int

    # Rates (0.0 - 1.0)
    answer_rate: float
    abandon_rate: float
    amd_rate: float
    short_abandon_rate: float

    # Timing
    avg_talk_time: float
    avg_wrapup_time: float
    avg_ring_time: float
    talk_time_variance: float = 0.0

    # Rolling windows (pre-computed from DB)
    calls_last_5min: int = 0
    calls_last_15min: int = 0
    calls_last_30min: int = 0
    answer_rate_5min: float = 0.0
    answer_rate_15min: float = 0.0
    abandon_rate_5min: float = 0.0
    abandon_rate_15min: float = 0.0
    avg_wait_5min: float = 0.0
    dial_ratio_5min: float = 1.0
    dial_ratio_15min: float = 1.0

    # Campaign context
    leads_remaining: int = 0
    leads_total: int = 1
    campaign_start_time: Optional[datetime] = None
    time_since_last_answer: float = 0.0
    historical_best_ratio: float = 1.5

    # Erlang features (computed externally)
    erlang_traffic_intensity: float = 0.0
    erlang_utilization: float = 0.0
    erlang_wait_estimate: float = 0.0
    erlang_service_level: float = 1.0
    erlang_optimal_ratio: float = 1.5

    # Current dial ratio being used
    current_dial_ratio: float = 1.5


class FeatureEngineer:
    """
    Builds feature vectors from raw snapshots.

    Maintains rolling history for lag and EWMA features.
    All features normalized to reasonable ranges for XGBoost.
    """

    def __init__(self, history_size: int = 100):
        self.history_size = history_size
        # Ring buffer of recent snapshots
        self._history: List[RawSnapshot] = []

        # EWMA state
        self._ewma_answer_fast: float = 0.30   # α=0.3
        self._ewma_answer_slow: float = 0.30   # α=0.1
        self._ewma_abandon_fast: float = 0.0
        self._ewma_abandon_slow: float = 0.0

    def push(self, snapshot: RawSnapshot) -> np.ndarray:
        """
        Add new snapshot and build feature vector.

        Args:
            snapshot: Current system state

        Returns:
            np.ndarray: Feature vector of shape (NUM_FEATURES,)
        """
        # Update EWMA
        self._ewma_answer_fast = (
            0.3 * snapshot.answer_rate + 0.7 * self._ewma_answer_fast
        )
        self._ewma_answer_slow = (
            0.1 * snapshot.answer_rate + 0.9 * self._ewma_answer_slow
        )
        self._ewma_abandon_fast = (
            0.3 * snapshot.abandon_rate + 0.7 * self._ewma_abandon_fast
        )
        self._ewma_abandon_slow = (
            0.1 * snapshot.abandon_rate + 0.9 * self._ewma_abandon_slow
        )

        # Build feature vector
        features = self._build_features(snapshot)

        # Store in history
        self._history.append(snapshot)
        if len(self._history) > self.history_size:
            self._history.pop(0)

        return features

    def _build_features(self, s: RawSnapshot) -> np.ndarray:
        """Build complete feature vector"""
        f = np.zeros(NUM_FEATURES, dtype=np.float32)
        idx = FEATURE_INDEX

        # ── Agent Features ────────────────────────────────────────
        f[idx["agents_available"]]        = s.agents_available
        f[idx["agents_busy"]]             = s.agents_busy
        f[idx["agents_wrapup"]]           = s.agents_wrapup
        f[idx["agents_total"]]            = s.agents_total
        f[idx["agent_utilization"]]       = (
            s.agents_busy / s.agents_total if s.agents_total > 0 else 0
        )
        f[idx["agent_availability_ratio"]] = (
            s.agents_available / s.agents_total if s.agents_total > 0 else 0
        )
        f[idx["wrapup_pressure"]]         = (
            s.agents_wrapup / s.agents_total if s.agents_total > 0 else 0
        )

        # ── Call Features ─────────────────────────────────────────
        f[idx["calls_ringing"]]    = s.calls_ringing
        f[idx["calls_connected"]]  = s.calls_connected
        f[idx["calls_in_queue"]]   = s.calls_in_queue
        f[idx["calls_per_agent"]]  = (
            (s.calls_ringing + s.calls_connected)
            / s.agents_total if s.agents_total > 0 else 0
        )
        f[idx["ring_pressure"]]  = (
            s.calls_ringing / s.agents_available
            if s.agents_available > 0 else s.calls_ringing
        )
        f[idx["queue_pressure"]] = (
            s.calls_in_queue / s.agents_available
            if s.agents_available > 0 else s.calls_in_queue
        )

        # ── Rate Features ─────────────────────────────────────────
        f[idx["answer_rate"]]       = s.answer_rate
        f[idx["abandon_rate"]]      = s.abandon_rate
        f[idx["amd_rate"]]          = s.amd_rate
        f[idx["human_answer_rate"]] = (
            s.answer_rate * (1 - s.amd_rate)
        )
        f[idx["short_abandon_rate"]] = s.short_abandon_rate
        f[idx["connect_rate"]]       = (
            s.calls_connected
            / max(s.calls_ringing + s.calls_connected, 1)
        )

        # ── Timing Features ───────────────────────────────────────
        f[idx["avg_talk_time"]]    = s.avg_talk_time
        f[idx["avg_wrapup_time"]]  = s.avg_wrapup_time
        f[idx["avg_ring_time"]]    = s.avg_ring_time
        f[idx["avg_handle_time"]]  = s.avg_talk_time + s.avg_wrapup_time
        f[idx["talk_time_variance"]] = s.talk_time_variance
        f[idx["occupancy"]]        = (
            s.avg_talk_time / (s.avg_talk_time + s.avg_wrapup_time)
            if (s.avg_talk_time + s.avg_wrapup_time) > 0 else 0
        )

        # ── EWMA Features ─────────────────────────────────────────
        f[idx["ewma_answer_rate_fast"]] = self._ewma_answer_fast
        f[idx["ewma_answer_rate_slow"]] = self._ewma_answer_slow
        f[idx["ewma_abandon_rate_fast"]] = self._ewma_abandon_fast
        f[idx["ewma_abandon_rate_slow"]] = self._ewma_abandon_slow
        f[idx["answer_rate_momentum"]] = (
            self._ewma_answer_fast - self._ewma_answer_slow
        )
        f[idx["abandon_rate_momentum"]] = (
            self._ewma_abandon_fast - self._ewma_abandon_slow
        )

        # ── Time Features ─────────────────────────────────────────
        t = s.timestamp
        hour = t.hour
        minute = t.minute
        dow = t.weekday()

        f[idx["hour_of_day"]]    = hour
        f[idx["minute_of_hour"]] = minute
        f[idx["day_of_week"]]    = dow
        f[idx["is_morning"]]     = 1.0 if 8 <= hour < 12 else 0.0
        f[idx["is_afternoon"]]   = 1.0 if 12 <= hour < 17 else 0.0
        f[idx["is_evening"]]     = 1.0 if 17 <= hour < 21 else 0.0
        f[idx["is_monday"]]      = 1.0 if dow == 0 else 0.0
        f[idx["is_friday"]]      = 1.0 if dow == 4 else 0.0

        # Cyclical time encoding (captures 23→0 continuity)
        f[idx["time_sin"]] = np.sin(hour * 2 * np.pi / 24)
        f[idx["time_cos"]] = np.cos(hour * 2 * np.pi / 24)
        f[idx["dow_sin"]]  = np.sin(dow * 2 * np.pi / 7)
        f[idx["dow_cos"]]  = np.cos(dow * 2 * np.pi / 7)

        # ── Rolling Window Features ───────────────────────────────
        f[idx["calls_last_5min"]]    = s.calls_last_5min
        f[idx["calls_last_15min"]]   = s.calls_last_15min
        f[idx["calls_last_30min"]]   = s.calls_last_30min
        f[idx["answer_rate_5min"]]   = s.answer_rate_5min
        f[idx["answer_rate_15min"]]  = s.answer_rate_15min
        f[idx["abandon_rate_5min"]]  = s.abandon_rate_5min
        f[idx["abandon_rate_15min"]] = s.abandon_rate_15min
        f[idx["avg_wait_5min"]]      = s.avg_wait_5min
        f[idx["dial_ratio_5min"]]    = s.dial_ratio_5min
        f[idx["dial_ratio_15min"]]   = s.dial_ratio_15min

        # ── Lag Features ──────────────────────────────────────────
        lag1 = self._history[-1] if len(self._history) >= 1 else None
        lag3 = self._history[-3] if len(self._history) >= 3 else None
        lag5 = self._history[-5] if len(self._history) >= 5 else None

        f[idx["answer_rate_lag_1"]] = lag1.answer_rate if lag1 else s.answer_rate
        f[idx["answer_rate_lag_3"]] = lag3.answer_rate if lag3 else s.answer_rate
        f[idx["answer_rate_lag_5"]] = lag5.answer_rate if lag5 else s.answer_rate
        f[idx["abandon_rate_lag_1"]] = lag1.abandon_rate if lag1 else s.abandon_rate
        f[idx["abandon_rate_lag_3"]] = lag3.abandon_rate if lag3 else s.abandon_rate
        f[idx["dial_ratio_lag_1"]] = (
            lag1.current_dial_ratio if lag1 else s.current_dial_ratio
        )
        f[idx["dial_ratio_lag_3"]] = (
            lag3.current_dial_ratio if lag3 else s.current_dial_ratio
        )
        f[idx["agents_available_lag_1"]] = (
            lag1.agents_available if lag1 else s.agents_available
        )

        # ── Erlang-C Features ─────────────────────────────────────
        f[idx["erlang_traffic_intensity"]] = s.erlang_traffic_intensity
        f[idx["erlang_utilization"]]       = s.erlang_utilization
        f[idx["erlang_wait_estimate"]]     = min(s.erlang_wait_estimate, 300)
        f[idx["erlang_service_level"]]     = s.erlang_service_level
        f[idx["erlang_optimal_ratio"]]     = s.erlang_optimal_ratio

        # ── Campaign Features ─────────────────────────────────────
        f[idx["leads_remaining"]]    = s.leads_remaining
        f[idx["leads_depleted_pct"]] = (
            1 - s.leads_remaining / s.leads_total
            if s.leads_total > 0 else 1.0
        )
        campaign_age = 0.0
        if s.campaign_start_time:
            campaign_age = (
                s.timestamp - s.campaign_start_time
            ).total_seconds() / 3600.0
        f[idx["campaign_age_hours"]]     = campaign_age
        f[idx["time_since_last_answer"]] = min(s.time_since_last_answer, 300)
        f[idx["historical_best_ratio"]]  = s.historical_best_ratio

        return f

    def get_feature_df(self, features: np.ndarray) -> pd.DataFrame:
        """Convert feature array to labeled DataFrame (for SHAP/debugging)"""
        return pd.DataFrame([features], columns=FEATURE_NAMES)
