# calls/signals.py
"""
Django signal handlers for CallLog.

Responsibilities
----------------
1. Update Redis real-time counters (campaigns.metrics) on every call status
   transition so the predictive dialer never needs expensive DB aggregations.

2. Set the ``is_abandoned`` boolean flag when a call is dropped (customer
   answered but no agent was available).

Counter update rules
--------------------
* ``created=True``        -> increment_attempted   (new origination)
* status -> "completed"
  AND answered_at set
  AND duration >= 5
  AND not AMD machine     -> increment_answered    (real human call)
* status -> "dropped"     -> increment_abandoned   (abandon)

These signals fire synchronously inside the same DB transaction as the save,
so the Redis write is extremely low-latency (~0.1 ms for a local Redis).
They are non-blocking: if Redis is unavailable the exception is swallowed and
logged -- the call flow is never interrupted.

Important ordering for "dropped" status
----------------------------------------
The ``is_abandoned`` DB flag is written BEFORE the Redis counter so that it
is always set correctly even when Redis is unavailable.  The Redis counter
failure is caught by the outer exception handler and logged.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("dialflow.signals")


@receiver(post_save, sender="calls.CallLog")
def update_dialer_metrics_on_calllog_save(
    sender, instance, created, update_fields, **kwargs
):
    """
    Fire on every CallLog.save().

    Uses ``update_fields`` when present to skip unnecessary Redis writes:
    if only ``agent_notes`` was saved we don't touch Redis at all.
    """
    campaign_id = instance.campaign_id
    if not campaign_id:
        return

    # -- DB flag update (no Redis) ------------------------------------------
    # is_abandoned must be set even when Redis is down, so we handle it
    # outside the Redis try/except block.
    status = None if created else instance.status
    if not created and status == "dropped" and not instance.is_abandoned:
        if update_fields is None or "status" in update_fields:
            sender.objects.filter(pk=instance.pk).update(is_abandoned=True)

    # -- Redis counter updates -----------------------------------------------
    try:
        from campaigns.metrics import (
            increment_abandoned,
            increment_answered,
            increment_attempted,
        )

        if created:
            # New origination
            increment_attempted(campaign_id)
            return

        # Skip entirely when we know status wasn't changed
        if update_fields is not None and "status" not in update_fields:
            return

        if status == "completed":
            # Count only real human-answered calls:
            # - answered_at must be set  -> customer actually picked up
            # - duration >= 5 s          -> not a machine beep / false answer
            # - not AMD machine          -> AMD didn't flag this as machine
            is_human = (
                instance.answered_at is not None
                and (instance.duration or 0) >= 5
                and "MACHINE" not in (instance.amd_result or "").upper()
            )
            if is_human:
                increment_answered(campaign_id)

        elif status == "dropped":
            increment_abandoned(campaign_id)

    except Exception:
        # Never let a metrics error kill the call flow
        logger.exception(
            "metrics update failed for campaign=%s calllog=%s",
            campaign_id, instance.pk,
        )
