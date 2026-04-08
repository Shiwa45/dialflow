/* agent.js — Agent Dashboard Controller
   Handles: WebSocket state, JsSIP WebRTC, call UI, wrapup, dispositions.
   Loaded only on agents/dashboard.html
   Depends on: app.js (DF, ReconnectingWS)

   All state changes come FROM the server via WebSocket.
   The browser never assumes state — it only reflects what the DB says.
*/
'use strict';

// ── Configuration (injected by template) ──────────────────────────────────────
// window.AGENT_CFG must be set before this script runs:
//   { wsUrl, sipUri, extension, password, domain, displayName, stun }

// ── Module ────────────────────────────────────────────────────────────────────
window.Agent = (() => {

  // ── State ──────────────────────────────────────────────────────────────────
  let ws, ua, session;
  let agentStatus     = 'offline';
  let statusSince     = null;
  let callId          = null;
  let callStartedAt   = null;
  let inWrapup        = false;
  let selectedDispId  = null;
  let wrapupTimeout   = 0;
  let isMuted         = false;
  let isOnHold        = false;
  let statusTickStop  = null;
  let callTickStop    = null;
  let wrapupTickStop  = null;

  // ── DOM helpers ────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);

  // ── WebSocket ──────────────────────────────────────────────────────────────
  function initWS() {
    ws = new ReconnectingWS('/ws/agent/', { onmessage: handleMsg });
  }

  function handleMsg(data) {
    const handlers = {
      snapshot:               applySnapshot,
      status_changed:         applyStatusChange,
      call_incoming:          onCallIncoming,
      call_connected:         onCallConnected,
      call_ended:             onCallEnded,
      wrapup_timeout_warning: onWrapupWarning,
      wrapup_expired:         onWrapupExpired,
      dispose_ok:             onDisposeOk,
      error:                  d => DF.toast(d.message || 'Action failed', 'error'),
      force_logout:           () => { window.location = '/auth/logout/'; },
    };
    handlers[data.type]?.(data);
  }

  // ── Snapshot ───────────────────────────────────────────────────────────────
  function applySnapshot(data) {
    applyStatusChange({ status: data.status, display: data.status_display, since: data.status_since });

    if (data.dispositions?.length) renderDispositions(data.dispositions);

    // Stats
    if (data.stats_today) {
      _set('statCalls',    data.stats_today.calls);
      _set('statAnswered', data.stats_today.answered);
      _set('statTalk',     DF.formatDuration(data.stats_today.talk_sec));
    }

    // Campaign selector
    if (data.active_campaign_id) {
      const sel = $('campaignSelect');
      if (sel) sel.value = data.active_campaign_id;
    }

    // Auto-wrapup config
    const camp = data.campaigns?.find(c => c.id == data.active_campaign_id);
    if (camp?.auto_wrapup_enabled) wrapupTimeout = camp.auto_wrapup_timeout;

    // Restore pending wrapup after refresh/reconnect.
    if (data.status === 'wrapup') {
      callId = String(data.pending_call_log_id || data.pending_call?.id || callId || '');
      if (callId) {
        enterWrapup(data.pending_call || null);
        if (data.wrapup_seconds_remaining > 0) startWrapupCountdown(data.wrapup_seconds_remaining);
      }
    }

    enableChips(true);
  }

  // ── Status ─────────────────────────────────────────────────────────────────
  function applyStatusChange(data) {
    agentStatus = data.status || 'offline';
    statusSince = data.since ? new Date(data.since) : new Date();

    const dot = $('statusDot');
    if (dot) { dot.className = `status-dot ${agentStatus}`; }
    _set('statusLabel', data.display || titleCase(agentStatus));

    document.querySelectorAll('.status-chip').forEach(b => {
      b.classList.toggle('is-active', b.dataset.setStatus === agentStatus);
      b.disabled = ['on_call', 'wrapup'].includes(agentStatus);
    });

    if (statusTickStop) statusTickStop();
    statusTickStop = DF.ticker(() => {
      _set('statusTimer', DF.formatDuration(DF.elapsed(statusSince)));
    });
  }

  // ── Call events ────────────────────────────────────────────────────────────
  function onCallIncoming(data) {
    callId = data.call_id;
    showCallUI(data.lead || {}, 'ringing');
    renderLeadPanel(data.lead || {}, data.campaign?.name);
  }

  function onCallConnected(data) {
    callId        = data.call_id;
    callStartedAt = new Date();
    showCallUI(data.lead || {}, 'connected');
    startCallTimer();
  }

  function onCallEnded(data) {
    stopCallTimer();
    callId = String(data.call_log_id || data.call_id || callId || '');
    if (data.needs_disposition) {
      enterWrapup(null);
    } else {
      resetCallUI();
    }
  }

  function showCallUI(lead, state) {
    _hide('callIdle');
    const ca = $('callActive');
    if (ca) ca.classList.add('visible');

    const name = [lead.first_name, lead.last_name].filter(Boolean).join(' ') || lead.phone || '—';
    _set('callAvatar',    name[0]?.toUpperCase() || '?');
    _set('callLeadName',  name);
    _set('callLeadPhone', lead.phone || '—');

    const badge = $('callBadge');
    if (badge) {
      badge.className = `call-status-badge ${state}`;
      badge.textContent = state === 'ringing' ? 'Ringing…' : state === 'connected' ? 'Connected' : state;
    }
  }

  function resetCallUI() {
    stopCallTimer();
    const ca = $('callActive');
    if (ca) ca.classList.remove('visible');
    const ci = $('callIdle');
    if (ci) ci.style.display = '';
    _set('callTimer', '00:00');
    const lb = $('leadBody');
    if (lb) lb.innerHTML = '<div style="text-align:center;padding:20px;color:var(--color-text-4);font-size:13px">No active call</div>';
    isMuted = isOnHold = false;
    callId = null;
  }

  function startCallTimer() {
    if (callTickStop) callTickStop();
    callStartedAt = callStartedAt || new Date();
    callTickStop  = DF.ticker(() => _set('callTimer', DF.formatDuration(DF.elapsed(callStartedAt))));
  }
  function stopCallTimer() { callTickStop?.(); callTickStop = null; }

  // ── Wrapup ─────────────────────────────────────────────────────────────────
  function enterWrapup(callInfo) {
    inWrapup        = true;
    selectedDispId  = null;
    const btnDisp   = $('btnDispose');
    if (btnDisp) btnDisp.disabled = true;

    if (callInfo) {
      const name = callInfo.lead_name || callInfo.first_name || '—';
      _set('dispSubtitle', `Call with ${name} • ${DF.formatDuration(callInfo.duration || 0)}`);
    }

    const notes = $('callNotes');
    const dn    = $('dispNotes');
    if (dn && notes) { dn.value = notes.value; notes.value = ''; }

    resetCallUI();
    $('dispOverlay')?.classList.add('visible');
  }

  function onWrapupWarning(data) { startWrapupCountdown(data.seconds_remaining); }

  function startWrapupCountdown(seconds) {
    if (wrapupTickStop) wrapupTickStop();
    const timerEl   = $('wrapupTimer');
    const cdEl      = $('wrapupCountdown');
    const barFill   = $('wrapupBarFill');
    const total     = wrapupTimeout || seconds;
    if (timerEl) timerEl.style.display = '';

    let remaining = seconds;
    wrapupTickStop = DF.ticker(() => {
      if (cdEl)    cdEl.textContent  = `${remaining}s`;
      if (barFill) {
        barFill.style.width = `${(remaining / total) * 100}%`;
        barFill.classList.toggle('urgent', remaining <= 15);
      }
      if (remaining <= 0) wrapupTickStop?.();
      remaining--;
    });
  }

  function onWrapupExpired(data) {
    wrapupTickStop?.();
    DF.toast(`Auto-wrapup: ${data.disposition_applied}`, 'info');
    closeWrapup();
  }

  function closeWrapup() {
    inWrapup = false;
    $('dispOverlay')?.classList.remove('visible');
    const wt = $('wrapupTimer');
    if (wt) wt.style.display = 'none';
    wrapupTickStop?.();
  }

  function onDisposeOk() {
    closeWrapup();
    DF.toast('Disposition saved', 'success');
  }

  // ── Dispositions ───────────────────────────────────────────────────────────
  function renderDispositions(disps) {
    const grid = $('dispGrid');
    if (!grid) return;
    grid.innerHTML = disps.map(d => `
      <button class="disp-btn" data-disp-id="${d.id}"
              onclick="Agent.selectDisp(${d.id}, this)">
        <span class="disp-color" style="background:${d.color}"></span>
        <span>${d.name}</span>
        ${d.hotkey ? `<span class="disp-hotkey">${d.hotkey.toUpperCase()}</span>` : ''}
      </button>`).join('');
  }

  function selectDisp(id, btn) {
    selectedDispId = id;
    document.querySelectorAll('.disp-btn').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    const bd = $('btnDispose');
    if (bd) bd.disabled = false;
    // Show callback row if this disposition is for callbacks
    const isCallback = btn.textContent.toLowerCase().includes('callback');
    const cbRow = $('callbackRow');
    if (cbRow) cbRow.style.display = isCallback ? '' : 'none';
  }

  function submitDisposition() {
    if (!selectedDispId || !callId) return;
    const parsedCallLogId = parseInt(callId, 10);
    if (!Number.isFinite(parsedCallLogId) || parsedCallLogId <= 0) {
      DF.toast('Missing call log id for disposition', 'error');
      return;
    }
    const bd = $('btnDispose');
    if (bd) bd.disabled = true;
    ws.send({
      action:         'dispose',
      disposition_id: parseInt(selectedDispId, 10),
      call_log_id:    parsedCallLogId,
      notes:          $('dispNotes')?.value?.trim() || '',
      callback_at:    $('callbackAt')?.value || null,
    });
  }

  // Keyboard shortcuts
  document.addEventListener('keydown', e => {
    if (!inWrapup || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
    document.querySelectorAll('.disp-btn').forEach(btn => {
      const hk = btn.querySelector('.disp-hotkey')?.textContent;
      if (hk && e.key.toUpperCase() === hk) btn.click();
    });
    if (e.key === 'Enter' && !$('btnDispose')?.disabled) submitDisposition();
  });

  // ── Lead panel ─────────────────────────────────────────────────────────────
  function renderLeadPanel(lead, campaign) {
    const lb = $('leadBody');
    if (!lb) return;
    const fields = [
      ['Phone',   lead.phone],
      ['Email',   lead.email],
      ['Company', lead.company],
      ['City',    [lead.city, lead.state].filter(Boolean).join(', ')],
    ].filter(([, v]) => v);

    lb.innerHTML = fields.map(([l, v]) => `
      <div class="lead-field">
        <span class="lead-field-label">${l}</span>
        <span class="lead-field-value">${v}</span>
      </div>`).join('');

    if (campaign) _set('leadCampaign', campaign);
  }

  // ── Call controls ──────────────────────────────────────────────────────────
  function toggleMute() {
    isMuted = !isMuted;
    session?.connection?.getSenders()?.forEach(s => {
      if (s.track?.kind === 'audio') s.track.enabled = !isMuted;
    });
    const btn = $('btnMute');
    if (btn) {
      btn.classList.toggle('active', isMuted);
      btn.querySelector('i').className = isMuted ? 'fa-solid fa-microphone-slash' : 'fa-solid fa-microphone';
    }
  }

  function toggleHold() {
    isOnHold = !isOnHold;
    isOnHold ? session?.hold() : session?.unhold();
    $('btnHold')?.classList.toggle('active', isOnHold);
  }

  function hangup() {
    session?.terminate();
    ws?.send({ action: 'hangup', channel_id: null });
  }

  // ── JsSIP WebRTC ───────────────────────────────────────────────────────────
  function initWebRTC(cfg) {
    if (!cfg?.sipUri || !cfg?.wsUrl) {
      setRTCStatus('failed', 'No extension configured');
      return;
    }
    try {
      const socket = new JsSIP.WebSocketInterface(cfg.wsUrl);
      ua = new JsSIP.UA({
        sockets:        [socket],
        uri:            cfg.sipUri,
        password:       cfg.password,
        display_name:   cfg.displayName,
        register:       true,
        session_timers: false,
      });

      ua.on('registered',         ()  => setRTCStatus('registered', 'Registered'));
      ua.on('unregistered',       ()  => setRTCStatus('failed',     'Unregistered'));
      ua.on('registrationFailed', e   => setRTCStatus('failed', 'Failed: ' + (e.cause || '')));
      ua.on('newRTCSession',      e   => handleRTCSession(e.session));
      ua.start();
      setRTCStatus('connecting', 'Connecting…');
    } catch (e) {
      setRTCStatus('failed', 'Init error: ' + e.message);
      console.error('[WebRTC]', e);
    }
  }

  function setRTCStatus(state, text) {
    const dot  = $('webrtcDot');
    const span = $('webrtcStatus');
    if (dot)  dot.className   = `webrtc-dot ${state}`;
    if (span) span.textContent = text;
  }

  function handleRTCSession(s) {
    if (session) { s.terminate({ status_code: 486 }); return; }
    session = s;

    s.on('peerconnection', pc => {
      pc.peerconnection.addEventListener('track', ev => {
        const audio = $('remoteAudio');
        if (audio && ev.streams?.[0]) audio.srcObject = ev.streams[0];
      });
    });

    s.on('progress', () => {
      const badge = $('callBadge');
      if (badge) {
        badge.className   = 'call-status-badge ringing';
        badge.textContent = s.direction === 'incoming' ? 'Incoming…' : 'Ringing…';
      }
    });

    s.on('accepted', () => {
      callStartedAt = new Date();
      const badge = $('callBadge');
      if (badge) { badge.className = 'call-status-badge connected'; badge.textContent = 'Connected'; }
      startCallTimer();
    });

    s.on('ended',  () => { session = null; });
    s.on('failed', () => { session = null; });
  }

  // ── Chip handlers ──────────────────────────────────────────────────────────
  function initChips() {
    document.querySelectorAll('[data-set-status]').forEach(btn => {
      btn.addEventListener('click', () => {
        ws?.send({ action: 'set_status', status: btn.dataset.setStatus });
      });
    });

    const sel = $('campaignSelect');
    sel?.addEventListener('change', () => {
      if (sel.value) ws?.send({ action: 'set_campaign', campaign_id: parseInt(sel.value) });
    });
  }

  function enableChips(enabled) {
    document.querySelectorAll('.status-chip').forEach(b => { b.disabled = !enabled; });
  }

  // ── Utilities ──────────────────────────────────────────────────────────────
  function _set(id, val) { const el = $(id); if (el) el.textContent = val; }
  function _hide(id)     { const el = $(id); if (el) el.style.display = 'none'; }
  function titleCase(s)  { return (s || '').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()); }

  // ── Boot ───────────────────────────────────────────────────────────────────
  function init(cfg) {
    initWS();
    initChips();
    if (typeof JsSIP !== 'undefined') {
      initWebRTC(cfg);
    } else {
      setRTCStatus('failed', 'JsSIP not loaded');
    }
  }

  return {
    init,
    selectDisp,
    submitDisposition,
    toggleMute,
    toggleHold,
    hangup,
  };
})();
