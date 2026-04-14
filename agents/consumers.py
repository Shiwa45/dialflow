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

FIXES applied:
  1. _build_snapshot now includes login_since, call_started_at for DB-driven timers
  2. _set_agent_wrapup sends `since` in WS event
  3. status_changed events always include `since` from DB
  4. Snapshot includes active call info when agent is on_call (for page refresh mid-call)
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

        await sync_to_async(self._recover_wrapup_state)()
        await sync_to_async(self._recover_stale_live_call_state)()

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
        await sync_to_async(self._update_heartbeat)()
        await self._send({'type': 'heartbeat_ack', 'ts': timezone.now().isoformat()})

    async def handle_set_status(self, data):
        new_status = data.get('status', '')
        pause_code_id = data.get('pause_code_id')
        force = bool(data.get('force'))
        result = await sync_to_async(self._change_status)(new_status, pause_code_id, force)
        if result.get('error'):
            await self._send({'type': 'error', 'message': result['error']})

    async def handle_set_campaign(self, data):
        campaign_id = data.get('campaign_id')
        result = await sync_to_async(self._set_campaign)(campaign_id)
        if result.get('error'):
            await self._send({'type': 'error', 'message': result['error']})
        else:
            await self._send({'type': 'campaign_set', 'campaign_id': campaign_id})

    async def handle_dispose(self, data):
        disposition_id = data.get('disposition_id')
        call_log_id    = data.get('call_log_id')
        notes          = data.get('notes', '').strip()
        callback_at    = data.get('callback_at')

        if not disposition_id:
            await self._send({'type': 'error', 'message': 'disposition_id required'})
            return

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
        channel_id = data.get('channel_id') or await sync_to_async(self._get_active_channel)()
        if not channel_id:
            await self._send({'type': 'error', 'message': 'No active channel to hang up'})
            return
        try:
            await sync_to_async(self._ari_hangup)(channel_id)
            await self._send({'type': 'hangup_sent'})
        except Exception as e:
            logger.exception(f'Hangup failed for {self.user.username}: {e}')
            await self._send({'type': 'error', 'message': 'Unable to disconnect this call'})

    # ── Group message handlers ────────────────────────────────────────────────

    async def agent_message(self, event):
        await self._send(event['payload'])

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send(self, data: dict):
        try:
            await self.send(text_data=json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f'AgentConsumer._send failed: {e}')

    async def send_snapshot(self, restore_to_ready: bool = False):
        snapshot = await sync_to_async(self._build_snapshot)()
        if restore_to_ready:
            snapshot['restore_to'] = 'ready'
        await self._send({'type': 'snapshot', **snapshot})

    # ── DB operations (called via sync_to_async) ──────────────────────────────

    def _park_if_ready(self) -> bool:
        from agents.models import AgentStatus
        from core.ws_utils import broadcast_supervisor
        from datetime import timedelta

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

        recently_parked = AgentStatus.objects.filter(
            user=self.user,
            status='offline',
            status_changed_at__gte=timezone.now() - timedelta(seconds=90),
        ).exists()
        return recently_parked

    def _recover_wrapup_state(self):
        from agents.models import AgentStatus

        try:
            agent_status = AgentStatus.objects.get(user=self.user)
        except AgentStatus.DoesNotExist:
            return

        # Only wrapup_call_log_id should force wrapup restoration.
        # active_call_log_id may remain populated briefly during transitions.
        has_wrapup_call_log = bool(agent_status.wrapup_call_log_id)

        # Case 1: Already in wrapup but no pending call log => unblock to ready.
        if agent_status.status == 'wrapup' and not has_wrapup_call_log:
            # Backward-compatible recovery for rows where only active_call_log_id
            # was populated during wrapup.
            if agent_status.active_call_log_id:
                agent_status.wrapup_call_log_id = agent_status.active_call_log_id
                agent_status.save(update_fields=['wrapup_call_log_id', 'updated_at'])
                return
            logger.info(
                f'Stranded wrapup for {self.user.username}: no call log — auto-restoring to ready'
            )
            agent_status.go_ready()
            return

        # Case 2: Pending wrapup call exists but status drifted away from wrapup
        # (e.g. page refresh/reconnect race). Restore wrapup so disposition flow continues.
        if agent_status.status != 'wrapup' and has_wrapup_call_log:
            logger.warning(
                'Recovering lost wrapup for %s: status=%s wrapup_call_log_id=%s active_call_log_id=%s',
                self.user.username,
                agent_status.status,
                agent_status.wrapup_call_log_id,
                agent_status.active_call_log_id,
            )
            agent_status.status = 'wrapup'
            if not agent_status.wrapup_started_at:
                agent_status.wrapup_started_at = timezone.now()
            agent_status.status_changed_at = timezone.now()
            agent_status.save(update_fields=['status', 'wrapup_started_at', 'status_changed_at', 'updated_at'])

    def _recover_stale_live_call_state(self):
        """
        Heal stale ringing/on_call rows when ARI no longer has the channel.
        This runs on agent WS connect/reconnect so dashboard state does not
        remain stuck after missed ARI channel-destroy events.
        """
        from agents.models import AgentStatus
        from django.conf import settings
        import requests

        try:
            st = AgentStatus.objects.get(user=self.user)
        except AgentStatus.DoesNotExist:
            return

        if st.status not in ('ringing', 'on_call'):
            return
        if not st.active_channel_id:
            return

        cfg = settings.ASTERISK
        url = f"http://{cfg['ARI_HOST']}:{cfg['ARI_PORT']}/ari/channels"
        try:
            r = requests.get(
                url,
                auth=(cfg['ARI_USERNAME'], cfg['ARI_PASSWORD']),
                timeout=3,
            )
            if r.status_code != 200:
                return
            live_ids = {
                ch.get('id') for ch in (r.json() or [])
                if isinstance(ch, dict) and ch.get('id')
            }
        except Exception:
            return

        if st.active_channel_id in live_ids:
            return

        now = timezone.now()
        logger.warning(
            'Recovering stale %s for %s: channel %s no longer exists in ARI',
            st.status,
            self.user.username,
            st.active_channel_id,
        )

        if st.active_call_log_id:
            # Preserve disposition flow if call log is known.
            st.status = 'wrapup'
            st.wrapup_started_at = now
            st.wrapup_call_log_id = st.active_call_log_id
            st.active_channel_id = None
            st.active_lead_id = None
            st.call_started_at = None
            st.status_changed_at = now
            st.save(update_fields=[
                'status',
                'status_changed_at',
                'active_channel_id',
                'active_lead_id',
                'call_started_at',
                'wrapup_started_at',
                'wrapup_call_log_id',
                'updated_at',
            ])
            return

        st.go_ready()

    def _update_heartbeat(self):
        from agents.models import AgentStatus
        AgentStatus.objects.filter(user=self.user).update(
            last_heartbeat=timezone.now(),
            updated_at=timezone.now(),
        )

    def _change_status(self, new_status, pause_code_id=None, force=False):
        from agents.models import AgentStatus, PauseCode

        allowed = {
            'ready':    ['offline', 'break', 'training'],
            'break':    ['ready'],
            'training': ['ready'],
            'offline':  ['ready', 'break', 'training'],
        }
        allowed_from = allowed.get(new_status, [])

        try:
            agent_status = AgentStatus.objects.select_related('active_campaign').get(
                user=self.user
            )
        except AgentStatus.DoesNotExist:
            return {'error': 'AgentStatus not found'}

        # Wrap-up should normally be exited ONLY via disposition submit.
        # Allow explicit override only when the browser sends force=true
        # (used by manual "Skip wrap-up" action).
        if new_status == 'ready' and agent_status.status == 'wrapup' and force:
            pass
        elif agent_status.status not in allowed_from:
            return {'error': f'Cannot go from {agent_status.status} to {new_status}'}

        if new_status == 'break' and pause_code_id:
            try:
                pause_code = PauseCode.objects.get(id=pause_code_id, is_active=True)
            except PauseCode.DoesNotExist:
                pause_code = None
            agent_status.go_break(pause_code=pause_code)
        elif new_status == 'ready':
            agent_status.go_ready()
        elif new_status == 'training':
            agent_status.go_training()
        elif new_status == 'offline':
            agent_status.go_offline()

        return {'success': True}

    def _set_campaign(self, campaign_id):
        from agents.models import AgentStatus
        from campaigns.models import CampaignAgent

        if not campaign_id:
            AgentStatus.objects.filter(user=self.user).update(
                active_campaign=None, updated_at=timezone.now()
            )
            return {'success': True}

        try:
            campaign_id = int(campaign_id)
        except (ValueError, TypeError):
            return {'error': 'Invalid campaign_id'}

        is_assigned = CampaignAgent.objects.filter(
            agent=self.user, campaign_id=campaign_id, is_active=True
        ).exists()
        if not is_assigned:
            return {'error': 'Not assigned to this campaign'}

        AgentStatus.objects.filter(user=self.user).update(
            active_campaign_id=campaign_id, updated_at=timezone.now()
        )
        return {'success': True}

    def _get_pending_wrapup_call_log_id(self):
        from agents.models import AgentStatus
        try:
            st = AgentStatus.objects.get(user=self.user)
            return st.wrapup_call_log_id or st.active_call_log_id
        except AgentStatus.DoesNotExist:
            return None

    def _save_disposition(self, disposition_id, call_log_id, notes, callback_at):
        from agents.models import AgentStatus, CallDisposition
        from calls.models import CallLog
        from campaigns.models import Disposition
        from dateutil.parser import isoparse

        try:
            agent_status = AgentStatus.objects.select_related('active_campaign').get(
                user=self.user
            )
            call_log = CallLog.objects.select_related('lead', 'campaign').get(
                id=call_log_id
            )
            disposition = Disposition.objects.get(id=disposition_id)

            cb_time = None
            if callback_at:
                try:
                    cb_time = isoparse(callback_at)
                except Exception:
                    pass

            CallDisposition.objects.update_or_create(
                call_log=call_log,
                defaults={
                    'agent':       self.user,
                    'campaign':    call_log.campaign,
                    'lead':        call_log.lead,
                    'disposition': disposition,
                    'notes':       notes,
                    'callback_at': cb_time,
                }
            )

            call_log.disposition    = disposition
            call_log.agent_notes    = notes
            call_log.save(update_fields=['disposition', 'agent_notes', 'updated_at'])

            self._apply_disposition_outcome(call_log, disposition, agent_status)

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
        from campaigns.models import HopperEntry
        from leads.models import LeadAttempt

        lead     = call_log.lead
        campaign = call_log.campaign
        outcome  = disposition.outcome

        if not lead:
            return

        prev_attempts = LeadAttempt.objects.filter(lead=lead, campaign=campaign).count()
        LeadAttempt.objects.create(
            lead           = lead,
            campaign       = campaign,
            attempt_number = prev_attempts + 1,
            phone_number   = call_log.phone_number,
            call_log       = call_log,
            result         = disposition.category,
        )

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
                HopperEntry.objects.create(
                    campaign    = campaign,
                    lead        = lead,
                    phone_number= lead.primary_phone,
                    status      = HopperEntry.STATUS_QUEUED,
                    priority    = 1,
                )

    def _build_snapshot(self) -> dict:
        """Full state snapshot for dashboard restoration on page load / reconnect.

        FIX: Now includes:
          - login_since: from AgentLoginLog (DB, not client-side new Date())
          - call_started_at: for restoring call timer on page refresh
          - active_call: full call info when agent is on_call (so dashboard shows call mid-refresh)
          - server_time: current server timestamp for client clock-sync
        """
        from agents.models import AgentStatus, AgentLoginLog
        from calls.models import CallLog
        from campaigns.models import CampaignAgent, Disposition

        agent_status, _ = AgentStatus.objects.get_or_create(user=self.user)

        # Assigned campaigns
        assigned_ids = list(
            CampaignAgent.objects.filter(
                agent=self.user, is_active=True
            ).select_related('campaign').values_list('campaign_id', flat=True)
        )
        assigned_campaigns_data = []
        if assigned_ids:
            from campaigns.models import Campaign
            for c in Campaign.objects.filter(id__in=assigned_ids):
                assigned_campaigns_data.append({
                    'id':                   c.id,
                    'name':                 c.name,
                    'auto_wrapup_enabled':  c.auto_wrapup_enabled,
                    'auto_wrapup_timeout':  c.wrapup_timeout,
                })

        # Dispositions for the active campaign (or all if none selected)
        disp_qs = Disposition.objects.filter(is_active=True)
        if agent_status.active_campaign_id:
            # Campaign-specific dispositions first, then global ones
            campaign_disps = disp_qs.filter(
                Q(campaigns=agent_status.active_campaign_id) | Q(campaigns__isnull=True)
            ).distinct()
            dispositions = list(campaign_disps.values(
                'id', 'name', 'category', 'outcome', 'color', 'hotkey',
            ))
        else:
            dispositions = list(disp_qs.values(
                'id', 'name', 'category', 'outcome', 'color', 'hotkey',
            ))

        # Pending wrapup call
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

        # ── FIX: Active call info for ringing/on_call state (page refresh mid-call) ──
        active_call = None
        if agent_status.status in ('ringing', 'on_call') and agent_status.active_lead_id:
            try:
                from leads.models import Lead
                lead = Lead.objects.get(id=agent_status.active_lead_id)
                active_call = {
                    'call_id':    agent_status.active_channel_id,
                    'lead': {
                        'id':         lead.id,
                        'first_name': lead.first_name,
                        'last_name':  lead.last_name,
                        'phone':      lead.primary_phone,
                        'email':      getattr(lead, 'email', ''),
                    },
                    'campaign_id': agent_status.active_campaign_id,
                }
            except Exception:
                pass

        # Today's stats from DB
        from django.utils import timezone as tz
        today_stats = CallLog.objects.filter(
            agent=self.user,
            started_at__date=tz.now().date(),
        ).aggregate(
            total    = Count('id'),
            answered = Count('id', filter=Q(status='completed')),
            talk_sec = Sum('duration'),
        )

        # ── FIX: Login time from DB ──
        login_since = None
        try:
            latest_login = AgentLoginLog.objects.filter(
                user=self.user, logout_at__isnull=True
            ).order_by('-login_at').first()
            if latest_login:
                login_since = latest_login.login_at.isoformat()
        except Exception:
            pass

        return {
            'status':                    agent_status.status,
            'status_display':            agent_status.get_status_display(),
            'status_since':              agent_status.status_changed_at.isoformat(),
            'active_campaign_id':        agent_status.active_campaign_id,
            'campaigns':                 assigned_campaigns_data,
            'dispositions':              dispositions,
            'wrapup_seconds_remaining':  agent_status.get_wrapup_seconds_remaining(),
            'pending_call_log_id':       pending_call_log_id,
            'pending_call':              pending_call,
            'active_call':               active_call,             # ← NEW
            'call_started_at':           agent_status.call_started_at.isoformat() if agent_status.call_started_at else None,  # ← NEW
            'login_since':               login_since,             # ← NEW (from DB)
            'server_time':               tz.now().isoformat(),    # ← NEW (for clock sync)
            'stats_today': {
                'calls':    today_stats['total']    or 0,
                'answered': today_stats['answered'] or 0,
                'talk_sec': today_stats['talk_sec'] or 0,
            },
        }

    def _get_active_channel(self):
        from agents.models import AgentStatus
        try:
            st = AgentStatus.objects.get(user=self.user)
            return st.active_channel_id
        except AgentStatus.DoesNotExist:
            return None

    def _ari_hangup(self, channel_id):
        from telephony.ari_worker import ARIClient
        from django.conf import settings
        cfg = settings.ASTERISK
        client = ARIClient(
            cfg['ARI_HOST'], cfg['ARI_PORT'],
            cfg['ARI_USERNAME'], cfg['ARI_PASSWORD'],
            cfg.get('ARI_APP_NAME', 'dialflow'),
        )
        client.hangup(channel_id)

