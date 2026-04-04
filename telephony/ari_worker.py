# telephony/ari_worker.py
"""
ARI Worker — Real-time Asterisk event processor.

Auto-started by TelephonyConfig.ready() in a daemon thread.
No management command needed. Reconnects automatically on disconnect.

Event flow:
  StasisStart(autodial)   → customer answered → find available agent → bridge
  StasisStart(agent_leg)  → agent channel ready → mark agent available
  ChannelHangup           → update call log → push call_ended to agent WS
  BridgeDestroyed         → cleanup bridge state
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests
import websockets
from asgiref.sync import sync_to_async
from django.utils import timezone

logger = logging.getLogger('telephony.ari_worker')


# ─── REST helpers (synchronous, runs in executor) ─────────────────────────────

class ARIClient:
    """Thin synchronous ARI REST client."""

    def __init__(self, host, port, username, password, app_name):
        self.base = f'http://{host}:{port}/ari'
        self.auth = (username, password)
        self.app  = app_name

    def _req(self, method, path, **kwargs):
        url = f'{self.base}{path}'
        try:
            r = requests.request(method, url, auth=self.auth, timeout=5, **kwargs)
            r.raise_for_status()
            return r.json() if r.content else {}
        except Exception as e:
            logger.error(f'ARI {method} {path} failed: {e}')
            return None

    def get(self, path, **kw):    return self._req('GET',    path, **kw)
    def post(self, path, **kw):   return self._req('POST',   path, **kw)
    def delete(self, path, **kw): return self._req('DELETE', path, **kw)

    # ── Call control ──────────────────────────────────────────────────────────

    def originate(self, endpoint, context, extension, priority,
                  caller_id='', variables=None, timeout=30):
        return self.post('/channels', json={
            'endpoint':  endpoint,
            'context':   context,
            'extension': extension,
            'priority':  priority,
            'callerId':  caller_id,
            'timeout':   timeout,
            'app':       self.app,
            'variables': variables or {},
        })

    def create_bridge(self, bridge_type='mixing', name=''):
        return self.post('/bridges', json={'type': bridge_type, 'name': name})

    def add_to_bridge(self, bridge_id, channel_id):
        return self.post(f'/bridges/{bridge_id}/addChannel', json={'channel': channel_id})

    def remove_from_bridge(self, bridge_id, channel_id):
        return self.post(f'/bridges/{bridge_id}/removeChannel', json={'channel': channel_id})

    def hangup(self, channel_id, reason='normal'):
        return self.delete(f'/channels/{channel_id}', params={'reason': reason})

    def answer(self, channel_id):
        return self.post(f'/channels/{channel_id}/answer')

    def start_recording(self, channel_id, name, format='wav', max_silence=3, beep=False):
        return self.post(f'/channels/{channel_id}/record', json={
            'name':          name,
            'format':        format,
            'maxSilenceSeconds': max_silence,
            'beep':          beep,
            'ifExists':      'overwrite',
        })

    def start_bridge_recording(self, bridge_id, name, format='wav'):
        return self.post(f'/bridges/{bridge_id}/record', json={
            'name':     name,
            'format':   format,
            'ifExists': 'overwrite',
        })

    def play_sound(self, channel_id, sound_uri):
        return self.post(f'/channels/{channel_id}/play', json={'media': sound_uri})

    def get_channel_var(self, channel_id, variable):
        result = self.get(f'/channels/{channel_id}/variable', params={'variable': variable})
        return result.get('value') if result else None

    def moh_start(self, channel_id, moh_class='default'):
        return self.post(f'/channels/{channel_id}/moh', json={'mohClass': moh_class})

    def moh_stop(self, channel_id):
        return self.delete(f'/channels/{channel_id}/moh')


# ─── Event Handler ────────────────────────────────────────────────────────────

class ARIEventHandler:
    """
    Processes all ARI events received over WebSocket.

    State maps:
      active_calls   : channel_id → {type, campaign_id, lead_id, agent_id, ...}
      active_bridges : bridge_id  → {channel_ids, recording_name, ...}
      agent_channels : agent_id   → channel_id  (agent's persistent leg)
    """

    def __init__(self, client: ARIClient):
        self.ari           = client
        self.active_calls  = {}
        self.active_bridges = {}
        self.agent_channels = {}  # agent_id → channel_id

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def handle(self, event: dict):
        etype = event.get('type', '')
        handlers = {
            'StasisStart':            self.on_stasis_start,
            'StasisEnd':              self.on_stasis_end,
            'ChannelStateChange':     self.on_channel_state_change,
            'ChannelHangupRequest':   self.on_hangup_request,
            'ChannelDestroyed':       self.on_channel_destroyed,
            'BridgeCreated':          self.on_bridge_created,
            'BridgeDestroyed':        self.on_bridge_destroyed,
            'ChannelEnteredBridge':   self.on_channel_entered_bridge,
            'ChannelLeftBridge':      self.on_channel_left_bridge,
        }
        handler = handlers.get(etype)
        if handler:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(f'Error handling {etype}: {e}')
        else:
            logger.debug(f'Unhandled ARI event: {etype}')

    # ── StasisStart ───────────────────────────────────────────────────────────

    async def on_stasis_start(self, event):
        channel    = event.get('channel', {})
        channel_id = channel.get('id', '')

        # Read channel variables to understand call purpose
        vars_raw = event.get('args', [])   # ARI passes vars as positional args
        call_type      = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CALL_TYPE')      or ''
        campaign_id    = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CAMPAIGN_ID')    or ''
        lead_id        = await sync_to_async(self.ari.get_channel_var)(channel_id, 'LEAD_ID')        or ''
        agent_id       = await sync_to_async(self.ari.get_channel_var)(channel_id, 'AGENT_ID')       or ''
        customer_num   = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CUSTOMER_NUMBER') or channel.get('caller', {}).get('number', '')

        logger.info(f'StasisStart: {channel_id} type={call_type} campaign={campaign_id} lead={lead_id} agent={agent_id}')

        if call_type == 'autodial':
            await self._handle_autodial(channel_id, campaign_id, lead_id, customer_num)

        elif call_type == 'agent_leg':
            await self._handle_agent_leg(channel_id, agent_id)

    async def _handle_autodial(self, channel_id, campaign_id, lead_id, customer_num):
        """Customer call answered by Asterisk. Find best available agent and bridge."""
        self.active_calls[channel_id] = {
            'type':        'customer',
            'campaign_id': campaign_id,
            'lead_id':     lead_id,
            'customer_num': customer_num,
            'state':       'answered',
            'answered_at': timezone.now(),
        }

        # Update call log in DB
        await sync_to_async(self._update_call_log)(channel_id, 'answered', lead_id, campaign_id)

        # Play hold music while we find an agent
        await sync_to_async(self.ari.moh_start)(channel_id)

        # Find an available agent from DB (real status — not virtual)
        agent = await sync_to_async(self._find_available_agent)(campaign_id)

        if agent is None:
            logger.warning(f'No available agent for campaign {campaign_id}. Call {channel_id} will be dropped.')
            await sync_to_async(self._mark_call_dropped)(channel_id)
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        await self._bridge_customer_to_agent(channel_id, agent, campaign_id, lead_id)

    async def _handle_agent_leg(self, channel_id, agent_id):
        """Agent's persistent channel is connected. Mark agent as ready."""
        if agent_id:
            self.agent_channels[int(agent_id)] = channel_id
            await sync_to_async(self._set_agent_status)(int(agent_id), 'ready', channel_id)
            logger.info(f'Agent {agent_id} channel {channel_id} registered as ready.')

    async def _bridge_customer_to_agent(self, channel_id, agent, campaign_id, lead_id):
        """Create a bridge, add customer + agent, start recording."""
        agent_channel_id = self.agent_channels.get(agent.id)
        if not agent_channel_id:
            logger.error(f'Agent {agent.id} has no active channel.')
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        # Stop hold music
        await sync_to_async(self.ari.moh_stop)(channel_id)

        # Create bridge
        bridge_name   = f'bridge_{campaign_id}_{lead_id}_{int(time.time())}'
        bridge_result = await sync_to_async(self.ari.create_bridge)(name=bridge_name)
        if not bridge_result:
            logger.error('Failed to create ARI bridge.')
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        bridge_id = bridge_result.get('id')

        # Add both channels
        await sync_to_async(self.ari.add_to_bridge)(bridge_id, channel_id)
        await sync_to_async(self.ari.add_to_bridge)(bridge_id, agent_channel_id)

        self.active_bridges[bridge_id] = {
            'customer_channel': channel_id,
            'agent_channel':    agent_channel_id,
            'agent_id':         agent.id,
            'campaign_id':      campaign_id,
            'lead_id':          lead_id,
            'created_at':       timezone.now(),
        }
        self.active_calls[channel_id]['bridge_id'] = bridge_id
        self.active_calls[channel_id]['agent_id']  = agent.id

        # Mark agent as on-call in DB
        await sync_to_async(self._set_agent_on_call)(agent.id, channel_id, lead_id, campaign_id)

        # Start recording if campaign has it enabled
        recording_name = await sync_to_async(self._start_recording)(
            bridge_id, campaign_id, lead_id
        )
        if recording_name:
            self.active_bridges[bridge_id]['recording_name'] = recording_name

        # Push call_connected to agent via WebSocket
        lead_info = await sync_to_async(self._get_lead_info)(lead_id)
        await sync_to_async(self._ws_call_connected)(agent.id, channel_id, bridge_id, lead_info)

        logger.info(f'Bridge {bridge_id} created: customer={channel_id} ↔ agent={agent_channel_id}')

    # ── ChannelDestroyed ──────────────────────────────────────────────────────

    async def on_channel_destroyed(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        call_data  = self.active_calls.pop(channel_id, None)

        if not call_data:
            return

        logger.info(f'Channel destroyed: {channel_id} (type={call_data.get("type")})')

        if call_data.get('type') == 'customer':
            agent_id = call_data.get('agent_id')
            lead_id  = call_data.get('lead_id')
            duration = (timezone.now() - call_data.get('answered_at', timezone.now())).seconds

            # Finalise call log
            await sync_to_async(self._finalise_call_log)(
                channel_id, lead_id, duration, call_data.get('bridge_id')
            )

            # Push call_ended to agent — they need to see disposition modal
            if agent_id:
                await sync_to_async(self._ws_call_ended)(agent_id, channel_id)
                # Set agent to wrapup state in DB
                await sync_to_async(self._set_agent_wrapup)(agent_id, channel_id)

    async def on_channel_state_change(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        state      = event.get('channel', {}).get('state', '')
        logger.debug(f'ChannelStateChange: {channel_id} → {state}')

    async def on_hangup_request(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        logger.debug(f'HangupRequest: {channel_id}')

    async def on_stasis_end(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        # Remove from agent_channels if this was an agent leg
        for agent_id, ch_id in list(self.agent_channels.items()):
            if ch_id == channel_id:
                del self.agent_channels[agent_id]
                logger.info(f'Agent {agent_id} channel ended.')
                break

    async def on_bridge_created(self, event):
        bridge_id = event.get('bridge', {}).get('id', '')
        logger.debug(f'Bridge created: {bridge_id}')

    async def on_bridge_destroyed(self, event):
        bridge_id  = event.get('bridge', {}).get('id', '')
        bridge_data = self.active_bridges.pop(bridge_id, None)
        if bridge_data:
            logger.info(f'Bridge {bridge_id} destroyed. Agent={bridge_data.get("agent_id")}')

    async def on_channel_entered_bridge(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        bridge_id  = event.get('bridge',  {}).get('id', '')
        logger.debug(f'Channel {channel_id} entered bridge {bridge_id}')

    async def on_channel_left_bridge(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        bridge_id  = event.get('bridge',  {}).get('id', '')
        logger.debug(f'Channel {channel_id} left bridge {bridge_id}')

    # ── DB helpers (sync — called via sync_to_async) ─────────────────────────

    def _find_available_agent(self, campaign_id):
        from agents.models import AgentStatus
        from campaigns.models import CampaignAgent
        # Agents assigned to this campaign who are ready
        agent_ids = CampaignAgent.objects.filter(
            campaign_id=campaign_id, is_active=True
        ).values_list('agent_id', flat=True)

        status = AgentStatus.objects.filter(
            user_id__in=agent_ids,
            status='ready',
        ).select_related('user').first()

        return status.user if status else None

    def _set_agent_status(self, agent_id, status, channel_id=None):
        from agents.models import AgentStatus
        from core.ws_utils import send_to_agent, agent_status_event
        qs = AgentStatus.objects.filter(user_id=agent_id)
        update = {'status': status}
        if channel_id is not None:
            update['active_channel_id'] = channel_id
        qs.update(**update)
        send_to_agent(agent_id, agent_status_event(agent_id, status, status.capitalize()))

    def _set_agent_on_call(self, agent_id, channel_id, lead_id, campaign_id):
        from agents.models import AgentStatus
        AgentStatus.objects.filter(user_id=agent_id).update(
            status='on_call',
            active_channel_id=channel_id,
            active_lead_id=lead_id,
            active_campaign_id=campaign_id,
            call_started_at=timezone.now(),
        )

    def _set_agent_wrapup(self, agent_id, channel_id):
        from agents.models import AgentStatus
        from core.ws_utils import send_to_agent
        AgentStatus.objects.filter(user_id=agent_id).update(
            status='wrapup',
            wrapup_started_at=timezone.now(),
            active_channel_id=None,
        )
        send_to_agent(agent_id, {'type': 'status_changed', 'status': 'wrapup', 'display': 'Wrap-up'})

    def _update_call_log(self, channel_id, status, lead_id, campaign_id):
        from calls.models import CallLog
        CallLog.objects.filter(channel_id=channel_id).update(status=status)
        # Also try to create if it doesn't exist (might be first event)
        CallLog.objects.get_or_create(
            channel_id=channel_id,
            defaults={
                'lead_id':      lead_id or None,
                'campaign_id':  campaign_id or None,
                'status':       status,
                'direction':    'outbound',
                'started_at':   timezone.now(),
            }
        )

    def _finalise_call_log(self, channel_id, lead_id, duration_seconds, bridge_id):
        from calls.models import CallLog
        CallLog.objects.filter(channel_id=channel_id).update(
            status='completed',
            ended_at=timezone.now(),
            duration=duration_seconds,
            bridge_id=bridge_id or '',
        )

    def _mark_call_dropped(self, channel_id):
        from calls.models import CallLog
        CallLog.objects.filter(channel_id=channel_id).update(
            status='dropped',
            ended_at=timezone.now(),
        )

    def _start_recording(self, bridge_id, campaign_id, lead_id):
        from campaigns.models import Campaign
        try:
            campaign = Campaign.objects.get(id=campaign_id)
            if not campaign.enable_recording:
                return None
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            name = f'rec_{campaign_id}_{lead_id}_{ts}'
            self.ari.start_bridge_recording(bridge_id, name)
            # Save path to call log
            from calls.models import CallLog
            from django.conf import settings
            rec_path = f"{settings.DIALER['RECORDING_PATH']}/{name}.wav"
            CallLog.objects.filter(bridge_id=bridge_id).update(recording_path=rec_path)
            return name
        except Exception as e:
            logger.error(f'Recording start failed: {e}')
            return None

    def _get_lead_info(self, lead_id):
        from leads.models import Lead
        try:
            lead = Lead.objects.get(id=lead_id)
            return {
                'id':         lead.id,
                'first_name': lead.first_name,
                'last_name':  lead.last_name,
                'phone':      lead.primary_phone,
                'email':      lead.email,
            }
        except Exception:
            return {}

    def _ws_call_connected(self, agent_id, call_id, bridge_id, lead_info):
        from core.ws_utils import send_to_agent, call_connected_event
        send_to_agent(agent_id, call_connected_event(call_id, lead_info, bridge_id))

    def _ws_call_ended(self, agent_id, call_id):
        from core.ws_utils import send_to_agent, call_ended_event
        send_to_agent(agent_id, call_ended_event(call_id, needs_disposition=True))


# ─── WebSocket Loop ───────────────────────────────────────────────────────────

async def run_ari_worker(server_config: dict):
    """
    Main async loop for the ARI WebSocket worker.
    Reconnects indefinitely with exponential back-off.
    """
    ari = ARIClient(
        host      = server_config['ARI_HOST'],
        port      = server_config['ARI_PORT'],
        username  = server_config['ARI_USERNAME'],
        password  = server_config['ARI_PASSWORD'],
        app_name  = server_config['ARI_APP_NAME'],
    )
    handler    = ARIEventHandler(ari)
    ws_url     = (
        f"ws://{server_config['ARI_HOST']}:{server_config['ARI_PORT']}"
        f"/ari/events?app={server_config['ARI_APP_NAME']}"
        f"&api_key={server_config['ARI_USERNAME']}:{server_config['ARI_PASSWORD']}"
    )
    delay      = 2   # initial reconnect delay (seconds)
    max_delay  = 60

    while True:
        try:
            logger.info(f'ARI worker connecting to {server_config["ARI_HOST"]}:{server_config["ARI_PORT"]} …')
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                delay = 2  # reset on successful connection
                logger.info('ARI worker connected ✓')

                # Update server status in DB
                await sync_to_async(_mark_server_connected)(server_config['ARI_HOST'])

                async for raw in ws:
                    event = json.loads(raw)
                    await handler.handle(event)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f'ARI WebSocket closed: {e}. Reconnecting in {delay}s …')
        except OSError as e:
            logger.warning(f'ARI connection error: {e}. Reconnecting in {delay}s …')
        except Exception as e:
            logger.exception(f'Unexpected ARI error: {e}. Reconnecting in {delay}s …')

        await sync_to_async(_mark_server_disconnected)(server_config['ARI_HOST'])
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


def _mark_server_connected(host):
    from telephony.models import AsteriskServer
    AsteriskServer.objects.filter(ari_host=host).update(
        connection_status='connected',
        last_connected=timezone.now(),
    )


def _mark_server_disconnected(host):
    from telephony.models import AsteriskServer
    AsteriskServer.objects.filter(ari_host=host).update(
        connection_status='disconnected',
    )


# ─── Thread entry-point (called from AppConfig.ready) ────────────────────────

def start_ari_worker_thread(server_config: dict):
    """
    Spin up the ARI worker in a daemon thread with its own event loop.
    Called once from TelephonyConfig.ready() — no management command needed.
    """
    def thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_ari_worker(server_config))
        except Exception as e:
            logger.exception(f'ARI worker thread crashed: {e}')
        finally:
            loop.close()

    t = threading.Thread(target=thread_target, name='ari-worker', daemon=True)
    t.start()
    logger.info('ARI worker thread started (daemon).')
    return t
