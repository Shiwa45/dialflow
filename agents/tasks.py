# agents/tasks.py
"""
Agent background tasks.

check_wrapup_timeouts  — runs every 5 seconds
  For every agent in wrapup whose auto_wrapup timeout has elapsed:
    1. Apply the default disposition (server-side)
    2. Push 'wrapup_expired' event via WS
    3. Set agent back to ready

cleanup_zombie_agents  — runs every 60 seconds
  Agents whose last_heartbeat is > ZOMBIE_TIMEOUT seconds ago
  are marked offline. This handles browser crashes / network drops.
"""
import logging
from celery import shared_task
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger('dialflow')


@shared_task(name='agents.tasks.check_wrapup_timeouts', max_retries=0)
def check_wrapup_timeouts():
    """
    Find all agents in wrapup whose campaign auto_wrapup timeout has elapsed.
    Apply default disposition server-side and return agent to ready.
    Runs every 5 seconds via Celery beat.
    """
    from agents.models import AgentStatus, CallDisposition
    from campaigns.models import Disposition
    from calls.models import CallLog
    from core.ws_utils import send_to_agent, wrapup_expired_event

    now       = timezone.now()
    processed = 0

    agents_in_wrapup = AgentStatus.objects.filter(
        status='wrapup',
        wrapup_started_at__isnull=False,
        active_campaign__auto_wrapup_enabled=True,
    ).select_related('user', 'active_campaign', 'active_campaign__auto_wrapup_disposition')

    for agent_status in agents_in_wrapup:
        campaign = agent_status.active_campaign
        if not campaign:
            continue

        elapsed = (now - agent_status.wrapup_started_at).total_seconds()
        timeout = campaign.auto_wrapup_timeout

        if elapsed < timeout:
            # Not yet expired — send a countdown warning if close
            remaining = int(timeout - elapsed)
            if remaining <= 15 and remaining % 5 == 0:
                from core.ws_utils import wrapup_timeout_warning_event
                send_to_agent(agent_status.user_id, wrapup_timeout_warning_event(
                    agent_id=agent_status.user_id,
                    seconds_remaining=remaining,
                ))
            continue

        # Timeout expired — apply auto disposition
        default_disp = campaign.auto_wrapup_disposition
        if not default_disp:
            # Fallback: find or create a "Not Dispositioned" disposition
            default_disp, _ = Disposition.objects.get_or_create(
                name='Not Dispositioned',
                defaults={
                    'category':  'other',
                    'outcome':   'recycle',
                    'color':     '#9CA3AF',
                    'is_system': True,
                }
            )

        # Apply disposition to the pending call log
        if agent_status.wrapup_call_log_id:
            try:
                call_log = CallLog.objects.select_related('lead', 'campaign').get(
                    id=agent_status.wrapup_call_log_id
                )
                CallDisposition.objects.update_or_create(
                    call_log=call_log,
                    defaults={
                        'agent':        agent_status.user,
                        'campaign':     campaign,
                        'lead':         call_log.lead,
                        'disposition':  default_disp,
                        'notes':        'Auto-wrapup timeout applied by system',
                    }
                )
                call_log.disposition = default_disp
                call_log.save(update_fields=['disposition', 'updated_at'])

            except CallLog.DoesNotExist:
                logger.warning(f'Auto-wrapup: CallLog {agent_status.wrapup_call_log_id} not found')

        # Push expired event to agent browser
        send_to_agent(agent_status.user_id, wrapup_expired_event(
            agent_id=agent_status.user_id,
            disposition_applied=default_disp.name,
        ))

        # Return agent to ready state
        agent_status.go_ready()
        processed += 1
        logger.info(
            f'Auto-wrapup applied for agent {agent_status.user.username}: '
            f'disposition={default_disp.name}'
        )

    return {'processed': processed}


@shared_task(name='agents.tasks.cleanup_zombie_agents', max_retries=0)
def cleanup_zombie_agents():
    """
    Mark agents offline if their heartbeat is too old.
    Handles browser crashes, network drops, and stale sessions.
    Runs every 60 seconds.
    """
    from agents.models import AgentStatus
    from core.ws_utils import send_to_agent, broadcast_supervisor

    zombie_timeout = settings.DIALER.get('ZOMBIE_TIMEOUT', 120)
    cutoff         = timezone.now() - timezone.timedelta(seconds=zombie_timeout)
    cleaned        = 0

    zombies = AgentStatus.objects.filter(
        status__in=['ready', 'break', 'training'],  # not on_call or wrapup — those are protected
        last_heartbeat__lt=cutoff,
        last_heartbeat__isnull=False,
    ).select_related('user')

    for agent_status in zombies:
        lag = (timezone.now() - agent_status.last_heartbeat).total_seconds()
        logger.warning(
            'Zombie cleanup: %s last_heartbeat=%s lag=%.0fs → forcing offline',
            agent_status.user.username, agent_status.last_heartbeat, lag,
        )
        agent_status.go_offline()
        cleaned += 1

        # Notify the agent's browser tab (if still open) so it can show a
        # "session expired" message rather than silently going stale.
        try:
            send_to_agent(
                agent_status.user_id,
                {
                    'type':   'force_logout',
                    'reason': f'Session expired — no heartbeat for {int(lag)}s.',
                },
            )
        except Exception:
            pass

        # Let the supervisor monitor know an agent went zombie-offline.
        try:
            broadcast_supervisor({
                'type':     'agent_status_changed',
                'agent_id': agent_status.user_id,
                'status':   'offline',
                'reason':   'zombie_timeout',
            })
        except Exception:
            pass

    if cleaned:
        logger.info('Zombie cleanup complete: %d agent(s) set offline', cleaned)

    return {'cleaned': cleaned}


@shared_task(name='agents.tasks.reset_daily_stats', max_retries=0)
def reset_daily_stats():
    """Reset per-agent daily counters. Runs at midnight via Celery beat."""
    from agents.models import AgentStatus
    updated = AgentStatus.objects.all().update(
        calls_today=0,
        talk_time_today=0,
        break_time_today=0,
    )
    logger.info(f'Daily stats reset for {updated} agents.')
    return {'reset': updated}
