# campaigns/predictive.py
"""
Adaptive Predictive Dialing Engine — v3
=========================================

Phase 1 — Critical fixes
--------------------------
* **Answer-rate fix** : counts only calls where ``answered_at`` is set,
  ``duration >= 5`` seconds, and AMD did not classify the call as MACHINE.
  Eliminates false positives from machine beeps and silent answers.
* **Abandon-rate fix** : abandon = ``dropped / (dropped + completed)``
  i.e. calls answered by a human but with no agent available, divided by
  all human-answered calls. Maps to the ``is_abandoned`` boolean field.
* **CPS limiter** : ``campaign.cps_limit`` caps originations per second
  using a Redis sliding-window; enforced inside ``originate_calls()``.
* **Async origination** : ``originate_calls()`` fires ARI requests
  concurrently via a thread pool, cutting latency for N > 1 calls.
* **Redis metrics** : ``_collect_metrics()`` reads Redis counters first
  (O(1)) and falls back to DB aggregation only when counters are cold.

Phase 2 — Stability & accuracy
--------------------------------
* **Correct Erlang-C inputs** :
    service_rate  = 1 / (avg_talk_time + avg_wrapup_time)
    arrival_rate  = calls_answered_5min / 300          (per second)
    intensity     = arrival_rate / service_rate
  This is the standard Erlang-C traffic-intensity formula (Erlangs = A = λ/μ).
* **Ring-time awareness** : while a call is ringing, agents are still
  available. ``_predict_agents_becoming_free()`` estimates how many on-call
  agents will finish during the average ring window, allowing a slightly
  higher ratio when many agents are near end-of-call.
* **Lag compensation** : counts calls originated in the last 5 seconds
  (via Redis sorted set) and adds them to ``calls_in_flight`` so we
  don't overshoot before the network has caught up.
* **Agent-availability prediction** :
    agents_becoming_free = agents_on_call × (ring_window / avg_talk_time)
  This value is factored into the effective agent count.
* **EMA ratio smoothing** : applies an exponential moving average
  (α = 0.3) to the raw Erlang-C result, dampening sudden ratio swings
  that would cause call-volume spikes.

Phase 3 — AI preparation
--------------------------
* **Feature logger** : every predictive tick logs a rich feature snapshot
  (metrics + ratio + time-of-day) to a Redis list consumed by the
  XGBoost online-learning pipeline.
* **Reward function** is implemented in ``ai_dialer/online_learning.py``.
* **Model fallback** : AI engine → Erlang-C → floor ratio.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger("dialflow.predictive")


# ─────────────────────────────────────────────────────────────────────────────
# Correlation ID helper
# ─────────────────────────────────────────────────────────────────────────────

def build_cid(campaign_id=None, lead_id=None, channel_id=None) -> str:
    """
    Build a compact correlation token shared across dialer/ARI logs.
    Example: cid=c1-l42-ch1706652491.12
    """
    parts = []
    if campaign_id not in (None, ""):
        parts.append(f"c{campaign_id}")
    if lead_id not in (None, ""):
        parts.append(f"l{lead_id}")
    if channel_id not in (None, ""):
        parts.append(f"ch{channel_id}")
    return "cid=" + ("-".join(parts) if parts else "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Metrics dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DialerMetrics:
    """
    Snapshot of campaign state at one dial tick.

    All rates are fractions [0.0 – 1.0].
    Time fields are in seconds.
    """
    # ── Agent state ────────────────────────────────────��──────────
    agents_ready:    int   = 0
    agents_on_call:  int   = 0
    agents_wrapup:   int   = 0

    # ── Timing ────────────────────────────────────────────────────
    avg_talk_time:   float = 120.0   # seconds per completed call
    avg_wrapup_time: float = 45.0    # seconds post-call
    avg_ring_time:   float = 20.0    # average ringing duration

    # ── Rates (fractions 0-1) ─────────────────────────────────────
    answer_rate:     float = 0.30   # human-answered / attempts
    abandon_rate:    float = 0.0    # dropped / human-answered
    amd_machine_rate: float = 0.0   # machine-flagged / attempts

    # ── Call queue ────────────────────────────────────────────────
    calls_in_flight: int   = 0      # currently ringing/connecting

    # ── Window counts (for Erlang-C) ──────────────────────────────
    calls_answered_5min:  int = 0   # human answers in last 5 min
    calls_attempted_5min: int = 0   # originations in last 5 min

    # ── Source flags (for logging) ────────────────────────────────
    metrics_source: str = "db"      # "redis" | "db" | "default"


# ─────────────────────────────────────────────────────────────────────────────
# Erlang-C (queue-theory wait probability)
# ─────────────────────────────────────────────────────────────────────────────

def _erlang_c(n_agents: int, intensity: float) -> float:
    """
    Erlang-C formula: probability that an arriving call must wait.

    Args:
        n_agents:  Number of parallel agents (servers).
        intensity: Traffic intensity A = λ/μ (Erlangs).

    Returns:
        Float in [0, 1]. 1.0 = all calls will wait; 0.0 = no waits.
    """
    if n_agents <= 0 or intensity <= 0:
        return 1.0
    if intensity >= n_agents:
        return 1.0   # System is overloaded

    try:
        # Erlang B (blocking) sum used as normalisation denominator
        b_sum  = sum((intensity ** k) / math.factorial(k) for k in range(n_agents))
        b_term = (
            (intensity ** n_agents)
            / (math.factorial(n_agents) * (1.0 - intensity / n_agents))
        )
        p0 = 1.0 / (b_sum + b_term)
        ec = b_term * p0
        return min(ec, 1.0)
    except (ZeroDivisionError, OverflowError):
        return 1.0


def _compute_erlang_intensity(
    calls_answered_5min: int,
    avg_talk_time: float,
    avg_wrapup_time: float,
) -> float:
    """
    Compute traffic intensity using the proper Erlang formula:

        arrival_rate  = calls_answered / observation_window (per second)
        service_rate  = 1 / (avg_talk_time + avg_wrapup_time)
        intensity (A) = arrival_rate / service_rate

    Uses a 5-minute (300 s) observation window for statistical stability.
    """
    # Arrival rate: answered calls per second over 5-minute window
    arrival_rate = calls_answered_5min / 300.0

    # Service rate: how many calls one agent completes per second
    handle_time  = max(avg_talk_time + avg_wrapup_time, 1.0)
    service_rate = 1.0 / handle_time

    # Traffic intensity: Erlangs (dimensionless)
    if service_rate <= 0:
        return 0.0
    return arrival_rate / service_rate


# ─────────────────────────────────────────────────────────────────────────────
# Metric collection (Redis-first, DB fallback)
# ─────────────────────────────────────────────────────────────────────────────

# Minimum number of calls in the window before Redis counts are trusted
_REDIS_MIN_CALLS = 5


def _collect_metrics(campaign_id: int, assigned_ids: list) -> DialerMetrics:
    """
    Build a DialerMetrics snapshot for one campaign tick.

    Fast path  → Redis real-time counters (O(1), ~0.1 ms)
    Slow path  → DB aggregation over last 200 calls (~5-20 ms)

    The fast path is used once the Redis sliding-window counters have
    accumulated at least ``_REDIS_MIN_CALLS`` events.

    Answer-rate fix
    ~~~~~~~~~~~~~~~
    Only counts calls where:
      1. ``answered_at`` is not null  → customer actually picked up
      2. ``duration >= 5``            → not a machine beep / false answer
      3. AMD result is not MACHINE    → AMD didn't flag it

    Abandon-rate fix
    ~~~~~~~~~~~~~~~~
    abandon_rate = dropped / (dropped + completed_human)
    i.e. calls answered by a human but lost due to no available agent,
    divided by ALL human-answered calls.
    """
    from agents.models import AgentStatus
    from calls.models import CallLog
    from campaigns.hopper import get_hopper_stats
    from campaigns.metrics import get_window_counts, get_lag_calls

    # ── Agent status counts (always from DB — this is cheap) ─────────────
    sc = AgentStatus.objects.filter(user_id__in=assigned_ids).aggregate(
        ready   = Count("id", filter=Q(status="ready")),
        on_call = Count("id", filter=Q(status="on_call")),
        wrapup  = Count("id", filter=Q(status="wrapup")),
    )
    agents_ready   = sc["ready"]   or 0
    agents_on_call = sc["on_call"] or 0
    agents_wrapup  = sc["wrapup"]  or 0

    hopper = get_hopper_stats(campaign_id)

    # Lag-compensation: calls originated in the last 5 s that haven't
    # been reflected in the dialing hash yet
    lag_calls   = get_lag_calls(campaign_id, window_seconds=5)
    in_flight   = hopper["in_flight"] + lag_calls

    # ── Try Redis counters (fast path) ────────────────────────────────────
    counts_60s  = get_window_counts(campaign_id, window_seconds=60)
    counts_5min = get_window_counts(campaign_id, window_seconds=300)

    redis_total = counts_60s["attempted"]

    if redis_total >= _REDIS_MIN_CALLS:
        # ── Redis metrics ──────────────────────────────────────────────────
        attempted  = counts_60s["attempted"]
        answered   = counts_60s["answered"]
        abandoned  = counts_60s["abandoned"]

        # Human-answered = completed + abandoned (both had a human pick up)
        human_answered = answered + abandoned

        answer_rate  = human_answered / max(attempted, 1)
        # Abandon rate = callers lost to "no agent" / all who answered
        abandon_rate = abandoned / max(human_answered, 1)
        # AMD machine rate is not tracked in Redis directly; use DB figure
        amd_rate     = 0.0

        # Timing from DB (cached values; not recalculated every tick)
        timing = (
            CallLog.objects
            .filter(campaign_id=campaign_id, status="completed",
                    answered_at__isnull=False)
            .order_by("-started_at")[:100]
            .aggregate(
                avg_talk = Avg("duration"),
                avg_ring = Avg("ring_duration"),
            )
        )
        avg_talk_time = float(timing["avg_talk"] or 120.0)
        avg_ring_time = float(timing["avg_ring"] or 20.0)

        logger.debug(
            "Metrics(redis): campaign=%d attempted=%d answered=%d abandoned=%d "
            "answer_rate=%.2f abandon_rate=%.4f in_flight=%d lag=%d",
            campaign_id, attempted, answered, abandoned,
            answer_rate, abandon_rate, in_flight, lag_calls,
        )

        return DialerMetrics(
            agents_ready          = agents_ready,
            agents_on_call        = agents_on_call,
            agents_wrapup         = agents_wrapup,
            avg_talk_time         = avg_talk_time,
            avg_ring_time         = avg_ring_time,
            answer_rate           = answer_rate,
            abandon_rate          = abandon_rate,
            amd_machine_rate      = amd_rate,
            calls_in_flight       = in_flight,
            calls_answered_5min   = counts_5min["answered"],
            calls_attempted_5min  = counts_5min["attempted"],
            metrics_source        = "redis",
        )

    # ── DB fallback (cold start or Redis unavailable) ─────────────────────
    recent = (
        CallLog.objects
        .filter(campaign_id=campaign_id, started_at__isnull=False)
        .order_by("-started_at")[:200]
    )
    total = recent.count()

    if total < 5:
        logger.debug(
            "Metrics(default): campaign=%d too few calls=%d",
            campaign_id, total,
        )
        return DialerMetrics(
            agents_ready   = agents_ready,
            agents_on_call = agents_on_call,
            agents_wrapup  = agents_wrapup,
            calls_in_flight = in_flight,
            metrics_source = "default",
        )

    agg = recent.aggregate(
        # Timing
        avg_talk = Avg("duration", filter=Q(status="completed")),
        avg_ring = Avg("ring_duration"),
        # ── FIXED: human-answered = answered_at set + duration >= 5 + not machine
        human_answered = Count(
            "id",
            filter=Q(
                answered_at__isnull=False,
                duration__gte=5,
            ) & ~Q(amd_result__icontains="MACHINE"),
        ),
        # ── FIXED: abandoned = dropped (answered but no agent)
        abandoned  = Count("id", filter=Q(status="dropped")),
        amd_machine = Count("id", filter=Q(amd_result__icontains="MACHINE")),
    )

    human_answered = agg["human_answered"] or 0
    abandoned      = agg["abandoned"]      or 0
    amd_m          = agg["amd_machine"]    or 0

    # answer_rate  = human_answered / total_attempts
    answer_rate  = human_answered / max(total, 1)
    # abandon_rate = abandoned / (abandoned + human_answered)
    #   — fraction of human-answered calls that got no agent
    abandon_rate = abandoned / max(human_answered + abandoned, 1)
    amd_rate     = amd_m / max(total, 1)

    avg_talk_time = float(agg["avg_talk"] or 120.0)
    avg_ring_time = float(agg["avg_ring"] or 20.0)

    # 5-minute window counts for Erlang-C (approximate from the 200-call slice)
    # Use the full window queries for precision when we have enough data
    counts_5m = get_window_counts(campaign_id, window_seconds=300)

    logger.debug(
        "Metrics(db): campaign=%d total=%d human_answered=%d abandoned=%d "
        "answer_rate=%.2f abandon_rate=%.4f amd_rate=%.2f in_flight=%d",
        campaign_id, total, human_answered, abandoned,
        answer_rate, abandon_rate, amd_rate, in_flight,
    )

    return DialerMetrics(
        agents_ready          = agents_ready,
        agents_on_call        = agents_on_call,
        agents_wrapup         = agents_wrapup,
        avg_talk_time         = avg_talk_time,
        avg_ring_time         = avg_ring_time,
        answer_rate           = answer_rate,
        abandon_rate          = abandon_rate,
        amd_machine_rate      = amd_rate,
        calls_in_flight       = in_flight,
        calls_answered_5min   = counts_5m["answered"],
        calls_attempted_5min  = counts_5m["attempted"],
        metrics_source        = "db",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent availability prediction
# ─────────────────────────────────────────────────────────────────────────────

def _predict_agents_becoming_free(
    metrics: DialerMetrics,
    time_window_s: float,
) -> float:
    """
    Estimate how many currently-on-call agents will finish before
    ``time_window_s`` seconds elapse.

    Uses the memoryless property of exponential service times:
        P(finish in window) ≈ window / avg_talk_time   (linear approximation)
        agents_becoming_free = agents_on_call × P(finish)

    Capped at ``agents_on_call`` to stay physically realistic.

    This is used to slightly increase the effective agent count during the
    ring period, allowing the dialer to be a bit more aggressive when many
    agents are near end-of-call.
    """
    if metrics.agents_on_call <= 0 or metrics.avg_talk_time <= 0:
        return 0.0

    prob   = min(time_window_s / metrics.avg_talk_time, 1.0)
    result = metrics.agents_on_call * prob
    return min(result, float(metrics.agents_on_call))


# ─────────────────────────────────────────────────────────────────────────────
# Core dial-ratio algorithm (Erlang-C + governors + smoothing)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_dial_ratio(
    metrics: DialerMetrics,
    campaign,
    campaign_id: int = 0,
) -> float:
    """
    Compute the recommended dial ratio for one campaign tick.

    Improvements over v2
    ---------------------
    1. **Correct Erlang-C intensity** — uses proper arrival/service rates
       instead of a fixed ``n_ready × 0.90`` target utilisation.
    2. **Ring-time awareness** — accounts for agents becoming free during
       the average ring window, slightly increasing effective capacity.
    3. **Lag compensation** — already applied in calls_in_flight via
       ``_collect_metrics()``; the ratio itself doesn't double-count.
    4. **Agent availability forecast** — ``_predict_agents_becoming_free``
       adds a fractional agent count to the effective ready pool.
    5. **EMA smoothing** — prevents sudden ratio spikes that cause
       call-volume surges.

    Args:
        metrics:     Snapshot from ``_collect_metrics()``.
        campaign:    Django Campaign ORM instance.
        campaign_id: Campaign PK (used for EMA state lookup).

    Returns:
        Smoothed, clamped dial ratio.
    """
    from campaigns.metrics import update_ema_ratio

    min_ratio  = float(campaign.min_dial_ratio)
    max_ratio  = float(campaign.max_dial_ratio)
    target_abn = float(campaign.abandon_rate) / 100.0  # stored as %, need fraction
    n_ready    = metrics.agents_ready

    if n_ready <= 0:
        return 0.0

    # ── Governor 1: Hard abandon throttle ────────────────────────────────────
    # If the live abandon rate is already > 1.5× target, slam the brakes.
    if metrics.abandon_rate > target_abn * 1.5:
        logger.warning(
            "Abandon throttle: campaign=%d abandon=%.2f%% target=%.2f%% → min_ratio",
            campaign_id, metrics.abandon_rate * 100, target_abn * 100,
        )
        ema = update_ema_ratio(campaign_id, min_ratio) if campaign_id else min_ratio
        return ema

    ar = max(metrics.answer_rate, 0.01)

    # ── Governor 2: Very low answer rate (likely network congestion) ─────────
    if ar < 0.15:
        ratio = min(min_ratio + 0.3, max_ratio)
        ema   = update_ema_ratio(campaign_id, ratio) if campaign_id else ratio
        return ema

    # ── Phase 2: Correct Erlang-C intensity ──────────────────────────────────
    intensity = _compute_erlang_intensity(
        calls_answered_5min = metrics.calls_answered_5min,
        avg_talk_time       = metrics.avg_talk_time,
        avg_wrapup_time     = metrics.avg_wrapup_time,
    )

    # If we don't have 5-minute data yet, fall back to n_ready × 0.85 target
    if intensity <= 0:
        intensity = n_ready * 0.85

    # Cap intensity below n_ready to keep Erlang-C numerically stable
    intensity = min(intensity, n_ready * 0.99)

    ec         = _erlang_c(n_ready, intensity)
    base_ratio = (intensity / n_ready) / ar

    # ── Erlang-C adjustment (EC is probability of waiting) ───────────────────
    #   EC > 0.35  → system is congested → reduce ratio
    #   EC < 0.05  → system is idle      → increase ratio slightly
    if ec > 0.35:
        adjustment = 1.0 - (ec - 0.35) * 0.5
    elif ec < 0.05:
        adjustment = 1.0 + (0.05 - ec) * 2.0
    else:
        adjustment = 1.0

    ratio = base_ratio * adjustment

    # ── Phase 2: Abandon rate fine-tuning ────────────────────────────────────
    if metrics.abandon_rate > target_abn:
        overrun = (metrics.abandon_rate - target_abn) / max(target_abn, 0.01)
        ratio   = ratio * (1.0 - min(overrun * 0.3, 0.4))

    # ── Phase 2: Ring-time awareness + agent availability forecast ────────────
    # During the ring window, some on-call agents will finish. Count them
    # as fractional additions to the ready pool, allowing a slightly higher ratio.
    ring_window          = max(metrics.avg_ring_time, 5.0)
    agents_becoming_free = _predict_agents_becoming_free(metrics, ring_window)
    effective_ready      = n_ready + agents_becoming_free * 0.4  # conservative weight

    # Re-scale ratio by effective vs actual ready agents
    if effective_ready > n_ready:
        ratio = ratio * (n_ready / effective_ready)

    # ── Clamp and round ───────────────────────────────────────────────────────
    raw_ratio = max(min_ratio, min(max_ratio, round(ratio, 3)))

    # ── Phase 2: EMA smoothing ────────────────────────────────────────────────
    # Prevent sudden spikes: new value is blended with the historical EMA.
    smoothed = update_ema_ratio(campaign_id, raw_ratio) if campaign_id else raw_ratio
    smoothed = max(min_ratio, min(max_ratio, smoothed))

    logger.debug(
        "Ratio calc: campaign=%d intensity=%.3f ec=%.3f base=%.3f adj=%.3f "
        "raw=%.3f ema=%.3f agents_free_est=%.2f",
        campaign_id, intensity, ec, base_ratio, adjustment,
        raw_ratio, smoothed, agents_becoming_free,
    )

    return round(smoothed, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Main dial-decision entry point
# ─────────────────────────────────────────────────────────────────────────────

def get_calls_to_dial(campaign_id: int) -> int:
    """
    Return the number of calls to originate for one campaign on this tick.

    Called every second by ``predictive_dial_tick`` Celery task.

    Decision flow
    -------------
    1. Validate campaign is active and within call hours.
    2. Bail if hopper is empty or no agents are assigned.
    3. Return 0 if no agents are ready (prevents wasted dials).
    4. Mode dispatch:
       - preview     → 0 (agent dials manually)
       - progressive → 1 call per ready agent
       - predictive  → AI engine (XGBoost) → Erlang-C → floor
    """
    from campaigns.models import Campaign
    from campaigns.hopper import get_hopper_stats

    try:
        campaign = Campaign.objects.select_related(
            "asterisk_server", "carrier"
        ).get(id=campaign_id, status=Campaign.STATUS_ACTIVE)
    except Campaign.DoesNotExist:
        logger.debug("Dial skip: campaign=%d not active or missing", campaign_id)
        return 0

    # ── Call hours check ──────────────────────────────────────────────────────
    try:
        import pytz
        tz  = pytz.timezone("Asia/Kolkata")
        now = timezone.now().astimezone(tz)
        if not (campaign.call_hour_start <= now.time() <= campaign.call_hour_end):
            logger.debug(
                "Dial skip: campaign=%d outside hours now=%s window=%s-%s",
                campaign_id, now.time(), campaign.call_hour_start, campaign.call_hour_end,
            )
            return 0
    except Exception:
        pass

    # ── Hopper / agent guards ─────────────────────────────────────────────────
    hopper = get_hopper_stats(campaign_id)
    if hopper["queued"] == 0:
        logger.debug("Dial skip: campaign=%d hopper empty", campaign_id)
        return 0

    assigned_ids = list(
        campaign.agents.filter(is_active=True).values_list("agent_id", flat=True)
    )
    if not assigned_ids:
        logger.debug("Dial skip: campaign=%d no assigned agents", campaign_id)
        return 0

    metrics = _collect_metrics(campaign_id, assigned_ids)

    if metrics.agents_ready == 0:
        logger.debug(
            "Dial skip: campaign=%d no ready agents on_call=%d wrapup=%d in_flight=%d",
            campaign_id, metrics.agents_on_call, metrics.agents_wrapup, metrics.calls_in_flight,
        )
        return 0

    # ── Mode dispatch ─────────────────────────────────────────────────────────

    if campaign.dial_mode == Campaign.DIAL_MODE_PREVIEW:
        logger.debug("Dial skip: campaign=%d preview mode", campaign_id)
        return 0

    elif campaign.dial_mode == Campaign.DIAL_MODE_PROGRESSIVE:
        to_dial = max(
            0,
            min(metrics.agents_ready - metrics.calls_in_flight, hopper["queued"]),
        )
        logger.info(
            "Progressive: campaign=%d ready=%d in_flight=%d queued=%d to_dial=%d",
            campaign_id, metrics.agents_ready, metrics.calls_in_flight,
            hopper["queued"], to_dial,
        )
        return to_dial

    elif campaign.dial_mode == Campaign.DIAL_MODE_PREDICTIVE:
        return _predictive_decision(campaign_id, campaign, metrics, hopper)

    logger.debug("Dial skip: campaign=%d unsupported mode=%s", campaign_id, campaign.dial_mode)
    return 0


def _predictive_decision(
    campaign_id: int,
    campaign,
    metrics: DialerMetrics,
    hopper: dict,
) -> int:
    """
    Inner predictive decision: AI → Erlang-C → floor.

    AI Engine (XGBoost)
    ~~~~~~~~~~~~~~~~~~~~
    Attempts to use the pre-trained / online-learning AI engine.
    Falls through to Erlang-C if AI is in cold-start or errors out.

    Erlang-C fallback
    ~~~~~~~~~~~~~~~~~
    Uses ``calculate_dial_ratio()`` with all Phase 2 improvements.

    Phase 3: Feature logging
    ~~~~~~~~~~~~~~~~~~~~~~~~
    Always logs the decision features for AI training regardless of which
    path produced the final answer.
    """
    from campaigns.metrics import log_dial_features

    # ── AI engine (XGBoost) ───────────────────────────────────────────────���───
    try:
        from ai_dialer.engine import get_ai_manager
        decision = get_ai_manager().get_dialer(campaign_id).decide()
        if decision.ratio_source not in ("error_fallback", "paused"):
            to_dial = min(decision.calls_to_dial, hopper["queued"])
            log = logger.info if to_dial > 0 else logger.debug
            log(
                "AI decision: campaign=%d ratio=%.3f source=%s conf=%.2f "
                "pred_answer=%.1f%% abandon_risk=%.1f%% to_dial=%d health=%s",
                campaign_id, decision.dial_ratio, decision.ratio_source,
                decision.model_confidence,
                decision.predicted_answer_rate * 100,
                decision.abandon_risk_probability * 100,
                to_dial, decision.health,
            )
            for w in decision.warnings:
                logger.warning("AI warning: campaign=%d %s", campaign_id, w)

            _log_decision_features(
                campaign_id, metrics, decision.dial_ratio, to_dial, "ai"
            )
            return to_dial
    except Exception as exc:
        logger.warning(
            "AI engine unavailable campaign=%d: %s — Erlang-C fallback",
            campaign_id, exc,
        )

    # ── Erlang-C fallback ─────────────────────────────────────────────────────
    ratio = calculate_dial_ratio(metrics, campaign, campaign_id=campaign_id)
    if ratio <= 0:
        logger.debug("Dial skip: campaign=%d ratio=%.3f", campaign_id, ratio)
        return 0

    target  = math.ceil(ratio * metrics.agents_ready)
    to_dial = max(0, min(target - metrics.calls_in_flight, hopper["queued"]))

    # Safety cap: never place more simultaneous dials than ready agents.
    # Prevents over-origination races in low-seat campaigns (1–2 agents).
    to_dial = min(to_dial, metrics.agents_ready)

    logger.info(
        "Erlang-C decision: campaign=%d ratio=%.3f ready=%d on_call=%d wrapup=%d "
        "in_flight=%d queued=%d target=%d answer=%.1f%% abandon=%.2f%% to_dial=%d "
        "src=%s",
        campaign_id, ratio, metrics.agents_ready, metrics.agents_on_call,
        metrics.agents_wrapup, metrics.calls_in_flight, hopper["queued"], target,
        metrics.answer_rate * 100, metrics.abandon_rate * 100, to_dial,
        metrics.metrics_source,
    )

    _log_decision_features(campaign_id, metrics, ratio, to_dial, "erlang_c")
    return to_dial


def _log_decision_features(
    campaign_id: int,
    metrics: DialerMetrics,
    ratio: float,
    to_dial: int,
    source: str,
) -> None:
    """
    Phase 3: Log dial-decision feature snapshot to Redis for AI training.

    Stored in the feature_snapshots list consumed by:
    ``ai_dialer.engine.AIDialer.record_outcome()``
    """
    try:
        now = timezone.now()
        from campaigns.metrics import log_dial_features
        log_dial_features(campaign_id, {
            # ── Agent state ──────────────────────────────────────────────
            "agents_ready":       metrics.agents_ready,
            "agents_on_call":     metrics.agents_on_call,
            "agents_wrapup":      metrics.agents_wrapup,
            # ── Call queue ───────────────────────────────────────────────
            "calls_in_flight":    metrics.calls_in_flight,
            "calls_answered_5m":  metrics.calls_answered_5min,
            "calls_attempted_5m": metrics.calls_attempted_5min,
            # ── Rates ────────────────────────────────────────────────────
            "answer_rate":        round(metrics.answer_rate, 4),
            "abandon_rate":       round(metrics.abandon_rate, 4),
            "amd_rate":           round(metrics.amd_machine_rate, 4),
            # ── Timing ───────────────────────────────────────────────────
            "avg_talk_time":      metrics.avg_talk_time,
            "avg_wrapup_time":    metrics.avg_wrapup_time,
            "avg_ring_time":      metrics.avg_ring_time,
            # ── Decision output ──────────────────────────────────────────
            "dial_ratio":         ratio,
            "to_dial":            to_dial,
            "source":             source,
            # ── Time-of-day context ──────────────────────────────────────
            "hour_of_day":        now.hour,
            "day_of_week":        now.weekday(),
            "metrics_source":     metrics.metrics_source,
            # ── Reward function inputs (Phase 3) ─────────────────────────
            # Full reward computed later by record_dialer_outcomes task
            "_reward_answered":   metrics.calls_answered_5min,
            "_reward_abandoned":  metrics.calls_attempted_5min - metrics.calls_answered_5min,
        })
    except Exception:
        pass  # Never let logging kill the dial tick


# ─────────────────────────────────────────────────────────────────────────────
# Agent helpers (unchanged API)
# ─────────────────────────────────────────────────────────────────────────────

def get_longest_waiting_agent(campaign_id: int) -> Optional[int]:
    """Return the user_id of the agent who has been ready the longest."""
    from agents.models import AgentStatus
    from campaigns.models import CampaignAgent

    assigned = CampaignAgent.objects.filter(
        campaign_id=campaign_id, is_active=True
    ).values_list("agent_id", flat=True)

    agent = (
        AgentStatus.objects
        .filter(user_id__in=assigned, status="ready")
        .order_by("status_changed_at")
        .first()
    )
    return agent.user_id if agent else None


def get_ready_agents_ordered(campaign_id: int) -> List[int]:
    """Return ready agent IDs ordered by wait time (longest-waiting first)."""
    from agents.models import AgentStatus
    from campaigns.models import CampaignAgent

    assigned = CampaignAgent.objects.filter(
        campaign_id=campaign_id, is_active=True
    ).values_list("agent_id", flat=True)

    return list(
        AgentStatus.objects
        .filter(user_id__in=assigned, status="ready")
        .order_by("status_changed_at")
        .values_list("user_id", flat=True)
    )


def resolve_campaign_carrier(campaign):
    """Resolve the SIP carrier for a campaign (direct or prefix-matched)."""
    from telephony.models import Carrier

    if campaign.carrier_id:
        return campaign.carrier

    if campaign.dial_prefix:
        return (
            Carrier.objects
            .filter(
                asterisk_server=campaign.asterisk_server,
                is_active=True,
                dial_prefix=campaign.dial_prefix,
            )
            .order_by("id")
            .first()
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# ARI origination — async + CPS-throttled
# ─────────────────────────────────────────────────────────────────────────────

def originate_calls(campaign_id: int, count: int) -> int:
    """
    Originate ``count`` outbound calls via Asterisk ARI.

    Improvements over v2
    --------------------
    * **CPS enforcement** — checks ``campaign.cps_limit`` before each call.
      Uses a Redis 1-second sliding window to track the rate.
    * **Async / concurrent** — uses a thread-pool so multiple ARI HTTP
      requests are fired in parallel, reducing total latency from
      O(N × RTT) to O(RTT) for small batches.
    * **Rollback on failure** — failed calls are re-queued in the hopper.
    * **Redis counter** — calls ``increment_attempted()`` on each success
      to keep the real-time metrics current.

    Args:
        campaign_id: Campaign to originate for.
        count:       Maximum calls to place this tick.

    Returns:
        Number of calls successfully submitted to ARI.
    """
    from campaigns.hopper import pop_lead, get_redis, hopper_key, complete_lead
    from campaigns.metrics import (
        check_and_reserve_cps, record_cps_origination, increment_attempted
    )
    from campaigns.models import Campaign
    from calls.models import CallLog

    if count <= 0:
        logger.debug("Originate skip: campaign=%d count=%d", campaign_id, count)
        return 0

    try:
        campaign = Campaign.objects.select_related(
            "asterisk_server", "carrier"
        ).get(id=campaign_id)
    except Campaign.DoesNotExist:
        logger.debug("Originate skip: campaign=%d missing", campaign_id)
        return 0

    # ── CPS check — apply before popping any leads ────────────────────────────
    cps_limit     = int(campaign.cps_limit)
    allowed_count = check_and_reserve_cps(campaign_id, cps_limit, count)
    if allowed_count == 0:
        logger.info(
            "CPS limit reached: campaign=%d cps_limit=%d wanted=%d",
            campaign_id, cps_limit, count,
        )
        return 0

    server   = campaign.asterisk_server
    ari_base = f"http://{server.ari_host}:{server.ari_port}/ari"
    auth     = (server.ari_username, server.ari_password)

    # ── Pop leads from hopper ─────────────────────────────────────────────────
    leads_batch = []
    for _ in range(allowed_count):
        lead_data = pop_lead(campaign_id)
        if not lead_data:
            break
        leads_batch.append(lead_data)

    if not leads_batch:
        return 0

    # ── Build per-call origination payloads ───────────────────────────────────
    carrier = resolve_campaign_carrier(campaign)

    originate_jobs = []
    for lead_data in leads_batch:
        phone   = lead_data["phone"]
        lead_id = lead_data["lead_id"]

        dial_prefix = campaign.dial_prefix or (carrier.dial_prefix if carrier else "")
        dial_no     = f"{dial_prefix}{phone}" if dial_prefix else phone
        endpoint    = (
            f"Local/{dial_no}@{carrier.dialplan_context}"
            if carrier
            else f"PJSIP/{dial_no}@dialout"
        )

        variables = {
            "CALL_TYPE":       "autodial",
            "CAMPAIGN_ID":     str(campaign_id),
            "LEAD_ID":         str(lead_id),
            "CUSTOMER_NUMBER": phone,
            "RECORD_CALL":     "1" if campaign.enable_recording else "0",
        }
        if campaign.amd_enabled:
            variables.update({
                "AMD_ENABLED":                "1",
                "AMD_ACTION":                 campaign.amd_action,
                "AMD_INITIAL_SILENCE":        "3000",
                "AMD_GREETING":               "1500",
                "AMD_AFTER_GREETING_SILENCE": "800",
                "AMD_TOTAL_ANALYSIS_TIME":    "5000",
                "AMD_MIN_WORD_LENGTH":        "100",
                "AMD_BETWEEN_WORDS_SILENCE":  "50",
                "AMD_MAX_WORDS":              "3",
                "AMD_SILENCE_THRESHOLD":      "256",
            })

        originate_jobs.append({
            "lead_data": lead_data,
            "lead_id":   lead_id,
            "payload": {
                "endpoint":   endpoint,
                "app":        server.ari_app_name,
                "callerId":   campaign.caller_id or "",
                "timeout":    campaign.dial_timeout,
                "variables":  variables,
            },
        })

    # ── Concurrent origination via thread pool ────────────────────────────────
    initiated = 0

    def _originate_one(job: dict) -> Optional[str]:
        """
        Fire one ARI origination request.
        Returns channel_id on success, None on failure.
        """
        import requests as req_lib
        cid_str = build_cid(campaign_id=campaign_id, lead_id=job["lead_id"])
        try:
            resp = req_lib.post(
                f"{ari_base}/channels",
                json=job["payload"],
                auth=auth,
                timeout=5,
            )
            resp.raise_for_status()
            ch_id = resp.json().get("id", "")
            logger.info(
                "Originate OK: %s campaign=%d lead=%d channel=%s",
                cid_str, campaign_id, job["lead_id"], ch_id,
            )
            return ch_id
        except Exception as exc:
            logger.error(
                "Originate FAIL: %s lead=%d error=%s",
                cid_str, job["lead_id"], exc,
            )
            return None

    # Use up to min(allowed_count, 20) threads for concurrent HTTP calls
    max_workers = min(len(originate_jobs), 20)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {
            pool.submit(_originate_one, job): job
            for job in originate_jobs
        }

        for future in concurrent.futures.as_completed(future_to_job):
            job       = future_to_job[future]
            lead_data = job["lead_data"]
            lead_id   = job["lead_id"]
            cid_str   = build_cid(campaign_id=campaign_id, lead_id=lead_id)

            try:
                channel_id = future.result()
            except Exception as exc:
                channel_id = None
                logger.error("Thread error: %s %s", cid_str, exc)

            if channel_id:
                # ── Success ───────────────────────────────────────────────────
                CallLog.objects.create(
                    campaign_id  = campaign_id,
                    lead_id      = lead_id,
                    channel_id   = channel_id,
                    phone_number = lead_data["phone"],
                    direction    = "outbound",
                    status       = "initiated",
                    started_at   = timezone.now(),
                )
                # Update CPS window and Redis attempted counter
                record_cps_origination(campaign_id)
                increment_attempted(campaign_id)
                initiated += 1
            else:
                # ── Failure: re-queue lead back to hopper ───────���─────────────
                try:
                    complete_lead(campaign_id, lead_id)
                    get_redis().rpush(hopper_key(campaign_id), json.dumps(lead_data))
                    logger.info(
                        "Rollback re-queue: %s campaign=%d lead=%d",
                        cid_str, campaign_id, lead_id,
                    )
                except Exception:
                    pass

    logger.info(
        "Originate summary: campaign=%d requested=%d allowed=%d initiated=%d",
        campaign_id, count, allowed_count, initiated,
    )
    return initiated
