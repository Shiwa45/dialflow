# campaigns/consumers.py
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from core.ws_utils import SUPERVISOR_GROUP, campaign_group

logger = logging.getLogger('dialflow')


class SupervisorConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for supervisor dashboard.
    Receives live stats for all campaigns and all agents.
    Auth: user must be admin or supervisor.
    """

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return
        if not (user.is_supervisor or user.is_admin):
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(SUPERVISOR_GROUP, self.channel_name)
        await self.accept()
        logger.info(f'Supervisor WS connected: {user.username}')

        # Send current snapshot on connect
        await self.send_snapshot()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(SUPERVISOR_GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Supervisor can send control commands (pause/start campaign etc.)."""
        try:
            data    = json.loads(text_data or '{}')
            action  = data.get('action')
            if action == 'ping':
                await self.send(text_data=json.dumps({'type': 'pong'}, default=str))
        except Exception as e:
            logger.warning(f'SupervisorConsumer.receive error: {e}')

    # ── Group message handlers ────────────────────────────────────────────────

    async def supervisor_message(self, event):
        """Forward any supervisor-group broadcast to this socket."""
        await self.send(text_data=json.dumps(event['payload'], default=str))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def send_snapshot(self):
        """Push current state of all campaigns + agents on connect."""
        from asgiref.sync import sync_to_async
        snapshot = await sync_to_async(self._build_snapshot)()
        await self.send(text_data=json.dumps({'type': 'snapshot', **snapshot}, default=str))

    def _build_snapshot(self):
        from campaigns.models import Campaign
        from agents.models import AgentStatus

        campaigns = list(Campaign.objects.filter(
            status__in=[Campaign.STATUS_ACTIVE, Campaign.STATUS_PAUSED]
        ).values('id', 'name', 'status', 'dial_mode',
                 'stat_calls_today', 'stat_answered_today',
                 'stat_abandon_rate', 'stat_agents_active'))

        agents = list(AgentStatus.objects.select_related('user').filter(
            status__in=['ready', 'on_call', 'wrapup', 'break']
        ).values(
            'user_id', 'user__username', 'user__first_name', 'user__last_name',
            'status', 'active_campaign_id', 'call_started_at', 'status_changed_at',
        ))

        return {'campaigns': campaigns, 'agents': agents}


class CampaignConsumer(AsyncWebsocketConsumer):
    """Per-campaign stat stream. Used by campaign detail page."""

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.campaign_id = self.scope['url_route']['kwargs']['campaign_id']
        await self.channel_layer.group_add(
            campaign_group(self.campaign_id), self.channel_name
        )
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(
            campaign_group(self.campaign_id), self.channel_name
        )

    async def campaign_message(self, event):
        await self.send(text_data=json.dumps(event['payload'], default=str))
