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
import os
import threading
import time
from datetime import datetime
from typing import Optional

import requests
import websockets
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('telephony.ari_worker')
ARI_TRACE = os.environ.get('DIALFLOW_ARI_TRACE', '1') == '1'


def build_cid(campaign_id=None, lead_id=None, customer_channel=None, agent_channel=None):
    """Build a compact correlation token shared across ARI and dialer logs."""
    parts = []
    if campaign_id not in (None, ''):
        parts.append(f'c{campaign_id}')
    if lead_id not in (None, ''):
        parts.append(f'l{lead_id}')
    if customer_channel not in (None, ''):
        parts.append(f'cust{customer_channel}')
    if agent_channel not in (None, ''):
        parts.append(f'agt{agent_channel}')
    return 'cid=' + ('-'.join(parts) if parts else 'unknown')


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
            if r.status_code == 404 and method == 'GET':
                return None
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

    def originate_to_app(self, endpoint, variables=None, caller_id='', timeout=30):
        """Originate a channel directly into the Stasis app (no dialplan context)."""
        return self.post('/channels', json={
            'endpoint':     endpoint,
            'app':          self.app,
            'callerId':     caller_id,
            'timeout':      timeout,
            'variables':    variables or {},
        })

    def moh_start(self, channel_id, moh_class='default'):
        return self.post(f'/channels/{channel_id}/moh', json={'mohClass': moh_class})

    def moh_stop(self, channel_id):
        return self.delete(f'/channels/{channel_id}/moh')


# ─── Event Handler ────────────────────────────────────────────────────────────

class ARIEventHandler:
    """
    Processes all ARI events received over WebSocket.

    State maps:
      active_calls    : channel_id → {type, campaign_id, lead_id, agent_id, ...}
      active_bridges  : bridge_id  → {channel_ids, recording_name, ...}
      agent_channels  : agent_id   → channel_id  (agent's active leg)
      pending_bridges : agent_channel_id → {customer_channel, agent_id, ...}
    """

    def __init__(self, client: ARIClient):
        self.ari             = client
        self.active_calls    = {}
        self.active_bridges  = {}
        self.agent_channels  = {}   # agent_id → channel_id
        self.pending_bridges = {}   # agent_channel_id → bridge info (waiting for agent answer)
        self.pending_by_agent = {}  # agent_id → bridge info (fallback when ARI channel ids differ)
        self.pending_agent_channels = {}  # agent_channel_id -> pending info

    def _trace(self, msg, *args):
        if ARI_TRACE:
            logger.warning('[ARI-TRACE] ' + msg, *args)

    def _trace_pending(self, label):
        if not ARI_TRACE:
            return
        try:
            pb = list(self.pending_bridges.keys())
            pa = {k: (v.get('customer_channel'), v.get('originated_agent_channel')) for k, v in self.pending_by_agent.items()}
            logger.warning('[ARI-TRACE] %s pending_bridges=%s pending_by_agent=%s', label, pb, pa)
        except Exception:
            logger.warning('[ARI-TRACE] %s pending dump failed', label)

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

        # Read channel variables to understand call purpose.
        # get_channel_var makes an HTTP REST call to Asterisk — if ARI_HOST is
        # 127.0.0.1 on Windows with WSL2, this can fail due to portproxy
        # flakiness.  The run_ari command now detects WSL2 IP directly, but we
        # log failures here so they're visible in the ARI worker window.
        call_type      = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CALL_TYPE')      or ''
        campaign_id    = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CAMPAIGN_ID')    or ''
        lead_id        = await sync_to_async(self.ari.get_channel_var)(channel_id, 'LEAD_ID')        or ''
        agent_id       = await sync_to_async(self.ari.get_channel_var)(channel_id, 'AGENT_ID')       or ''
        customer_num   = await sync_to_async(self.ari.get_channel_var)(channel_id, 'CUSTOMER_NUMBER') or channel.get('caller', {}).get('number', '')

        if not call_type:
            # Variables unreadable — this usually means the ARI REST call to
            # Asterisk failed (wrong IP, portproxy issue, or channel already gone).
            # Log all available event info so we can diagnose.
            caller = channel.get('caller', {})
            dialplan = channel.get('dialplan', {})
            logger.error(
                'StasisStart: CALL_TYPE variable is EMPTY for channel=%s — '
                'ARI REST call to get_channel_var failed (check ARI_HOST connectivity). '
                'channel_name=%s caller=%s exten=%s context=%s '
                'Ignoring this StasisStart to avoid dropping a live channel.',
                channel_id,
                channel.get('name', '?'),
                caller.get('number', '?'),
                dialplan.get('exten', '?'),
                dialplan.get('context', '?'),
            )
            # Do not hangup here; late/partial StasisStart events can occur
            # during normal call flow and must not tear down active calls.
            return
        cid = build_cid(campaign_id=campaign_id, lead_id=lead_id, customer_channel=channel_id if call_type == 'autodial' else None, agent_channel=channel_id if call_type == 'agent_leg' else None)

        logger.info(f'StasisStart: {cid} channel={channel_id} type={call_type} campaign={campaign_id} lead={lead_id} agent={agent_id}')

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

        # Answer the channel so media can flow
        self._trace('autodial answer customer_channel=%s cid=%s', channel_id, build_cid(campaign_id=campaign_id, lead_id=lead_id, customer_channel=channel_id))
        await sync_to_async(self.ari.answer)(channel_id)

        # Play hold music while we find an agent
        self._trace('autodial moh_start customer_channel=%s', channel_id)
        await sync_to_async(self.ari.moh_start)(channel_id)

        # Atomically reserve an available agent from DB so parallel answered calls
        # cannot grab the same ready agent.
        agent = await sync_to_async(self._reserve_available_agent)(campaign_id)
        cid = build_cid(campaign_id=campaign_id, lead_id=lead_id, customer_channel=channel_id)

        if agent is None:
            logger.warning(f'No available agent. {cid} campaign={campaign_id} customer_channel={channel_id} will_be_dropped=true')
            await sync_to_async(self._mark_call_dropped)(channel_id)
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        await self._bridge_customer_to_agent(channel_id, agent, campaign_id, lead_id)

    async def _handle_agent_leg(self, channel_id, agent_id):
        """
        Agent's PJSIP channel entered Stasis after answering the originated call.
        Complete the pending bridge if one exists.
        """
        if not agent_id:
            return

        agent_id = int(agent_id)
        self.agent_channels[agent_id] = channel_id
        self._trace('agent_leg stasis_start agent_id=%s channel=%s', agent_id, channel_id)
        self._trace_pending('before agent_leg resolution')

        # DO NOT call ari.answer() here.
        # This channel is an ARI-originated OUTBOUND leg (Asterisk → JsSIP).
        # Calling answer() on an outbound PJSIP channel while it is still
        # RINGING causes Asterisk to send SIP CANCEL to the browser, which is
        # exactly the "cause=Canceled originator=remote" failure we want to
        # prevent.  For originate_to_app channels, the SIP answer is handled
        # entirely by JsSIP sending 200 OK — no ARI answer call is needed.
        logger.info(f'Agent {agent_id} channel {channel_id} in Stasis.')

        # Check if there's a pending bridge waiting for this agent.
        # Primary lookup: channel id (normal case — StasisStart arrived after originate).
        pending = self.pending_bridges.get(channel_id)
        if not pending:
            # Fallback: agent-id lookup.  Covers two cases:
            #   a) StasisStart arrived before originate_to_app returned (race condition
            #      handled by pre-registering pending_by_agent before the HTTP call).
            #   b) Asterisk assigned a different channel id in StasisStart than the one
            #      returned by originate (seen on some Asterisk versions).
            pending = self.pending_by_agent.get(agent_id)
            if pending:
                stale_channel_id = pending.get('originated_agent_channel')
                if stale_channel_id and stale_channel_id != channel_id:
                    # Re-key pending to the actual Stasis channel id and stop
                    # tracking the stale originated id. We'll spawn a watcher
                    # on the new id below.
                    self.pending_bridges.pop(stale_channel_id, None)
                    pending['originated_agent_channel'] = channel_id
                    self.pending_bridges[channel_id] = pending

        if pending:
            # StasisStart may arrive while the outbound agent leg is still Ring/Ringing.
            # Bridging too early can cause CANCEL races. Only bridge once channel is Up.
            ch = await sync_to_async(self.ari.get)(f'/channels/{channel_id}')
            state = (ch.get('state', '') if ch else '').lower()
            self._trace('agent_leg channel lookup channel=%s state=%s raw=%s', channel_id, state or 'unknown', ch)
            if state != 'up':
                logger.info(
                    f'Agent leg StasisStart before answer; waiting for Up. '
                    f"agent={agent_id} channel={channel_id} state={state or 'unknown'}"
                )
                # Ensure fallback lookup remains intact.
                self.pending_bridges[channel_id] = pending
                self.pending_by_agent[agent_id] = pending
                # In some Asterisk builds there is no second StasisStart after 200 OK.
                # Keep an explicit watcher on this channel id.
                self._trace_pending('agent_leg wait for up')
                asyncio.create_task(self._await_agent_answer(channel_id, agent_id))
                return

            # Channel is Up — consume pending and complete bridge.
            self.pending_bridges.pop(channel_id, None)
            self.pending_agent_channels.pop(channel_id, None)
            self.pending_by_agent.pop(agent_id, None)
            stale_channel_id = pending.get('originated_agent_channel')
            if stale_channel_id and stale_channel_id != channel_id:
                self.pending_bridges.pop(stale_channel_id, None)
                self.pending_agent_channels.pop(stale_channel_id, None)

            self._trace_pending('agent_leg completing bridge')
            await self._complete_bridge(channel_id, pending)
        else:
            # No pending bridge found.  Check if _await_agent_answer already
            # completed the bridge (it pops pending_bridges and adds the agent
            # channel to active_calls).  If so, do nothing — the call is alive.
            if channel_id in self.active_calls:
                logger.debug(f'Agent channel {channel_id} already has active call — bridge completed by _await_agent_answer.')
                return
            # Truly orphaned: release only if agent is still in on_call state.
            cid = build_cid(agent_channel=channel_id)
            logger.warning(
                f'Agent leg has no pending customer bridge. {cid} agent={agent_id} '
                f'agent_channel={channel_id} — releasing agent reservation.'
            )
            await sync_to_async(self._release_agent_reservation)(agent_id)

    async def _bridge_customer_to_agent(self, channel_id, agent, campaign_id, lead_id):
        """Originate a call to the agent's PJSIP endpoint, then bridge when agent answers."""

        # Look up agent's PJSIP extension
        agent_ext = await sync_to_async(self._get_agent_extension)(agent.id)
        if not agent_ext:
            logger.error(f'Agent {agent.id} has no active PJSIP extension.')
            await sync_to_async(self._release_agent_reservation)(agent.id)
            await sync_to_async(self._mark_call_dropped)(channel_id)
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        # Originate call to agent via ARI (enters Stasis when agent answers)
        agent_endpoint = f'PJSIP/{agent_ext}'
        variables = {
            'CALL_TYPE':        'agent_leg',
            'AGENT_ID':         str(agent.id),
            'CAMPAIGN_ID':      str(campaign_id),
            'LEAD_ID':          str(lead_id),
        }
        cid = build_cid(campaign_id=campaign_id, lead_id=lead_id, customer_channel=channel_id)

        # ── RACE-CONDITION FIX ──────────────────────────────────────────────────
        # StasisStart for the agent channel can arrive on the ARI WebSocket BEFORE
        # originate_to_app's HTTP response returns (asyncio yields to the event loop
        # while the HTTP call runs in the thread pool).  If that happens,
        # _handle_agent_leg finds pending_bridges empty and wrongly releases the
        # agent.  Pre-registering pending_by_agent (keyed only on agent id, since we
        # don't know the channel id yet) lets _handle_agent_leg find the bridge via
        # its agent-id fallback even in that early-StasisStart scenario.
        pending = {
            'customer_channel':         channel_id,
            'agent_id':                 agent.id,
            'agent_ext':                agent_ext,
            'campaign_id':              campaign_id,
            'lead_id':                  lead_id,
            'originated_agent_channel': None,   # filled in below after originate
        }
        self.pending_by_agent[agent.id] = pending
        self._trace_pending('after pending_by_agent seed')

        logger.info(f'Originating agent leg: {cid} agent_id={agent.id} ext={agent_ext} customer_channel={channel_id}')

        # Give WebRTC leg enough time to pass browser permission/auto-answer flow.
        result = await sync_to_async(self.ari.originate_to_app)(
            agent_endpoint, variables=variables, timeout=60
        )
        self._trace('originate_to_app result agent_id=%s endpoint=%s result=%s', agent.id, agent_endpoint, result)
        if not result:
            logger.error(f'Failed to originate agent leg: {cid} agent_id={agent.id} ext={agent_ext}')
            self.pending_by_agent.pop(agent.id, None)
            await sync_to_async(self._release_agent_reservation)(agent.id)
            await sync_to_async(self._mark_call_dropped)(channel_id)
            await sync_to_async(self.ari.hangup)(channel_id)
            return

        agent_channel_id = result.get('id', '')
        cid = build_cid(
            campaign_id=campaign_id,
            lead_id=lead_id,
            customer_channel=channel_id,
            agent_channel=agent_channel_id,
        )

        # Update pending with real channel id (same dict object — pending_by_agent
        # already holds a reference to it, so this update is visible there too).
        pending['originated_agent_channel'] = agent_channel_id
        self.pending_bridges[agent_channel_id] = pending
        self.pending_agent_channels[agent_channel_id] = pending
        self._trace_pending('after pending_bridges set')
        # Note: pending_by_agent[agent.id] already points to this same dict.

        # Notify agent dashboard immediately so it shows the ringing call
        await sync_to_async(self._set_agent_ringing)(
            agent.id, channel_id, lead_id, campaign_id
        )
        lead_info     = await sync_to_async(self._get_lead_info)(lead_id)
        campaign_info = await sync_to_async(self._get_campaign_info)(campaign_id)
        await sync_to_async(self._ws_call_incoming)(agent.id, channel_id, lead_info, campaign_info)

        logger.info(f'Waiting for agent answer: {cid} agent_id={agent.id} agent_channel={agent_channel_id}')
        asyncio.create_task(self._await_agent_answer(agent_channel_id, agent.id, timeout_seconds=70))

    async def _await_agent_answer(
        self,
        agent_channel_id,
        agent_id,
        timeout_seconds=35,
        force_bridge_after_ring_seconds=2,
    ):
        """
        Fallback path when agent-leg StasisStart is not delivered.
        Poll channel state and complete bridge once agent channel is Up.
        """
        started_waiting_at = time.time()
        deadline = started_waiting_at + timeout_seconds
        while time.time() < deadline:
            pending = self.pending_bridges.get(agent_channel_id)
            if not pending:
                # Bridge already completed (or cleaned up) via normal Stasis path.
                self._trace('await_agent_answer stop no pending agent_channel=%s', agent_channel_id)
                return

            channel = await sync_to_async(self.ari.get)(f'/channels/{agent_channel_id}')
            if channel is None:
                # Channel vanished; stop waiting.
                self._trace('await_agent_answer channel vanished agent_channel=%s', agent_channel_id)
                channels_snapshot = await sync_to_async(self.ari.get)('/channels')
                self._trace('await_agent_answer channels snapshot after vanish=%s', channels_snapshot)
                pending = self.pending_bridges.get(agent_channel_id)
                replacement = None
                if pending:
                    replacement = await self._find_replacement_agent_channel(
                        pending, agent_channel_id
                    )
                if replacement:
                    self._trace(
                        'await_agent_answer rebind pending from %s to replacement %s',
                        agent_channel_id, replacement,
                    )
                    self.pending_bridges.pop(agent_channel_id, None)
                    self.pending_agent_channels.pop(agent_channel_id, None)
                    pending['originated_agent_channel'] = replacement
                    self.pending_bridges[replacement] = pending
                    self.pending_agent_channels[replacement] = pending
                    self.agent_channels[agent_id] = replacement
                    agent_channel_id = replacement
                    continue
                break

            state = (channel.get('state') or '').lower()
            self._trace('await_agent_answer poll agent_channel=%s state=%s channel=%s', agent_channel_id, state or 'unknown', channel)
            if state == 'up':
                pending = self.pending_bridges.pop(agent_channel_id, None)
                if not pending:
                    return
                self.pending_agent_channels.pop(agent_channel_id, None)
                self.pending_by_agent.pop(agent_id, None)
                cid = build_cid(
                    campaign_id=pending.get('campaign_id'),
                    lead_id=pending.get('lead_id'),
                    customer_channel=pending.get('customer_channel'),
                    agent_channel=agent_channel_id,
                )
                logger.warning(
                    f'Agent answer detected via channel-state fallback (missing agent StasisStart). {cid}'
                )
                self._trace_pending('await_agent_answer completing bridge')
                await self._complete_bridge(agent_channel_id, pending)
                return

            ring_wait = time.time() - started_waiting_at
            if state in ('ring', 'ringing', 'down') and ring_wait >= force_bridge_after_ring_seconds:
                pending = self.pending_bridges.pop(agent_channel_id, None)
                if not pending:
                    return
                self.pending_agent_channels.pop(agent_channel_id, None)
                self.pending_by_agent.pop(agent_id, None)
                cid = build_cid(
                    campaign_id=pending.get('campaign_id'),
                    lead_id=pending.get('lead_id'),
                    customer_channel=pending.get('customer_channel'),
                    agent_channel=agent_channel_id,
                )
                logger.warning(
                    f'Agent leg state={state or "unknown"} after {int(ring_wait)}s; forcing bridge/answer to avoid MOH delay. {cid}'
                )
                self._trace_pending('await_agent_answer force bridge on ringing')
                await self._complete_bridge(agent_channel_id, pending)
                return

            await asyncio.sleep(0.5)

        pending = self.pending_bridges.pop(agent_channel_id, None)
        self.pending_agent_channels.pop(agent_channel_id, None)
        if pending:
            self.pending_by_agent.pop(agent_id, None)
            customer_channel_id = pending.get('customer_channel')
            cid = build_cid(
                campaign_id=pending.get('campaign_id'),
                lead_id=pending.get('lead_id'),
                customer_channel=customer_channel_id,
                agent_channel=agent_channel_id,
            )
            logger.error(f'Agent answer timeout; dropping customer leg to avoid infinite MOH. {cid}')
            self._trace_pending('await_agent_answer timeout')
            await sync_to_async(self._release_agent_reservation)(agent_id)
            if customer_channel_id:
                await sync_to_async(self._mark_call_dropped)(customer_channel_id)
                await sync_to_async(self.ari.hangup)(customer_channel_id)

    async def _complete_bridge(self, agent_channel_id, pending):
        """Called when the agent answers — create bridge and connect customer ↔ agent.

        FIX: The original code did not answer the agent channel before adding it
        to the bridge. For PJSIP channels originated via originate_to_app, the
        channel enters Stasis in 'Ring' state. Adding a ringing channel to a
        mixing bridge does NOT establish media — the channel must be in 'Up' state.

        The fix: after JsSIP sends SIP 200 OK (which we detect via channel state
        polling in _await_agent_answer, or via StasisStart), we explicitly call
        ari.answer() on the agent channel. For outbound-originated channels that
        are already Up (JsSIP answered the INVITE), ari.answer() is a no-op.
        For channels still in Ring/Ringing state (race condition), this forces
        them Up so the bridge can mix audio.
        """
        customer_channel_id = pending['customer_channel']
        agent_id            = pending['agent_id']
        campaign_id         = pending['campaign_id']
        lead_id             = pending['lead_id']
        cid = build_cid(
            campaign_id=campaign_id,
            lead_id=lead_id,
            customer_channel=customer_channel_id,
            agent_channel=agent_channel_id,
        )

        # Check customer channel is still alive
        if customer_channel_id not in self.active_calls:
            logger.warning(f'Customer channel gone before bridge completion. {cid}')
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            await sync_to_async(self._release_agent_reservation)(agent_id)
            return

        # Guard against double bridge creation
        if self.active_calls[customer_channel_id].get('bridge_id'):
            logger.warning(f'Bridge already exists for customer {customer_channel_id}, skipping duplicate _complete_bridge. {cid}')
            return

        # ── CRITICAL: stamp agent_id on the customer entry NOW ──
        self.active_calls[customer_channel_id]['agent_id'] = agent_id

        # ── FIX: Ensure agent channel is in Up state before bridging ──
        # For originate_to_app channels, JsSIP sends SIP 200 OK which Asterisk
        # processes, but the ARI channel may still report as 'Ring'. Calling
        # answer() forces it to 'Up'. If already Up, this is a harmless no-op.
        agent_ch_state = await sync_to_async(self.ari.get)(f'/channels/{agent_channel_id}')
        self._trace('complete_bridge pre-answer agent_channel=%s state=%s payload=%s', agent_channel_id, (agent_ch_state or {}).get('state', 'unknown'), agent_ch_state)
        if agent_ch_state and (agent_ch_state.get('state', '') or '').lower() != 'up':
            logger.info(f'Agent channel {agent_channel_id} state={agent_ch_state.get("state")} — forcing answer. {cid}')
            ans = await sync_to_async(self.ari.answer)(agent_channel_id)
            self._trace('complete_bridge forced answer result agent_channel=%s result=%s', agent_channel_id, ans)
            # Brief pause to let Asterisk process the answer
            await asyncio.sleep(0.3)

        # Stop hold music on customer
        moh = await sync_to_async(self.ari.moh_stop)(customer_channel_id)
        self._trace('complete_bridge moh_stop customer=%s result=%s', customer_channel_id, moh)

        # Customer may have hung up while moh_stop was in flight
        if customer_channel_id not in self.active_calls:
            logger.warning(f'Customer hung up during moh_stop. {cid}')
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            return

        # Create bridge
        bridge_name   = f'bridge_{campaign_id}_{lead_id}_{int(time.time())}'
        bridge_result = await sync_to_async(self.ari.create_bridge)(name=bridge_name)
        self._trace('complete_bridge create_bridge name=%s result=%s', bridge_name, bridge_result)
        if not bridge_result:
            logger.error(f'Failed to create ARI bridge. {cid}')
            await sync_to_async(self.ari.hangup)(customer_channel_id)
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            return

        bridge_id = bridge_result.get('id')

        # Customer may have hung up during create_bridge
        if customer_channel_id not in self.active_calls:
            logger.warning(f'Customer hung up during bridge creation. {cid}')
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            return

        # Add both channels to the bridge
        add_cust = await sync_to_async(self.ari.add_to_bridge)(bridge_id, customer_channel_id)
        add_agent = await sync_to_async(self.ari.add_to_bridge)(bridge_id, agent_channel_id)
        self._trace('complete_bridge add_to_bridge bridge=%s customer=%s result=%s', bridge_id, customer_channel_id, add_cust)
        self._trace('complete_bridge add_to_bridge bridge=%s agent=%s result=%s', bridge_id, agent_channel_id, add_agent)

        # ARI addChannel commonly returns {} on success; only None means REST failure.
        if add_cust is None or add_agent is None:
            logger.error(f'Failed to add channel(s) to bridge. {cid} cust={add_cust} agent={add_agent}')
            # Try cleanup
            await sync_to_async(self.ari.hangup)(customer_channel_id)
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            return

        # Final check — customer could have hung up during add_to_bridge
        if customer_channel_id not in self.active_calls:
            logger.warning(f'Customer hung up during add_to_bridge. {cid}')
            await sync_to_async(self.ari.hangup)(agent_channel_id)
            return

        self.active_bridges[bridge_id] = {
            'customer_channel': customer_channel_id,
            'agent_channel':    agent_channel_id,
            'agent_id':         agent_id,
            'campaign_id':      campaign_id,
            'lead_id':          lead_id,
            'created_at':       timezone.now(),
        }
        self.active_calls[customer_channel_id]['bridge_id'] = bridge_id

        # Track agent channel in active_calls too
        self.active_calls[agent_channel_id] = {
            'type':      'agent',
            'agent_id':  agent_id,
            'bridge_id': bridge_id,
        }

        # Persist bridge/agent binding on the customer call log row.
        await sync_to_async(self._bind_call_log_agent)(
            customer_channel_id, agent_id, campaign_id, lead_id
        )
        from calls.models import CallLog
        await sync_to_async(CallLog.objects.filter(channel_id=customer_channel_id).update)(
            bridge_id=bridge_id,
            answered_at=timezone.now(),
            updated_at=timezone.now(),
        )

        # Mark agent as on-call in DB
        await sync_to_async(self._set_agent_on_call)(agent_id, customer_channel_id, lead_id, campaign_id)

        # Start recording if campaign has it enabled
        recording_name = await sync_to_async(self._start_recording)(
            bridge_id, campaign_id, lead_id
        )
        if recording_name:
            self.active_bridges[bridge_id]['recording_name'] = recording_name

        # Push call_connected to agent via WebSocket
        lead_info = await sync_to_async(self._get_lead_info)(lead_id)
        await sync_to_async(self._ws_call_connected)(agent_id, customer_channel_id, bridge_id, lead_info)

        logger.info(f'Bridge created: {cid} bridge={bridge_id} customer={customer_channel_id} agent={agent_channel_id}')

    # ── ChannelDestroyed ──────────────────────────────────────────────────────

    async def on_channel_destroyed(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        cause = event.get('cause')
        cause_txt = event.get('cause_txt', '')

        pending_agent = self.pending_agent_channels.pop(channel_id, None)
        if pending_agent:
            agent_id = pending_agent.get('agent_id')
            customer_channel_id = pending_agent.get('customer_channel')
            replacement = await self._find_replacement_agent_channel(
                pending_agent, channel_id
            )
            if replacement:
                self._trace(
                    'pending agent destroy ignored; replacement channel found old=%s new=%s',
                    channel_id, replacement,
                )
                pending_agent['originated_agent_channel'] = replacement
                self.pending_bridges.pop(channel_id, None)
                self.pending_bridges[replacement] = pending_agent
                self.pending_agent_channels[replacement] = pending_agent
                if agent_id is not None:
                    self.agent_channels[agent_id] = replacement
                return
            cid = build_cid(
                campaign_id=pending_agent.get('campaign_id'),
                lead_id=pending_agent.get('lead_id'),
                customer_channel=customer_channel_id,
                agent_channel=channel_id,
            )
            logger.error(
                'Pending agent leg destroyed before answer. %s cause=%s cause_txt=%s',
                cid, cause, cause_txt or 'n/a',
            )
            self.pending_bridges.pop(channel_id, None)
            if agent_id is not None:
                self.pending_by_agent.pop(agent_id, None)
                await sync_to_async(self._release_agent_reservation)(agent_id)
            if customer_channel_id:
                await sync_to_async(self._mark_call_dropped)(customer_channel_id)
                await sync_to_async(self.ari.hangup)(customer_channel_id)
            return

        call_data  = self.active_calls.pop(channel_id, None)

        if not call_data:
            self._trace(
                'channel_destroyed untracked channel=%s cause=%s cause_txt=%s raw=%s',
                channel_id, cause, cause_txt or 'n/a', event.get('channel', {}),
            )
            return

        cid = build_cid(
            campaign_id=call_data.get('campaign_id'),
            lead_id=call_data.get('lead_id'),
            customer_channel=channel_id if call_data.get('type') == 'customer' else call_data.get('customer_channel'),
            agent_channel=channel_id if call_data.get('type') == 'agent' else None,
        )
        logger.info(f'Channel destroyed: {cid} channel={channel_id} type={call_data.get("type")}')

        if call_data.get('type') == 'customer':
            agent_id = call_data.get('agent_id')
            lead_id  = call_data.get('lead_id')
            duration = (timezone.now() - call_data.get('answered_at', timezone.now())).seconds

            # Finalise call log
            call_log_id = await sync_to_async(self._finalise_call_log)(
                channel_id, lead_id, duration, call_data.get('bridge_id')
            )

            # Write wrapup state to DB FIRST so a page refresh gets correct snapshot,
            # then push call_ended so the live browser shows the disposition modal.
            if agent_id:
                await sync_to_async(self._set_agent_wrapup)(agent_id, channel_id, call_log_id)
                await sync_to_async(self._ws_call_ended)(agent_id, channel_id, call_log_id)

    async def _find_replacement_agent_channel(self, pending, dead_channel_id):
        """
        If Asterisk rotates agent channel IDs mid-ring (seen on some setups),
        find the replacement channel for the same endpoint and keep waiting.
        """
        agent_ext = pending.get('agent_ext')
        if not agent_ext:
            return None
        ep = await sync_to_async(self.ari.get)(f'/endpoints/PJSIP/{agent_ext}')
        if not isinstance(ep, dict):
            return None
        for cid in (ep.get('channel_ids') or []):
            if cid and cid != dead_channel_id:
                return cid
        return None

    async def on_channel_state_change(self, event):
        channel_id = event.get('channel', {}).get('id', '')
        state      = event.get('channel', {}).get('state', '')
        logger.debug(f'ChannelStateChange: {channel_id} -> {state}')
        self._trace('channel_state_change channel=%s state=%s', channel_id, state)

        # Robust bridge trigger: if agent leg transitions to Up, complete bridge
        # even if StasisStart happened earlier while still ringing.
        if (state or '').lower() != 'up':
            return

        pending = self.pending_bridges.get(channel_id)
        agent_id = None

        if not pending:
            # Reverse lookup of agent id by current channel map.
            for aid, ch_id in self.agent_channels.items():
                if ch_id == channel_id:
                    agent_id = aid
                    break
            if agent_id is not None:
                pending = self.pending_by_agent.get(agent_id)
                if pending:
                    stale_channel_id = pending.get('originated_agent_channel')
                    if stale_channel_id and stale_channel_id != channel_id:
                        self.pending_bridges.pop(stale_channel_id, None)
                    pending['originated_agent_channel'] = channel_id
                    self.pending_bridges[channel_id] = pending

        if not pending:
            self._trace('channel_state_change up without pending channel=%s', channel_id)
            return

        if agent_id is None:
            agent_id = pending.get('agent_id')

        self.pending_bridges.pop(channel_id, None)
        self.pending_agent_channels.pop(channel_id, None)
        if agent_id is not None:
            self.pending_by_agent.pop(agent_id, None)
        self._trace_pending('channel_state_change completing bridge')
        await self._complete_bridge(channel_id, pending)

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

    def _reserve_available_agent(self, campaign_id):
        from agents.models import AgentStatus
        from campaigns.models import CampaignAgent
        with transaction.atomic():
            # Agents assigned to this campaign who are ready
            agent_ids = CampaignAgent.objects.filter(
                campaign_id=campaign_id, is_active=True
            ).values_list('agent_id', flat=True)

            status = (
                AgentStatus.objects.select_for_update()
                .filter(user_id__in=agent_ids, status='ready')
                .select_related('user')
                .order_by('status_changed_at')
                .first()
            )
            if not status:
                return None

            # Reserve immediately to avoid double-assignment races.
            # Use "ringing" (not "on_call") until bridge is actually created.
            status.status = 'ringing'
            status.status_changed_at = timezone.now()
            status.active_campaign_id = int(campaign_id) if campaign_id else None
            status.call_started_at = None
            status.active_lead_id = None
            status.active_channel_id = None
            status.wrapup_started_at = None
            status.wrapup_call_log_id = None
            status.active_call_log_id = None
            status.save(update_fields=[
                'status',
                'status_changed_at',
                'active_campaign',
                'call_started_at',
                'active_lead_id',
                'active_channel_id',
                'wrapup_started_at',
                'wrapup_call_log_id',
                'active_call_log_id',
                'updated_at',
            ])
            return status.user

    def _release_agent_reservation(self, agent_id):
        """Return a pre-reserved agent back to ready when bridge setup fails."""
        from agents.models import AgentStatus
        from core.ws_utils import send_to_agent
        updated = AgentStatus.objects.filter(
            user_id=agent_id, status__in=['ringing', 'on_call']
        ).update(
            status='ready',
            status_changed_at=timezone.now(),
            active_channel_id=None,
            active_lead_id=None,
            call_started_at=None,
        )
        if updated:
            send_to_agent(agent_id, {
                'type': 'status_changed',
                'status': 'ready',
                'display': 'Ready',
                'since': timezone.now().isoformat(),
            })

    def _get_agent_extension(self, agent_id):
        """Look up the PJSIP extension for an agent from the Phone model."""
        from telephony.models import Phone
        try:
            phone = Phone.objects.get(user_id=agent_id, is_active=True)
            return phone.extension
        except Phone.DoesNotExist:
            logger.warning(f'No active phone found for agent {agent_id}')
            return None

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
        from core.ws_utils import send_to_agent, broadcast_supervisor

        now = timezone.now()
        AgentStatus.objects.filter(user_id=agent_id).update(
            status='on_call',
            status_changed_at=now,          # ← FIX: was missing
            active_channel_id=channel_id,
            active_lead_id=lead_id,
            active_campaign_id=campaign_id,
            active_call_log_id=None,
            wrapup_started_at=None,
            wrapup_call_log_id=None,
            call_started_at=now,
            updated_at=now,
        )
        payload = {
            'type':    'status_changed',
            'status':  'on_call',
            'display': 'On Call',
            'since':   now.isoformat(),     # ← FIX: was missing
            'active_lead_id': lead_id,
            'active_channel_id': channel_id,
        }
        send_to_agent(agent_id, payload)
        broadcast_supervisor({
            'type':      'agent_status_changed',
            'agent_id':  agent_id,
            'status':    'on_call',
            'since':     now.isoformat(),
        })

    def _set_agent_ringing(self, agent_id, customer_channel_id, lead_id, campaign_id):
        from agents.models import AgentStatus
        from core.ws_utils import send_to_agent, broadcast_supervisor

        now = timezone.now()
        AgentStatus.objects.filter(user_id=agent_id).update(
            status='ringing',
            status_changed_at=now,
            active_channel_id=customer_channel_id,
            active_lead_id=lead_id,
            active_campaign_id=campaign_id,
            active_call_log_id=None,
            wrapup_started_at=None,
            wrapup_call_log_id=None,
            call_started_at=None,
            updated_at=now,
        )
        payload = {
            'type':    'status_changed',
            'status':  'ringing',
            'display': 'Ringing',
            'since':   now.isoformat(),
            'active_lead_id': lead_id,
            'active_channel_id': customer_channel_id,
        }
        send_to_agent(agent_id, payload)
        broadcast_supervisor({
            'type':      'agent_status_changed',
            'agent_id':  agent_id,
            'status':    'ringing',
            'since':     now.isoformat(),
        })

    def _bind_call_log_agent(self, channel_id, agent_id, campaign_id, lead_id):
        from calls.models import CallLog

        call_log, _ = CallLog.objects.get_or_create(
            channel_id=channel_id,
            defaults={
                'lead_id':      lead_id or None,
                'campaign_id':  campaign_id or None,
                'status':       'answered',
                'direction':    'outbound',
                'started_at':   timezone.now(),
            }
        )
        CallLog.objects.filter(id=call_log.id).update(
            agent_id=agent_id,
            campaign_id=campaign_id or call_log.campaign_id,
            lead_id=lead_id or call_log.lead_id,
            updated_at=timezone.now(),
        )

    def _set_agent_wrapup(self, agent_id, channel_id, call_log_id=None):
        from agents.models import AgentStatus
        from core.ws_utils import send_to_agent, broadcast_supervisor

        now = timezone.now()
        AgentStatus.objects.filter(user_id=agent_id).update(
            status='wrapup',
            status_changed_at=now,          # ← FIX: was missing
            wrapup_started_at=now,
            active_channel_id=None,
            wrapup_call_log_id=call_log_id,
            active_call_log_id=call_log_id,
            updated_at=now,
        )
        payload = {
            'type':    'status_changed',
            'status':  'wrapup',
            'display': 'Wrap-up',
            'since':   now.isoformat(),     # ← FIX: was missing
            'call_log_id': call_log_id,     # ← NEW: so dashboard knows which call
            'active_lead_id': None,
            'active_channel_id': None,
        }
        send_to_agent(agent_id, payload)
        broadcast_supervisor({
            'type':      'agent_status_changed',
            'agent_id':  agent_id,
            'status':    'wrapup',
            'since':     now.isoformat(),
        })

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
        from agents.models import AgentStatus
        from django.db.models import F

        call_log, _ = CallLog.objects.get_or_create(
            channel_id=channel_id,
            defaults={
                'lead_id':    lead_id or None,
                'direction':  'outbound',
                'started_at': timezone.now(),
            }
        )
        CallLog.objects.filter(id=call_log.id).update(
            status='completed',
            bridge_id=bridge_id or call_log.bridge_id,
            duration=duration_seconds,
            answered_at=call_log.answered_at or timezone.now(),
            ended_at=timezone.now(),
            updated_at=timezone.now(),
        )

        # Increment today's agent stats in DB (not virtual JS)
        if call_log.agent_id:
            AgentStatus.objects.filter(user_id=call_log.agent_id).update(
                calls_today=F('calls_today') + 1,
                talk_time_today=F('talk_time_today') + (duration_seconds or 0),
            )

        return call_log.id

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

    def _get_campaign_info(self, campaign_id):
        from campaigns.models import Campaign
        try:
            c = Campaign.objects.get(id=campaign_id)
            return {'id': c.id, 'name': c.name}
        except Exception:
            return {}

    def _ws_call_incoming(self, agent_id, call_id, lead_info, campaign_info):
        from core.ws_utils import send_to_agent, call_incoming_event
        send_to_agent(agent_id, call_incoming_event(call_id, lead_info, campaign_info))

    def _ws_call_connected(self, agent_id, call_id, bridge_id, lead_info):
        from core.ws_utils import send_to_agent

        now = timezone.now()
        send_to_agent(agent_id, {
            'type':      'call_connected',
            'call_id':   call_id,
            'bridge_id': bridge_id,
            'lead':      lead_info,
            'since':     now.isoformat(),   # ← FIX: dashboard was using new Date()
        })

    def _ws_call_ended(self, agent_id, call_id, call_log_id=None):
        from core.ws_utils import send_to_agent, call_ended_event
        send_to_agent(
            agent_id,
            call_ended_event(call_id, needs_disposition=True, call_log_id=call_log_id),
        )


# ─── WebSocket Loop ───────────────────────────────────────────────────────────

def _reset_stuck_on_call_agents(ari: 'ARIClient'):
    """
    On ARI startup: find agents stuck in ringing/on_call with no live Asterisk
    channel
    (left over from a previous process crash / restart) and recover them.

    Agents WITH a live channel are left alone — the event handler will clean up
    when those calls naturally end.  Agents WITHOUT a live channel are moved to
    wrapup (so they can disposition) or ready (if no call log exists).
    """
    from agents.models import AgentStatus
    from core.ws_utils import send_to_agent

    channels = ari.get('/channels') or []
    live_ids  = {ch.get('id') for ch in channels if isinstance(ch, dict)}

    stuck = AgentStatus.objects.filter(
        status__in=['ringing', 'on_call']
    ).select_related('user')
    count = 0
    for st in stuck:
        if st.active_channel_id and st.active_channel_id in live_ids:
            continue  # genuinely on a live call — don't touch

        logger.warning(
            'Startup cleanup: %s stuck %s (channel=%s absent from ARI)',
            st.user.username, st.status, st.active_channel_id,
        )
        if st.active_call_log_id:
            # Call log exists → transition to wrapup so agent can dispose
            AgentStatus.objects.filter(pk=st.pk).update(
                status='wrapup',
                wrapup_started_at=timezone.now(),
                wrapup_call_log_id=st.active_call_log_id,
                active_channel_id=None,
                updated_at=timezone.now(),
            )
            send_to_agent(st.user_id, {
                'type': 'status_changed', 'status': 'wrapup', 'display': 'Wrap-up',
            })
        else:
            # No call log → just return to ready
            AgentStatus.objects.filter(pk=st.pk).update(
                status='ready',
                status_changed_at=timezone.now(),
                active_channel_id=None,
                active_lead_id=None,
                call_started_at=None,
                updated_at=timezone.now(),
            )
            send_to_agent(st.user_id, {
                'type': 'status_changed', 'status': 'ready', 'display': 'Ready',
            })
        count += 1

    if count:
        logger.info('ARI startup cleanup: recovered %d stuck agent(s)', count)


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
    delay           = 2   # initial reconnect delay (seconds)
    max_delay       = 60
    startup_cleanup = True  # run cleanup only on first successful connection

    while True:
        try:
            logger.info(f'ARI worker connecting to {server_config["ARI_HOST"]}:{server_config["ARI_PORT"]} …')
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                delay = 2  # reset on successful connection
                logger.info('ARI worker connected [OK]')

                # Update server status in DB
                await sync_to_async(_mark_server_connected)(server_config['ARI_HOST'])

                # On first connection: reset any agents stuck in on_call from a
                # previous process run (crash recovery).
                if startup_cleanup:
                    await sync_to_async(_reset_stuck_on_call_agents)(ari)
                    startup_cleanup = False

                async for raw in ws:
                    event = json.loads(raw)
                    await handler.handle(event)

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake,   # includes InvalidMessage
        ) as e:
            logger.warning('ARI WebSocket error: %s. Reconnecting in %ss …', e or type(e).__name__, delay)
        except OSError as e:
            # Covers ConnectionRefusedError, ConnectionResetError, WinError 64, etc.
            msg = str(e).strip() or type(e).__name__
            logger.warning('ARI connection error: %s. Reconnecting in %ss …', msg, delay)
        except Exception as e:
            logger.exception('Unexpected ARI error: %s. Reconnecting in %ss …', e, delay)

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
