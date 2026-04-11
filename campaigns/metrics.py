# campaigns/metrics.py
"""
Real-time dialer metrics via Redis.
=====================================
Replaces heavy CallLog.aggregate() calls with O(1) Redis counter operations
that are updated on every call event via Django signals.

Architecture
------------
* **Sliding-window counters** — Redis sorted-sets (score = Unix timestamp).
  Members are evicted when older than the window, keeping RAM bounded.
* **EMA ratio state** — one Redis string per campaign; updated each tick.
* **CPS limiter** — 1-second sliding window for Calls-Per-Second enforcement.
* **Lag tracker** — 5-second window counts recently-originated calls to
  prevent over-dialling before the network has caught up.
* **Feature logger** — stores dial-decision snapshots for AI training.

All Redis keys follow the pattern ``campaign:{id}:<purpose>`` so they can
be inspected/wiped per-campaign without touching other campaigns.
"""

import json
import logging
import time
from typing import Dict, Tuple

logger = logging.getLogger("dialflow.metrics")

# ── Redis key templates ───────────────────────────────────────────────────────
_K_ATTEMPTED  = "campaign:{cid}:ctr:attempted"   # zset — call was originated
_K_ANSWERED   = "campaign:{cid}:ctr:answered"    # zset — human answered (≥5 s)
_K_ABANDONED  = "campaign:{cid}:ctr:abandoned"   # zset — answered + no agent
_K_ORIGINATED = "campaign:{cid}:ctr:originated"  # zset — lag compensation
_K_CPS        = "campaign:{cid}:cps:window"      # zset — CPS sliding window
_K_EMA_RATIO  = "campaign:{cid}:ema_ratio"       # string — EMA dial ratio
_K_FEATURES   = "campaign:{cid}:feature_snapshots"  # list  — AI training log

# ── Window sizes ─────────────────────────────────────────────────────────────
WINDOW_5S    = 5
WINDOW_60S   = 60
WINDOW_300S  = 300   # 5-minute window used for Erlang-C arrival rate

# ── EMA smoothing factor ─────────────────────────────────────────────────────
EMA_ALPHA = 0.3   # 0.3 = fairly responsive; raise for faster tracking


# ── Internal helpers ──────────────────────────────────────────────────────────

def _r():
    """Return the shared Redis connection (re-uses hopper's connection)."""
    from campaigns.hopper import get_redis
    return get_redis()


def _k(template: str, campaign_id: int) -> str:
    return template.format(cid=campaign_id)


def _zincr(r, key: str, ttl: int):
    """
    Add current-timestamp member to sorted set and expire stale entries.
    Uses a pipeline for atomicity.
    """
    now = time.time()
    pipe = r.pipeline()
    pipe.zadd(key, {str(now): now})
    pipe.expire(key, ttl)
    pipe.execute()


def _zcount_window(r, key: str, window_seconds: int) -> int:
    """Count members in sorted set within the last ``window_seconds``."""
    cutoff = time.time() - window_seconds
    # Prune expired entries first (keeps memory tidy)
    r.zremrangebyscore(key, "-inf", cutoff)
    return r.zcard(key)


# ── Call-event counters ───────────────────────────────────────────────────────

def increment_attempted(campaign_id: int) -> None:
    """
    Record a call origination attempt.
    Called from originate_calls() immediately after each ARI request succeeds.
    """
    r = _r()
    _zincr(r, _k(_K_ATTEMPTED, campaign_id), WINDOW_300S + 60)
    # Lag tracker — very short TTL; only needed for the next few seconds
    _zincr(r, _k(_K_ORIGINATED, campaign_id), WINDOW_5S + 5)


def increment_answered(campaign_id: int) -> None:
    """
    Record a human-answered call (duration >= 5 s, not an AMD machine).
    Called from the CallLog post_save signal.
    """
    r = _r()
    _zincr(r, _k(_K_ANSWERED, campaign_id), WINDOW_300S + 60)


def increment_abandoned(campaign_id: int) -> None:
    """
    Record an abandoned call (answered by customer but no agent available).
    Maps to CallLog.status == 'dropped'.
    Called from the CallLog post_save signal.
    """
    r = _r()
    _zincr(r, _k(_K_ABANDONED, campaign_id), WINDOW_300S + 60)


# ── Window metric reads ───────────────────────────────────────────────────────

def get_window_counts(campaign_id: int, window_seconds: int) -> Dict[str, int]:
    """
    Return call counts for the last ``window_seconds``.

    Returns::

        {
            "attempted": N,   # Total originations
            "answered":  N,   # Human-answered (≥5 s, non-machine)
            "abandoned": N,   # Answered but no agent
        }
    """
    r = _r()
    return {
        "attempted": _zcount_window(r, _k(_K_ATTEMPTED, campaign_id), window_seconds),
        "answered":  _zcount_window(r, _k(_K_ANSWERED,  campaign_id), window_seconds),
        "abandoned": _zcount_window(r, _k(_K_ABANDONED,  campaign_id), window_seconds),
    }


def get_lag_calls(campaign_id: int, window_seconds: int = WINDOW_5S) -> int:
    """
    Return calls originated in the last ``window_seconds``.

    Used for **lag compensation**: calls placed in the last few seconds
    haven't been answered or dropped yet, so they aren't reflected in
    ``calls_in_flight`` (which comes from the Redis dialing hash).
    Including them prevents over-dialling.
    """
    r = _r()
    return _zcount_window(r, _k(_K_ORIGINATED, campaign_id), window_seconds)


# ── CPS (Calls Per Second) throttle ──────────────────────────────────────────

def check_and_reserve_cps(campaign_id: int, cps_limit: int, count: int) -> int:
    """
    Check how many calls can be placed this second without breaching CPS.

    * If ``cps_limit <= 0`` the campaign has no CPS cap → return ``count``.
    * Otherwise returns ``min(count, remaining_cps_budget)``.
    * **Does not** record the reservation; call ``record_cps_originations``
      after each successful origination.

    Args:
        campaign_id: Campaign to check.
        cps_limit:   Maximum calls per second (from campaign.cps_limit).
        count:       Desired number of calls.

    Returns:
        How many calls may actually be placed right now.
    """
    if cps_limit <= 0:
        return count

    r      = _r()
    k      = _k(_K_CPS, campaign_id)
    cutoff = time.time() - 1.0

    # Atomic prune + count
    pipe = r.pipeline()
    pipe.zremrangebyscore(k, "-inf", cutoff)
    pipe.zcard(k)
    _, current_cps = pipe.execute()

    remaining = max(0, cps_limit - current_cps)
    allowed   = min(count, remaining)

    if allowed < count:
        logger.info(
            "CPS throttle: campaign=%d cps_limit=%d current=%d wanted=%d allowed=%d",
            campaign_id, cps_limit, current_cps, count, allowed,
        )
    return allowed


def record_cps_origination(campaign_id: int) -> None:
    """Record one successful origination in the CPS sliding window."""
    r   = _r()
    k   = _k(_K_CPS, campaign_id)
    now = time.time()
    # Use a unique member key (timestamp + random suffix) so rapid successive
    # calls within the same microsecond are all counted as distinct entries.
    import random as _random
    member = f"{now:.6f}:{_random.getrandbits(32)}"
    r.zadd(k, {member: now})
    r.expire(k, 2)  # Auto-expire after 2 seconds — we only care about the last 1s


# ── EMA dial-ratio smoother ───────────────────────────────────────────────────

def get_ema_ratio(campaign_id: int, default: float = 1.5) -> float:
    """Return the current EMA-smoothed dial ratio. Falls back to ``default``."""
    v = _r().get(_k(_K_EMA_RATIO, campaign_id))
    return float(v) if v else default


def update_ema_ratio(campaign_id: int, raw_ratio: float) -> float:
    """
    Apply exponential moving average to a freshly-calculated dial ratio.

    Prevents sudden large ratio swings that would cause call spikes::

        ema_ratio = α × raw_ratio + (1-α) × prev_ema

    Args:
        campaign_id: Campaign being updated.
        raw_ratio:   The ratio just calculated by the Erlang-C algorithm.

    Returns:
        Smoothed ratio.
    """
    r    = _r()
    k    = _k(_K_EMA_RATIO, campaign_id)
    prev = float(r.get(k) or raw_ratio)   # default to raw if no history
    ema  = EMA_ALPHA * raw_ratio + (1.0 - EMA_ALPHA) * prev
    r.set(k, round(ema, 4), ex=3600)     # 1-hour TTL
    return ema


# ── Feature logger (Phase 3 — AI training data) ───────────────────────────────

def log_dial_features(campaign_id: int, features: Dict) -> None:
    """
    Persist a dial-decision feature snapshot for future AI/ML training.

    Stores up to 1 000 snapshots per campaign in a Redis list (FIFO).
    Each snapshot contains:

    * All DialerMetrics fields
    * The calculated ratio and calls_to_dial
    * Time-of-day context
    * Reward signal components

    These snapshots are consumed by the XGBoost online-learning pipeline
    (``ai_dialer.engine.AIDialer.record_outcome``).
    """
    r       = _r()
    k       = _k(_K_FEATURES, campaign_id)
    payload = json.dumps({**features, "_ts": time.time()})
    pipe    = r.pipeline()
    pipe.rpush(k, payload)
    pipe.ltrim(k, -1000, -1)   # Keep only the most recent 1 000 entries
    pipe.expire(k, 86400)      # 24-hour TTL
    pipe.execute()


def get_recent_features(campaign_id: int, n: int = 100) -> list:
    """
    Retrieve the last ``n`` feature snapshots for a campaign.
    Useful for debugging AI training data quality.
    """
    r   = _r()
    k   = _k(_K_FEATURES, campaign_id)
    raw = r.lrange(k, -n, -1)
    out = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:
            pass
    return out
