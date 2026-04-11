# agents/consumers.py
"""
AgentConsumer — WebSocket for the agent dashboard.

One connection per agent browser tab.
Handles:
  - Status changes (ready / break / wrapup)
  - Heartbeat pings
  - Incoming call notifications
  - Disposition submission
  - WS messages from ARI worker / Celery tasks

All state changes are written to DB first, then broadcast.
No virtual state in JS. No polling.
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q, Count, Sum
from asgiref.sync import sync_to_async
from django.utils import timezone

from core.ws_utils import agent_group, SUPERVISOR_GROUP

logger = logging.getLogger('dialflow')


class AgentConsumer(AsyncWebsocketConsumer):

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return
        if not user.is_agent:
            await self.close(code=4003)
            return

        self.user    = user
        self.group   = agent_group(user.pk)

        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

        logger.info(f'Agent WS connected: {user.username}')

        # Recover stranded wrapup: if the agent is in wrapup with no call log
        # (the call log was never written or was cleaned up) there is nothing to
        # disposition — auto-restore to ready so they aren't blocked forever.
        await sync_to_async(self._recover_stranded_wrapup)()

        # If the agent was ready, temporarily set them offline to prevent the
        # dialer picking them up before their SIP phone re-registers.
        # The snapshot includes restore_to='ready' so the JS auto-restores once
        # JsSIP fires 'registered'.
        restore_to_ready = await sync_to_async(self._park_if_ready)()

        await self.send_snapshot(restore_to_ready=restore_to_ready)

    async def disconnect(self, code):
        group = getattr(self, 'group', None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)
        logger.info(f'Agent WS disconnected: {getattr(self, "user", "unknown")} code={code}')

    # ── Receive from browser ──────────────────────────────────────────────────

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data   = json.loads(text_data or '{}')
            action = data.get('action', '')

            handlers = {
                'ping':         self.handle_ping,
                'heartbeat':    self.handle_heartbeat,
                'set_status':   self.handle_set_status,
                'set_campaign': self.handle_set_campaign,
                'dispose':      self.handle_dispose,
                'hangup':       self.handle_hangup,
            }
            handler = handlers.get(action)
            if handler:
                await handler(data)
            else:
                logger.debug(f'Unknown WS action from agent {self.user.pk}: {action}')

        except json.JSONDecodeError:
            await self._send({'type': 'error', 'message': 'Invalid JSON'})
        except Exception as e:
            logger.exception(f'AgentConsumer.receive error: {e}')
            await self._send({'type': 'error', 'message': 'Server error'})

    # ── Action handlers ───────────────────────────────────────────────────────

    async def handle_ping(self, data):
        await self._send({'type': 'pong', 'ts': timezone.now().isoformat()})

    async def handle_heartbeat(self, data):
        """Update last_heartbeat in DB — prevents zombie cleanup from marking agent offline."""
        await sync_to_async(self._update_heartbeat)()
        await self._send({'type': 'heartbeat_ack', 'ts': timezone.now().isoformat()})

    async def handle_set_status(self, data):
        """Agent manually changes their status (break, ready)."""
        new_status = data.get('status', '')
        allowed    = ('ready', 'break', 'training')
        if new_status not in allowed:
            await self._send({'type': 'error', 'message': f'Cannot set status to {new_status}'})
            return

        agent_status = await sync_to_async(self._get_agent_status)()
        if not agent_status:
            return

        # Don't allow status change while on call or in wrapup
        if agent_status.status in ('on_call', 'wrapup'):
            await self._send({
                'type': 'error',
                'message': f'Cannot change status while {agent_status.get_status_display()}'
            })
            return

        await sync_to_async(self._set_status)(new_status)

    async def handle_set_campaign(self, data):
        """Agent selects which campaign to work on."""
        campaign_id = data.get('campaign_id')
        if not campaign_id:
            return

        valid = await sync_to_async(self._validate_campaign_assignment)(campaign_id)
        if not valid:
            await self._send({'type': 'error', 'message': 'Not assigned to that campaign'})
            return

        await sync_to_async(self._assign_campaign)(campaign_id)
        await self._send({'type': 'campaign_set', 'campaign_id': campaign_id})

    async def handle_dispose(self, data):
        """
        Agent submits a disposition after a call.
        Writes CallDisposition, updates lead, clears wrapup state.
        """
        disposition_id = data.get('disposition_id')
        call_log_id    = data.get('call_log_id')
        notes          = data.get('notes', '').strip()
        callback_at    = data.get('callback_at')  # ISO string or null

        if not disposition_id:
            await self._send({'type': 'error', 'message': 'disposition_id required'})
            return

        # Be resilient to stale/missing frontend call ids after reconnect/refresh.
        try:
            call_log_id = int(call_log_id) if call_log_id is not None else None
        except Exception:
            call_log_id = None

        if not call_log_id:
            call_log_id = await sync_to_async(self._get_pending_wrapup_call_log_id)()

        if not call_log_id:
            await self._send({'type': 'error', 'message': 'No pending wrapup call found'})
            return

        result = await sync_to_async(self._save_disposition)(
            disposition_id, call_log_id, notes, callback_at
        )

        if result.get('success'):
            logger.info(f"Disposition saved for agent {self.user.username}, call {call_log_id}")
            await self._send({'type': 'dispose_ok', 'call_log_id': call_log_id})
        else:
            err = result.get('error', 'Disposition failed')
            logger.warning(f"Disposition failed for agent {self.user.username}: {err}")
            await self._send({'type': 'error', 'message': err})

    async def handle_hangup(self, data):
        """Agent-initiated hangup — tells ARI to hang up the bridge."""
        channel_id = data.get('channel_id') or await sync_to_async(self._get_active_channel)()
        if channel_id:
            await sync_to_async(self._ari_hangup)(channel_id)
        await self._send({'type': 'hangup_sent'})

    # ── Group message handlers (from ARI worker / Celery tasks) ──────────────

    async def agent_message(self, event):
        """Forward any group broadcast to this socket."""
        await self._send(event['payload'])

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send(self, data: dict):
        try:
            await self.send(text_data=json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f'AgentConsumer._send failed: {e}')

    async def send_snapshot(self, restore_to_ready: bool = False):
        """Push full current state to a freshly-connected agent."""
        snapshot = await sync_to_async(self._build_snapshot)()
        if restore_to_ready:
            snapshot['restore_to'] = 'ready'
        await self._send({'type': 'snapshot', **snapshot})

    # ── DB operations (called via sync_to_async) ──────────────────────────────

    def _park_if_ready(self) -> bool:
        """
        If the agent is currently ready, set them to offline temporarily.
        The dialer only picks up ready agents, so this prevents them from
        receiving a call before their SIP phone re-registers after page reload.
        Returns True if an agent should be restored to ready once JsSIP registers.

        Two cases handled:
          1. Agent is 'ready' → park to 'offline' now, return True.
          2. Agent is already 'offline' (parked by a previous WS connect that
             dropped before JsSIP finished registering) → don't re-park, but
             still return True so the snapshot includes restore_to='ready'.
             Heuristic: offline within the last 90 s = mid-reconnect window.
        """
        from agents.models import AgentStatus
        from core.ws_utils import broadcast_supervisor
        from datetime import timedelta

        # Case 1: agent is ready — park them
        parked = AgentStatus.objects.filter(
            user=self.user, status='ready',
        ).update(
            status='offline',
            status_changed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        if parked:
            broadcast_supervisor({
                'type':     'agent_status_changed',
                'agent_id': self.user.id,
                'status':   'offline',
                'reason':   'sip_reconnecting',
            })
            return True

        # Case 2: already offline — check if it was a recent park (WS reconnect)
        # If the agent went offline within the last 90 seconds they are almost
        # certainly in the middle of a reconnect cycle, not manually offline.
        recently_parked = AgentStatus.objects.filter(
            user=self.user,
            status='offline',
            status_changed_at__gte=timezone.now() - timedelta(seconds=90),
        ).exists()
        return recently_parked

    def _recover_stranded_wrapup(self):
        """
        If an agent is in wrapup but has no pending call log id, there is
        nothing to disposition — auto-restore to ready so the agent isn't
        blocked indefinitely.  This can happen when:
          • The page was refreshed after the call log was already cleaned up.
          • ARI wrapup state was set but the call log write failed.
        """
        from agents.models import AgentStatus
        qs = AgentStatus.objects.filter(
            user=self.user,
            status='wrapup',
            wrapup_call_log_id__isnull=True,
            active_call_log_id__isnull=True,
        )
        if qs.exists():
            logger.info(
                'Recovering stranded wrapup for %s (no call log) — restoring to ready.',
                self.user.username,
            )
            qs.first().go_ready()

    def _get_agent_status(self):
        from agents.models import AgentStatus
        status, _ = AgentStatus.objects.select_related(
            'user', 'active_campaign'
        ).get_or_create(user=self.user)
        return status

    def _update_heartbeat(self):
        from agents.models import AgentStatus
        AgentStatus.objects.filter(user=self.user).update(last_heartbeat=timezone.now())

    def _set_status(self, new_status: str):
        agent_status = self._get_agent_status()
        method_map = {
            'ready':    agent_status.go_ready,
            'break':    agent_status.go_break,
            'training': lambda: agent_status.set_status('training'),
        }
        method_map[new_status]()

    def _assign_campaign(self, campaign_id: int):
        from agents.models import AgentStatus
        AgentStatus.objects.filter(user=self.user).update(active_campaign_id=campaign_id)

    def _get_assigned_campaigns(self):
        from campaigns.models import Campaign
        return list(
            Campaign.objects.filter(
                agents__agent=self.user,
                agents__is_active=True,
                status__in=['active', 'paused'],
            ).order_by('id')
        )

    def _ensure_active_campaign(self, agent_status):
        assigned_campaigns = self._get_assigned_campaigns()
        valid_ids = {campaign.id for campaign in assigned_campaigns}
        if agent_status.active_campaign_id in valid_ids:
            return assigned_campaigns

        new_campaign = assigned_campaigns[0] if len(assigned_campaigns) == 1 else None
        if agent_status.active_campaign_id != (new_campaign.id if new_campaign else None):
            agent_status.active_campaign = new_campaign
            agent_status.save(update_fields=['active_campaign', 'updated_at'])
        return assigned_campaigns

    def _validate_campaign_assignment(self, campaign_id: int) -> bool:
        from campaigns.models import CampaignAgent
        return CampaignAgent.objects.filter(
            campaign_id=campaign_id,
            agent=self.user,
            is_active=True,
        ).exists()

    def _get_active_channel(self):
        from agents.models import AgentStatus
        try:
            return AgentStatus.objects.get(user=self.user).active_channel_id
        except Exception:
            return ''

    def _get_pending_wrapup_call_log_id(self):
        from agents.models import AgentStatus
        try:
            status = AgentStatus.objects.get(user=self.user)
            return status.wrapup_call_log_id or status.active_call_log_id
        except Exception:
            return None

    def _ari_hangup(self, channel_id: str):
        """Fire hangup via ARI REST."""
        from telephony.models import AsteriskServer
        import requests
        server = AsteriskServer.objects.filter(is_active=True).first()
        if not server:
            return
        try:
            requests.delete(
                f'http://{server.ari_host}:{server.ari_port}/ari/channels/{channel_id}',
                auth=(server.ari_username, server.ari_password),
                timeout=3,
            )
        except Exception as e:
            logger.warning(f'ARI hangup failed for {channel_id}: {e}')

    def _save_disposition(self, disposition_id: int, call_log_id: int,
                          notes: str, callback_at) -> dict:
        from agents.models import AgentStatus, CallDisposition
        from campaigns.models import Disposition
        from calls.models import CallLog

        try:
            call_log    = CallLog.objects.select_related('lead', 'campaign').get(id=call_log_id)
            disposition = Disposition.objects.get(id=disposition_id)
            agent_status = AgentStatus.objects.get(user=self.user)

            # Validate this is the agent's own wrapup call
            if (agent_status.wrapup_call_log_id and
                    agent_status.wrapup_call_log_id != call_log_id):
                return {'success': False, 'error': 'Call log mismatch'}

            # Parse callback time
            cb_time = None
            if callback_at:
                from datetime import datetime
                try:
                    cb_time = datetime.fromisoformat(callback_at)
                except Exception:
                    pass

            # Create disposition record
            CallDisposition.objects.update_or_create(
                call_log=call_log,
                defaults={
                    'agent':        self.user,
                    'campaign':     call_log.campaign,
                    'lead':         call_log.lead,
                    'disposition':  disposition,
                    'notes':        notes,
                    'callback_at':  cb_time,
                }
            )

            # Update call log
            call_log.disposition    = disposition
            call_log.agent_notes    = notes
            call_log.save(update_fields=['disposition', 'agent_notes', 'updated_at'])

            # Handle outcomes
            self._apply_disposition_outcome(call_log, disposition, agent_status)

            # Return agent to ready state
            agent_status.go_ready()

            return {'success': True}

        except CallLog.DoesNotExist:
            return {'success': False, 'error': 'Call log not found'}
        except Disposition.DoesNotExist:
            return {'success': False, 'error': 'Disposition not found'}
        except Exception as e:
            logger.exception(f'Disposition save error: {e}')
            return {'success': False, 'error': str(e)}

    def _apply_disposition_outcome(self, call_log, disposition, agent_status):
        """Post-disposition lead handling based on outcome."""
        from campaigns.models import HopperEntry
        from leads.models import LeadAttempt

        lead     = call_log.lead
        campaign = call_log.campaign
        outcome  = disposition.outcome

        if not lead:
            return

        # Record attempt
        prev_attempts = LeadAttempt.objects.filter(lead=lead, campaign=campaign).count()
        LeadAttempt.objects.create(
            lead           = lead,
            campaign       = campaign,
            attempt_number = prev_attempts + 1,
            phone_number   = call_log.phone_number,
            call_log       = call_log,
            result         = disposition.category,
        )

        # Mark hopper entry complete
        HopperEntry.objects.filter(
            campaign=campaign, lead=lead,
            status__in=['dialing', 'answered'],
        ).update(status=HopperEntry.STATUS_COMPLETED)

        if outcome == 'dnc':
            lead.mark_dnc(campaign=campaign, added_by=self.user, reason=f'Agent: {disposition.name}')

        elif outcome == 'complete':
            lead.is_active = False
            lead.save(update_fields=['is_active', 'updated_at'])

        elif outcome == 'callback' and hasattr(call_log, 'disposition_record'):
            cb_at = call_log.disposition_record.callback_at
            if cb_at:
                # Re-queue with future timestamp — handled by hopper fill
                HopperEntry.objects.create(
                    campaign    = campaign,
                    lead        = lead,
                    phone_number= lead.primary_phone,
                    status      = HopperEntry.STATUS_QUEUED,
                    priority    = 1,  # callbacks are high priority
                )

        # outcome == 'recycle': hopper fill will handle it at next retry_delay

    def _build_snapshot(self) -> dict:
        """Full state snapshot for dashboard restoration on page load / reconnect."""
        from agents.models import AgentStatus
        from campaigns.models import Campaign, Disposition
        from calls.models import CallLog

        agent_status = self._get_agent_status()
        assigned_campaigns = self._ensure_active_campaign(agent_status)

        # Campaigns this agent is assigned to
        assigned_campaigns_data = [
            {
                'id': campaign.id,
                'name': campaign.name,
                'status': campaign.status,
                'dial_mode': campaign.dial_mode,
                'auto_wrapup_enabled': campaign.auto_wrapup_enabled,
                'auto_wrapup_timeout': campaign.auto_wrapup_timeout,
            }
            for campaign in assigned_campaigns
        ]

        # Dispositions for active campaign
        dispositions = []
        if agent_status.active_campaign_id:
            dispositions = list(
                Disposition.objects.filter(
                    campaigns__id=agent_status.active_campaign_id,
                    is_active=True,
                ).order_by('sort_order', 'name').values(
                    'id', 'name', 'category', 'outcome', 'color', 'hotkey'
                )
            )

        # Wrapup state — keep disposition flow recoverable across page refresh.
        pending_call = None
        pending_call_log_id = agent_status.wrapup_call_log_id or agent_status.active_call_log_id
        if agent_status.status == 'wrapup' and pending_call_log_id:
            try:
                cl = CallLog.objects.select_related('lead').get(id=pending_call_log_id)
                pending_call = {
                    'id':          cl.id,
                    'lead_id':     cl.lead_id,
                    'lead_name':   cl.lead.full_name if cl.lead else '',
                    'phone':       cl.phone_number,
                    'duration':    cl.duration,
                    'campaign_id': cl.campaign_id,
                }
                pending_call_log_id = cl.id
            except Exception:
                pass

        # Today's stats
        from django.utils import timezone as tz
        today_stats = CallLog.objects.filter(
            agent=self.user,
            started_at__date=tz.now().date(),
        ).aggregate(
            total    = Count('id'),
            answered = Count('id', filter=Q(status='completed')),
            talk_sec = Sum('duration'),
        )

        return {
            'status':             agent_status.status,
            'status_display':     agent_status.get_status_display(),
            'status_since':       agent_status.status_changed_at.isoformat(),
            'active_campaign_id': agent_status.active_campaign_id,
            'campaigns':          assigned_campaigns_data,
            'dispositions':       dispositions,
            'wrapup_seconds_remaining': agent_status.get_wrapup_seconds_remaining(),
            'pending_call_log_id': pending_call_log_id,
            'pending_call':       pending_call,
            'stats_today': {
                'calls':    today_stats['total']    or 0,
                'answered': today_stats['answered'] or 0,
                'talk_sec': today_stats['talk_sec'] or 0,
            },
        }
