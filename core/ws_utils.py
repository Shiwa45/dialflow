# core/ws_utils.py
"""
Shared WebSocket broadcast helpers.

Usage (from any sync Django code):
    from core.ws_utils import send_to_agent, broadcast_supervisor

Usage (from async code):
    await channel_layer.group_send(...)
"""
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger('dialflow')


def _layer():
    return get_channel_layer()


# ── Group name helpers ────────────────────────────────────────────────────────

def agent_group(agent_id: int) -> str:
    return f'agent_{agent_id}'


def campaign_group(campaign_id: int) -> str:
    return f'campaign_{campaign_id}'


SUPERVISOR_GROUP = 'supervisors'


# ── Sync senders (safe to call from views, Celery tasks, signals) ─────────────

def send_to_agent(agent_id: int, message: dict):
    """Push a WebSocket message to a specific agent."""
    try:
        async_to_sync(_layer().group_send)(
            agent_group(agent_id),
            {'type': 'agent.message', 'payload': message},
        )
    except Exception as e:
        logger.warning(f'WS send_to_agent({agent_id}) failed: {e}')


def broadcast_supervisor(message: dict):
    """Push a message to all connected supervisors."""
    try:
        async_to_sync(_layer().group_send)(
            SUPERVISOR_GROUP,
            {'type': 'supervisor.message', 'payload': message},
        )
    except Exception as e:
        logger.warning(f'WS broadcast_supervisor failed: {e}')


def broadcast_campaign(campaign_id: int, message: dict):
    """Push a message to all listeners of a campaign."""
    try:
        async_to_sync(_layer().group_send)(
            campaign_group(campaign_id),
            {'type': 'campaign.message', 'payload': message},
        )
    except Exception as e:
        logger.warning(f'WS broadcast_campaign({campaign_id}) failed: {e}')


# ── Common event builders ─────────────────────────────────────────────────────

def agent_status_event(agent_id: int, status: str, display: str, **extra):
    return {
        'type': 'status_changed',
        'agent_id': agent_id,
        'status': status,
        'display': display,
        **extra,
    }


def call_incoming_event(call_id: str, lead: dict, campaign: dict):
    return {
        'type': 'call_incoming',
        'call_id': call_id,
        'lead': lead,
        'campaign': campaign,
    }


def call_connected_event(call_id: str, lead: dict, bridge_id: str):
    return {
        'type': 'call_connected',
        'call_id': call_id,
        'bridge_id': bridge_id,
        'lead': lead,
    }


def call_ended_event(call_id: str, needs_disposition: bool = True, call_log_id: int | None = None):
    payload = {
        'type': 'call_ended',
        'call_id': call_id,
        'needs_disposition': needs_disposition,
    }
    if call_log_id is not None:
        payload['call_log_id'] = call_log_id
    return payload


def wrapup_timeout_warning_event(agent_id: int, seconds_remaining: int):
    return {
        'type': 'wrapup_timeout_warning',
        'agent_id': agent_id,
        'seconds_remaining': seconds_remaining,
    }


def wrapup_expired_event(agent_id: int, disposition_applied: str):
    return {
        'type': 'wrapup_expired',
        'agent_id': agent_id,
        'disposition_applied': disposition_applied,
    }
