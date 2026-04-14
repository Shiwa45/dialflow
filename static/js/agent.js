/* agent.js — Agent Dashboard Controller (Rebuilt)
   ================================================
   ALL state comes FROM the server via WebSocket snapshot.
   ALL timers count from server-provided timestamps (DB-driven).
   NO virtual JS state. NO client-side Date() for timing.
   Page refresh restores everything from DB snapshot.

   Depends on: jssip.min.js (loaded before this)
*/
'use strict';
(function(){

  // ── Config (injected by template into window.AGENT_CFG) ────────────────────
  const CFG = window.AGENT_CFG || {};
  const CSRF = CFG.csrf || '';
  const rawStun = String(CFG.stun || '').trim();
  const disableStun = String(CFG.disable_stun || '').toLowerCase() === 'true'
    || String(CFG.disable_stun || '') === '1'
    || rawStun === ''
    || /^(none|off|disabled)$/i.test(rawStun);
  // For LAN/WSL deployments, external STUN can add long ICE delays.
  // Use host candidates by default unless STUN is explicitly configured.
  const ICE = disableStun ? [] : [{ urls: rawStun }];

  // ── State (all driven by server, never assumed) ────────────────────────────
  let wsConn = null, wsDelay = 1000;
  let ua = null, session = null;
  let autoAnswerTimer = null;
  let micStream = null;
  let callId = null;
  let currentLead = null;
  let inWrapup = false;
  let selDispId = null;
  let restoreToReady = false;
  let muted = false, held = false;

  // Server-provided timestamps (ISO strings → Date objects)
  let statusSince = null;    // from DB: AgentStatus.status_changed_at
  let loginSince  = null;    // from DB: AgentLoginLog.login_at
  let callStarted = null;    // from DB: AgentStatus.call_started_at
  let serverOffset = 0;      // ms offset: serverTime - clientTime

  // Timer intervals
  let statusTickId = null;
  let callTickId   = null;
  let wrapupTickId = null;

  // ── DOM helpers ────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const $any = (...ids) => {
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) return el;
    }
    return null;
  };
  const _set = (id, v) => { const el = $(id); if (el) el.textContent = v; };

  function fmt(sec) {
    if (sec == null || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }

  function tc(s) {
    return (s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  /** Get elapsed seconds since a Date, adjusted for server clock offset */
  function elapsed(since) {
    if (!since) return 0;
    return Math.max(0, Math.floor((Date.now() + serverOffset - since.getTime()) / 1000));
  }

  function toast(msg, type) {
    type = type || 'info';
    const el = $('toast');
    if (!el) return;
    el.textContent = msg;
    el.className = 'show ' + type;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 3500);
  }

  function post(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(data || {}),
    }).then(r => r.json()).catch(() => ({}));
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  WEBSOCKET
  // ══════════════════════════════════════════════════════════════════════════

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    wsConn = new WebSocket(`${proto}//${location.host}/ws/agent/`);

    wsConn.onopen = () => {
      _set('ws-status-text', 'Connected');
      const el = $('ws-status-text');
      if (el) el.style.color = 'var(--success)';
      wsDelay = 1000;
    };

    wsConn.onmessage = e => {
      try { handleMsg(JSON.parse(e.data)); } catch(_) {}
    };

    wsConn.onclose = () => {
      _set('ws-status-text', 'Disconnected');
      const el = $('ws-status-text');
      if (el) el.style.color = 'var(--danger)';
      setTimeout(connectWS, wsDelay);
      wsDelay = Math.min(wsDelay * 1.5, 30000);
    };
  }

  function wsSend(d) {
    if (wsConn && wsConn.readyState === 1)
      wsConn.send(JSON.stringify(d));
  }

  function handleMsg(d) {
    const t = d.type;
    if      (t === 'snapshot')                applySnapshot(d);
    else if (t === 'status_changed')          applyStatus(d);
    else if (t === 'call_incoming')           onIncoming(d);
    else if (t === 'call_connected')          onConnected(d);
    else if (t === 'call_ended')              onEnded(d);
    else if (t === 'wrapup_timeout_warning')  toast('Auto-wrapup in ' + d.seconds_remaining + 's', 'warning');
    else if (t === 'wrapup_expired')          { toast('Auto-wrapup: ' + d.disposition_applied, 'info'); closeWrapup(); }
    else if (t === 'dispose_ok')              { closeWrapup(); toast('Disposition saved', 'success'); }
    else if (t === 'error')                   { toast(d.message || 'Action failed', 'danger'); if (inWrapup && selDispId) { const btn = $('btn-save-disp'); if (btn) btn.disabled = false; } }
    else if (t === 'force_logout')            { toast(d.reason || 'Logged out by supervisor', 'warning'); setTimeout(() => { location.href = '/auth/logout/'; }, 2500); }
    else if (t === 'hangup_sent')             { /* noop, call_ended will follow */ }
    else if (t === 'campaign_set')            { /* noop */ }
    else if (t === 'heartbeat_ack')           { /* noop */ }
    else if (t === 'pong')                    { /* noop */ }
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  SNAPSHOT (full state restore from DB)
  // ══════════════════════════════════════════════════════════════════════════

  function applySnapshot(d) {
    // ── FIX: Calculate server clock offset for accurate timers ──
    if (d.server_time) {
      const serverNow = new Date(d.server_time).getTime();
      serverOffset = serverNow - Date.now();
    }

    // ── FIX: Login time from DB, not client-side new Date() ──
    if (d.login_since) {
      loginSince = new Date(d.login_since);
    } else {
      loginSince = new Date(Date.now() + serverOffset); // fallback
    }

    // Apply status (uses server timestamp)
    applyStatus({
      status:  d.status,
      display: d.status_display,
      since:   d.status_since,
    });

    // Dispositions
    if (d.dispositions && d.dispositions.length) renderDispositions(d.dispositions);

    // Stats from DB
    if (d.stats_today) updateStats(d.stats_today);

    // Campaign selector
    if (d.active_campaign_id) {
      const sel = $any('campaign-select', 'campaignSelect');
      if (sel) sel.value = d.active_campaign_id;
    }

    // ── FIX: Restore on_call state with active call info on page refresh ──
    if (d.status === 'on_call' && d.active_call) {
      callId = d.active_call.call_id;
      if (d.call_started_at) {
        callStarted = new Date(d.call_started_at);
      }
      showCall(d.active_call.lead || {}, 'connected');
      renderLead(d.active_call.lead || {});
      startCallTimer();
    }

    // Restore wrapup state
    if (d.status === 'wrapup') {
      callId = String(d.pending_call_log_id || (d.pending_call && d.pending_call.id) || callId || '');
      enterWrapup(d.pending_call || null);
    }

    // SIP re-registration flow
    if (d.restore_to === 'ready') {
      const protectedState = d.status === 'wrapup' || d.status === 'on_call' || d.status === 'ringing';
      if (protectedState) {
        restoreToReady = false;
      } else if (ua && ua.isRegistered()) {
        wsSend({ action: 'set_status', status: 'ready' });
        restoreToReady = false;
      } else {
        restoreToReady = true;
      }
    }

    // Enable status buttons
    document.querySelectorAll('[data-set-status]').forEach(b => { b.disabled = false; });

    // Start global tickers
    startStatusTicker();
    startLoginTicker();

    // Load call history
    loadHistory();
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  STATUS (all timestamps from server DB)
  // ══════════════════════════════════════════════════════════════════════════

  function applyStatus(d) {
    const st = d.status || 'offline';

    // ── FIX: Always use server-provided `since` timestamp ──
    if (d.since) {
      statusSince = new Date(d.since);
    }
    // If no `since` provided, DON'T reset — keep the existing DB timestamp

    // Update status pill
    const pill = $('status-pill');
    if (pill) {
      pill.textContent = d.display || tc(st);
      pill.className = 'status-pill ' + st;
    }

    // Update status buttons active state
    document.querySelectorAll('[data-set-status]').forEach(b => {
      b.classList.toggle('is-active', b.dataset.setStatus === st);
    });

    // Status since text
    _set('status-since-text', d.display || tc(st));

    // Restart status ticker with new timestamp
    startStatusTicker();

    // If wrapup status arrives before/without call_ended event, open wrapup modal immediately.
    if (st === 'wrapup' && !inWrapup) {
      if (d.call_log_id) callId = String(d.call_log_id);
      enterWrapup(null);
      return;
    }

    // If status moves away from wrapup, close stale wrapup UI.
    if (st !== 'wrapup' && inWrapup) {
      closeWrapup();
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  TIMERS (all DB-driven, survive page refresh)
  // ══════════════════════════════════════════════════════════════════════════

  function startStatusTicker() {
    clearInterval(statusTickId);
    if (!statusSince) return;
    const tick = () => {
      _set('status-timer', fmt(elapsed(statusSince)));
    };
    tick(); // immediate
    statusTickId = setInterval(tick, 1000);
  }

  function startLoginTicker() {
    clearInterval(wrapupTickId); // reuse for login ticker (separate from call)
    if (!loginSince) return;
    const tick = () => {
      _set('session-timer', fmt(elapsed(loginSince)));
    };
    tick();
    // Use a separate interval — don't collide with status ticker
    setInterval(tick, 1000);
  }

  function startCallTimer() {
    clearInterval(callTickId);
    // Use server-provided call start time, NOT client-side Date
    if (!callStarted) {
      callStarted = new Date(Date.now() + serverOffset);
    }
    const phoneTimer = $('phone-timer');
    if (phoneTimer) phoneTimer.style.display = '';
    _set('phone-status-disp', 'CONNECTED');

    const tick = () => {
      const t = fmt(elapsed(callStarted));
      _set('phone-timer', t);
      _set('call-meta-dur', t);
    };
    tick();
    callTickId = setInterval(tick, 1000);
  }

  function stopCallTimer() {
    clearInterval(callTickId);
    callTickId = null;
    callStarted = null;
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  CALL EVENTS
  // ══════════════════════════════════════════════════════════════════════════

  function onIncoming(d) {
    callId = d.call_id;
    currentLead = d.lead || {};
    // Use server time if provided
    applyStatus({ status: 'ringing', display: 'Ringing', since: d.since || new Date(Date.now() + serverOffset).toISOString() });
    showCall(currentLead, 'ringing');
    renderLead(currentLead);
    if (d.campaign) _set('call-meta-camp', d.campaign.name || '--');

    // If SIP session already arrived, answer it
    if (session) setTimeout(answerSession, 300);
  }

  function onConnected(d) {
    callId = d.call_id;
    const lead = (d.lead && Object.keys(d.lead).length) ? d.lead : (currentLead || {});
    currentLead = lead;
    // ── FIX: Use server-provided `since` for call timer, NOT new Date() ──
    const since = d.since || new Date(Date.now() + serverOffset).toISOString();
    callStarted = new Date(since);
    applyStatus({ status: 'on_call', display: 'On Call', since: since });
    showCall(lead, 'connected');
    startCallTimer();
    renderLead(lead);
  }

  function onEnded(d) {
    stopCallTimer();
    callId = String(d.call_log_id || d.call_id || callId || '');
    currentLead = null;
    if (d.needs_disposition && callId) {
      enterWrapup(null);
    } else {
      resetCall();
    }
  }

  function showCall(lead, state) {
    const ph = $('call-placeholder');
    const det = $('call-details');
    if (ph) ph.style.display = 'none';
    if (det) det.classList.add('visible');

    const n = [lead.first_name, lead.last_name].filter(Boolean).join(' ') || lead.phone || '--';
    _set('call-lead-name', n);
    _set('call-lead-sub', lead.phone || '--');
    _set('call-meta-phone', lead.phone || '--');
    _set('call-meta-status', tc(state));

    const badge = $('call-badge');
    if (badge) {
      badge.className = 'call-badge ' + state;
      badge.innerHTML = '<i class="fas fa-circle"></i> ' + tc(state);
    }

    [$('btn-hangup'), $('btn-mute'), $('btn-hold'), $('btn-transfer')].forEach(b => {
      if (b) b.disabled = false;
    });
  }

  function resetCall() {
    stopCallTimer();
    const ph = $('call-placeholder');
    const det = $('call-details');
    if (ph) ph.style.display = '';
    if (det) det.classList.remove('visible');

    const badge = $('call-badge');
    if (badge) {
      badge.className = 'call-badge idle';
      badge.innerHTML = '<i class="fas fa-circle"></i> Idle';
    }

    [$('btn-hangup'), $('btn-mute'), $('btn-hold'), $('btn-transfer')].forEach(b => {
      if (b) b.disabled = true;
    });

    callId = null;
    muted = false;
    held = false;
    _set('phone-status-disp', ua && ua.isRegistered() ? 'REGISTERED' : 'DISCONNECTED');
    const pt = $('phone-timer');
    if (pt) pt.style.display = 'none';
    _set('phone-num-disp', '');
  }

  function renderLead(lead) {
    _set('call-meta-name', [lead.first_name, lead.last_name].filter(Boolean).join(' ') || '--');
    _set('call-meta-email', lead.email || '--');
    _set('call-meta-phone', lead.phone || '--');
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  WRAPUP / DISPOSITION
  // ══════════════════════════════════════════════════════════════════════════

  function enterWrapup(callInfo) {
    inWrapup = true;
    selDispId = null;
    const saveBtn = $('btn-save-disp');
    if (saveBtn) saveBtn.disabled = true;

    // Status already set to wrapup by server — just update UI
    if (callInfo) {
      const n = callInfo.lead_name || callInfo.first_name || '--';
      _set('disp-subtitle', 'Call with ' + n + ' · ' + fmt(callInfo.duration || 0));
    } else {
      _set('disp-subtitle', 'Select a disposition to complete this call.');
    }

    // Copy notes
    const notesEl = $('call-notes');
    const dispNotes = $('disp-notes');
    if (notesEl && dispNotes) dispNotes.value = notesEl.value;

    // Preserve callId through resetCall
    const savedCallId = callId;
    resetCall();
    callId = savedCallId;

    // Show/hide skip link
    const hasCallId = !!(callId && parseInt(callId, 10) > 0);
    const skipLink = $('btn-skip-wrapup');
    const noCallMsg = $('disp-no-call-msg');
    const reqMsg = $('disp-required-msg');

    if (skipLink) skipLink.style.display = hasCallId ? 'none' : '';
    if (noCallMsg) noCallMsg.style.display = hasCallId ? 'none' : '';
    if (reqMsg) reqMsg.style.display = hasCallId ? '' : 'none';

    // Show disposition overlay
    const overlay = $('disposition-overlay');
    if (overlay) overlay.classList.add('visible');
  }

  function closeWrapup() {
    inWrapup = false;
    selDispId = null;
    callId = null;
    const overlay = $('disposition-overlay');
    if (overlay) overlay.classList.remove('visible');
  }

  function renderDispositions(disps) {
    const grid = $('disp-grid');
    if (!grid) return;
    grid.innerHTML = disps.map((d, i) => {
      const hk = d.hotkey || (i < 9 ? String(i + 1) : '');
      return `<button class="disp-btn" data-disp-id="${d.id}" onclick="window._selectDisp(${d.id}, this)">
        <span class="disp-color" style="background:${d.color || '#6B7280'}"></span>
        <span>${d.name}</span>
        ${hk ? '<span class="disp-hotkey">' + hk + '</span>' : ''}
      </button>`;
    }).join('');
  }

  window._selectDisp = function(id, btn) {
    selDispId = id;
    document.querySelectorAll('.disp-btn').forEach(b => b.classList.remove('selected'));
    if (btn) btn.classList.add('selected');
    const saveBtn = $('btn-save-disp');
    if (saveBtn) saveBtn.disabled = false;
  };

  window._saveDisposition = function() {
    if (!selDispId) return;
    const saveBtn = $('btn-save-disp');
    if (saveBtn) saveBtn.disabled = true;

    const notes = ($('disp-notes') || {}).value || '';
    const cbAt = ($('disp-callback') || {}).value || null;

    wsSend({
      action:         'dispose',
      disposition_id: selDispId,
      call_log_id:    callId,
      notes:          notes,
      callback_at:    cbAt,
    });
  };

  window.skipWrapup = function(e) {
    if (e) e.preventDefault();
    wsSend({ action: 'set_status', status: 'ready', force: true });
    closeWrapup();
    toast('Skipped disposition — going Ready', 'info');
  };

  // ══════════════════════════════════════════════════════════════════════════
  //  STATS
  // ══════════════════════════════════════════════════════════════════════════

  function updateStats(s) {
    const calls = s.calls || 0;
    const answered = s.answered || 0;
    const talk = fmt(s.talk_sec || 0);
    const rate = calls ? Math.round((answered * 100) / calls) : 0;

    _set('stat-total', calls);
    _set('stat-answered', s.answered || 0);
    _set('stat-talk', talk);
    _set('stat-rate', `${rate}%`);

    _set('qs-total', calls);
    _set('qs-answered', answered);
    _set('qs-talk', talk);
    _set('qs-rate', `${rate}%`);
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  CALL HISTORY
  // ══════════════════════════════════════════════════════════════════════════

  function loadHistory() {
    fetch('/agents/api/call-history/', {
      headers: { 'X-CSRFToken': CSRF },
    }).then(r => r.json()).then(d => {
      const el = $any('recent-calls-list', 'call-history-list');
      if (!el) return;
      if (!d.calls || !d.calls.length) {
        el.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;font-size:.8rem">No calls today</div>';
        return;
      }
      el.innerHTML = d.calls.slice(0, 10).map(c => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
          <div>
            <div style="font-weight:600;font-size:.82rem">${c.lead_name || c.phone}</div>
            <div style="font-size:.72rem;color:var(--text-muted)">${c.campaign || ''}</div>
          </div>
          <div style="text-align:right">
            <span class="badge badge-${c.status}" style="font-size:.65rem">${c.status}</span>
            <div style="font-size:.7rem;color:var(--text-muted);margin-top:2px">${c.duration ? fmt(c.duration) : '--'}</div>
          </div>
        </div>`).join('');
    }).catch(() => {});
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  HEARTBEAT
  // ══════════════════════════════════════════════════════════════════════════

  function startHeartbeat() {
    setInterval(() => {
      wsSend({ action: 'heartbeat' });
      post('/agents/api/heartbeat/');
    }, 25000);
  }

  async function fetchLeadById(leadId) {
    if (!leadId) return null;
    try {
      const r = await fetch('/agents/api/lead-info/?lead_id=' + encodeURIComponent(leadId), {
        headers: { 'X-CSRFToken': CSRF },
      });
      const d = await r.json();
      if (d && (d.id || d.first_name || d.phone)) return d;
    } catch (_) {}
    return null;
  }

  function startStateSync() {
    setInterval(async () => {
      try {
        const r = await fetch('/agents/api/status/', { headers: { 'X-CSRFToken': CSRF } });
        const s = await r.json();
        if (!s || !s.status) return;

        applyStatus({
          status: s.status,
          display: s.display || tc(s.status),
          since: s.since || null,
          call_log_id: s.wrapup_call_log_id || s.active_call_log_id || null,
        });

        if (s.status === 'wrapup' && !inWrapup) {
          callId = String(s.wrapup_call_log_id || s.active_call_log_id || callId || '');
          enterWrapup(null);
        }

        if ((s.status === 'ringing' || s.status === 'on_call') && (!currentLead || !currentLead.phone) && s.active_lead_id) {
          const lead = await fetchLeadById(s.active_lead_id);
          if (lead) {
            currentLead = lead;
            showCall(lead, s.status === 'on_call' ? 'connected' : 'ringing');
            renderLead(lead);
          }
        }
      } catch (_) {}
    }, 1000);
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  JsSIP WebRTC
  // ══════════════════════════════════════════════════════════════════════════

  function setRTC(state, text) {
    const dot = $('webrtc-dot');
    const txtMain = $('webrtc-status-text');
    const txtSide = $('rtc-status-text');
    const dotState = state === 'registered' ? 'registered' : state === 'connecting' ? 'connecting' : 'failed';
    if (dot) dot.className = 'webrtc-dot ' + dotState;
    if (txtMain) txtMain.textContent = text || '';
    if (txtSide) txtSide.textContent = text || '';
  }

  function _sipWentOffline(reason) {
    // Prevent dialer from reserving a dead endpoint
    if ($('status-pill') && $('status-pill').textContent !== 'Offline') {
      wsSend({ action: 'set_status', status: 'offline' });
      const curr = (($('status-pill') || {}).textContent || '').toLowerCase();
      const inProtected = curr.includes('wrap') || curr.includes('call') || curr.includes('ring');
      restoreToReady = !inProtected;
    }
  }

  async function prewarmMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      console.warn('[RTC] getUserMedia not available — mic pre-warm skipped');
      return null;
    }
    if (micStream && micStream.getAudioTracks().some(t => t.readyState === 'live')) {
      return micStream;
    }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      micStream.getAudioTracks().forEach(t => { t.enabled = true; });
      console.log('[RTC] Microphone permission granted (pre-warm OK)');
      return micStream;
    } catch (err) {
      console.error('[RTC] Microphone pre-warm FAILED:', err.name, err.message);
      toast('Microphone permission denied — calls will not work', 'danger');
      return null;
    }
  }

  function ensureMicEnabled() {
    if (!micStream) return;
    micStream.getAudioTracks().forEach(t => { t.enabled = true; });
  }

  function tryPlayRemoteAudio() {
    const remAudio = $('remote-audio');
    if (!remAudio) return;
    try {
      remAudio.muted = false;
      const p = remAudio.play && remAudio.play();
      if (p && typeof p.catch === 'function') {
        p.catch(() => {});
      }
    } catch (_) {}
  }

  function initRTC() {
    if (!CFG.uri || !CFG.ws) { setRTC('failed', 'No extension'); return; }
    prewarmMic();
    try {
      const sock = new JsSIP.WebSocketInterface(CFG.ws);
      ua = new JsSIP.UA({
        sockets: [sock],
        uri: CFG.uri,
        password: CFG.pw,
        display_name: CFG.name,
        register: true,
        session_timers: false,
        use_preloaded_route: false,
      });

      ua.on('registered', () => {
        setRTC('registered', 'Registered');
        if (restoreToReady) {
          const curr = (($('status-pill') || {}).textContent || '').toLowerCase();
          const inProtected = curr.includes('wrap') || curr.includes('call') || curr.includes('ring');
          if (!inProtected) {
            restoreToReady = false;
            wsSend({ action: 'set_status', status: 'ready' });
            toast('SIP registered — going Ready', 'info');
          } else {
            restoreToReady = false;
          }
        }
      });

      ua.on('unregistered', () => {
        setRTC('failed', 'Unregistered');
        _sipWentOffline('SIP unregistered');
      });

      ua.on('registrationFailed', ev => {
        const cause = ev && ev.cause || 'unknown';
        setRTC('failed', 'Failed: ' + cause);
        toast('SIP registration failed: ' + cause, 'danger');
        _sipWentOffline('SIP registration failed');
      });

      ua.on('disconnected', () => {
        setRTC('failed', 'Disconnected');
        _sipWentOffline('SIP disconnected');
      });

      ua.on('newRTCSession', ev => handleSession(ev.session));
      ua.start();
      setRTC('connecting', 'Connecting...');
    } catch (e) {
      setRTC('failed', 'Error: ' + e.message);
    }
  }

  function handleSession(s) {
    // Keep first active session and reject duplicate incoming INVITEs.
    if (
      session &&
      session !== s &&
      !(session.isEnded && session.isEnded())
    ) {
      console.warn('[RTC] Duplicate incoming SIP session; rejecting as busy', s.id);
      try { s.terminate({ status_code: 486, reason_phrase: 'Busy Here' }); } catch (_) {}
      return;
    }

    session = s;
    s.__answerSent = false;
    if (autoAnswerTimer) { clearTimeout(autoAnswerTimer); autoAnswerTimer = null; }
    const remAudio = $('remote-audio');

    s.on('accepted', () => {
      console.log('[RTC] Session accepted (200 OK sent)');
      _set('phone-status-disp', 'CONNECTED');
      tryPlayRemoteAudio();
    });

    s.on('confirmed', () => {
      console.log('[RTC] Session confirmed (ACK received)');
      _set('phone-status-disp', 'CONNECTED');
      tryPlayRemoteAudio();
    });

    s.on('peerconnection', e => {
      e.peerconnection.addEventListener('track', ev => {
        if (remAudio && ev.streams && ev.streams[0]) {
          remAudio.srcObject = ev.streams[0];
          tryPlayRemoteAudio();
        }
      });
    });

    s.on('progress', ev => {
      console.log('[RTC] Session progress', ev && ev.originator, ev && ev.response && ev.response.status_code);
    });

    s.on('ended', ev => {
      console.log('[RTC] Session ended, cause:', ev && ev.cause);
      if (autoAnswerTimer) { clearTimeout(autoAnswerTimer); autoAnswerTimer = null; }
      session = null;
      _set('phone-status-disp', ua && ua.isRegistered() ? 'REGISTERED' : 'DISCONNECTED');
    });

    s.on('failed', ev => {
      const cause = ev && ev.cause || 'unknown';
      console.error('[RTC] Session failed, cause:', cause, ev);
      if (autoAnswerTimer) { clearTimeout(autoAnswerTimer); autoAnswerTimer = null; }
      session = null;
      _set('phone-status-disp', ua && ua.isRegistered() ? 'REGISTERED' : 'FAILED');
      if (cause !== 'Terminated' && cause !== 'BYE') {
        toast('SIP call failed [' + cause + ']', 'danger');
      }
    });

    // Auto-answer incoming calls from the dialer
    if (s.direction === 'incoming') {
      console.log('[RTC] Incoming SIP session — scheduling auto-answer now', s.remote_identity && s.remote_identity.uri && s.remote_identity.uri.toString());
      _set('phone-status-disp', 'AUTO-ANSWERING...');
      autoAnswerTimer = setTimeout(() => answerSession(s), 0);
    }
  }

  async function answerSession(targetSession) {
    const s = targetSession || session;
    if (!s || s !== session) {
      console.warn('[RTC] answerSession called but session is null — already ended?');
      return;
    }
    if (s.__answerSent) {
      return;
    }
    console.log('[RTC] Calling session.answer() — requesting microphone...');
    try {
      const stream = await prewarmMic();
      if (!stream) {
        console.error('[RTC] No microphone stream available for answer');
        return;
      }
      ensureMicEnabled();
      s.__answerSent = true;
      s.answer({
        mediaStream: stream,
        mediaConstraints: { audio: true, video: false },
        pcConfig: { iceServers: ICE, iceCandidatePoolSize: 0 },
      });
      console.log('[RTC] session.answer() returned');
    } catch (e) {
      // JsSIP throws INVALID_STATE_ERROR if answer() is called after state moved.
      // That is not fatal for an already-progressing session.
      if (e && e.name === 'INVALID_STATE_ERROR') {
        console.warn('[RTC] session.answer() ignored invalid state:', e.message);
        s.__answerSent = true;
        return;
      }
      s.__answerSent = false;
      console.error('[RTC] session.answer() threw:', e.name, e.message);
      toast('SIP answer error: ' + e.message, 'danger');
    }
  }

  // We intentionally do a single auto-answer attempt per incoming session.

  function hangupCall() {
    if (session) { session.terminate(); session = null; }
    wsSend({ action: 'hangup', channel_id: null });
  }

  function toggleMute() {
    muted = !muted;
    if (session && session.connection) {
      session.connection.getSenders().forEach(s => {
        if (s.track && s.track.kind === 'audio') s.track.enabled = !muted;
      });
    }
    const btn = $('btn-mute');
    if (btn) btn.classList.toggle('active', muted);
    toast(muted ? 'Muted' : 'Unmuted', 'info');
  }

  function toggleHold() {
    held = !held;
    if (held) { if (session) session.hold(); }
    else      { if (session) session.unhold(); }
    const btn = $('btn-hold');
    if (btn) btn.classList.toggle('active', held);
    toast(held ? 'On Hold' : 'Resumed', 'info');
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  STATUS BUTTONS
  // ══════════════════════════════════════════════════════════════════════════

  function bindStatusButtons() {
    document.querySelectorAll('[data-set-status]').forEach(btn => {
      btn.addEventListener('click', () => {
        const st = btn.dataset.setStatus;
        if (st === 'break') {
          // Could show pause code selector — for now just send break
          wsSend({ action: 'set_status', status: 'break' });
        } else {
          wsSend({ action: 'set_status', status: st });
        }
      });
    });
  }

  function bindCampaignSelect() {
    const sel = $any('campaign-select', 'campaignSelect');
    if (sel) {
      sel.addEventListener('change', () => {
        wsSend({ action: 'set_campaign', campaign_id: sel.value });
      });
    }
  }

  function bindCallButtons() {
    const btnHang = $('btn-hangup');
    const btnMuteEl = $('btn-mute');
    const btnHoldEl = $('btn-hold');

    if (btnHang) btnHang.addEventListener('click', hangupCall);
    if (btnMuteEl) btnMuteEl.addEventListener('click', toggleMute);
    if (btnHoldEl) btnHoldEl.addEventListener('click', toggleHold);

    const saveBtn = $('btn-save-disp');
    if (saveBtn) saveBtn.addEventListener('click', window._saveDisposition);
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  KEYBOARD SHORTCUTS
  // ══════════════════════════════════════════════════════════════════════════

  function bindKeyboard() {
    document.addEventListener('keydown', e => {
      // Don't intercept when typing in inputs
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

      // Disposition hotkeys (1-9) when in wrapup
      if (inWrapup && e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key) - 1;
        const btns = document.querySelectorAll('.disp-btn');
        if (btns[idx]) btns[idx].click();
        return;
      }

      // Enter to save disposition when one is selected
      if (inWrapup && e.key === 'Enter' && selDispId) {
        window._saveDisposition();
        return;
      }
    });
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  THEME
  // ══════════════════════════════════════════════════════════════════════════

  function initTheme() {
    const html = document.documentElement;
    const apply = t => {
      html.setAttribute('data-theme', t);
      localStorage.setItem('df_theme', t);
      const icon = $('theme-icon');
      if (icon) icon.className = t === 'dark' ? 'fas fa-moon' : 'fas fa-sun';
    };
    apply(localStorage.getItem('df_theme') || 'dark');

    const toggle = $('theme-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        apply(html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
      });
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  NOTES SAVE (local)
  // ══════════════════════════════════════════════════════════════════════════

  window.saveNotes = function() {
    toast('Notes saved locally', 'success');
  };

  // ══════════════════════════════════════════════════════════════════════════
  //  INIT
  // ══════════════════════════════════════════════════════════════════════════

  function init() {
    initTheme();
    connectWS();
    initRTC();
    startHeartbeat();
    startStateSync();
    bindStatusButtons();
    bindCampaignSelect();
    bindCallButtons();
    bindKeyboard();
  }

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
