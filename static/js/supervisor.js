/* supervisor.js — Real-time supervisor monitor helpers (Fixed)
   =============================================================
   FIXES:
   1. Agent timers now count from server-provided status_changed_at (DB)
   2. Timers survive page refresh (snapshot includes status_changed_at)
   3. Agent status updates include `since` field from DB
   4. Added talk_time and login_time display per agent

   Depends on: app.js (DF, ReconnectingWS)
*/
'use strict';

window.Supervisor = (() => {

  // ── Formatting ─────────────────────────────────────────────────────────────
  function titleCase(s) {
    return (s || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  function badgeHtml(status, display) {
    return `<span class="badge badge-${status}" style="font-size:11px">${display || titleCase(status)}</span>`;
  }

  function statusDot(status) {
    return `<div class="status-dot ${status}" style="flex-shrink:0"></div>`;
  }

  function fmtDur(sec) {
    if (sec == null || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }

  // ── Agent card builder ──────────────────────────────────────────────────────
  function buildAgentCard(agent, campaigns) {
    const campName = campaigns[agent.active_campaign_id]?.name || '';
    const timerId  = `agttimer-${agent.user_id}`;
    const onCall   = agent.status === 'on_call';

    const monitorBtns = onCall ? `
      <button class="btn btn-secondary btn-sm" style="padding:3px 8px;font-size:11px"
              onclick="Supervisor.monitorCall(${agent.user_id},'listen')" title="Silent monitor">
        <i class="fa-solid fa-headphones"></i>
      </button>
      <button class="btn btn-secondary btn-sm" style="padding:3px 8px;font-size:11px"
              onclick="Supervisor.monitorCall(${agent.user_id},'whisper')" title="Whisper to agent">
        <i class="fa-solid fa-microphone-lines"></i>
      </button>
      <button class="btn btn-secondary btn-sm" style="padding:3px 8px;font-size:11px"
              onclick="Supervisor.monitorCall(${agent.user_id},'barge')" title="Barge into call">
        <i class="fa-solid fa-phone-volume"></i>
      </button>` : '';

    // ── FIX: Show call duration separately from status time ──
    const callTimerId = onCall ? `<div class="agent-card-call-time" id="agtcall-${agent.user_id}" style="font-size:11px;color:var(--color-accent);font-family:var(--font-mono);margin-top:2px">00:00</div>` : '';

    return `
    <div class="agent-card" id="agentcard-${agent.user_id}">
      <div class="flex items-center justify-between">
        <div class="agent-card-name">${agent.full_name || agent.user__username}</div>
        <div class="agent-card-timer" id="${timerId}">00:00</div>
      </div>
      <div class="agent-card-status" style="margin-top:6px">
        ${statusDot(agent.status)}
        ${badgeHtml(agent.status, agent.status_display)}
      </div>
      ${campName ? `<div class="agent-card-campaign">${campName}</div>` : ''}
      ${callTimerId}
      <div class="flex gap-2" style="margin-top:8px">
        ${monitorBtns}
        <button class="btn btn-secondary btn-sm" style="padding:3px 8px;font-size:11px"
                onclick="Supervisor.forceLogout(${agent.user_id})" title="Force logout">
          <i class="fa-solid fa-right-from-bracket"></i>
        </button>
      </div>
    </div>`;
  }

  // ── Campaign control card builder ───────────────────────────────────────────
  function buildCampaignControl(c) {
    const startBtn = c.status !== 'active'
      ? `<button class="btn btn-success btn-sm" onclick="Supervisor.campaignAction(${c.id},'start')">
           <i class="fa-solid fa-play"></i> Start
         </button>`
      : '';
    const pauseBtn = c.status === 'active'
      ? `<button class="btn btn-warning btn-sm" onclick="Supervisor.campaignAction(${c.id},'pause')">
           <i class="fa-solid fa-pause"></i> Pause
         </button>`
      : '';
    const stopBtn = c.status !== 'stopped'
      ? `<button class="btn btn-danger btn-sm" onclick="Supervisor.campaignAction(${c.id},'stop')">
           <i class="fa-solid fa-stop"></i> Stop
         </button>`
      : '';

    return `
    <div class="campaign-control">
      <div class="campaign-control-header">
        <div class="campaign-control-name">${c.name}</div>
        <span class="badge badge-${c.status}" style="font-size:11px">${titleCase(c.status)}</span>
      </div>
      <div class="campaign-control-stats">
        <div class="campaign-stat">
          <span class="campaign-stat-val" id="cs-calls-${c.id}">${c.stat_calls_today || 0}</span>
          <span>Calls</span>
        </div>
        <div class="campaign-stat">
          <span class="campaign-stat-val" id="cs-ans-${c.id}">${c.stat_answered_today || 0}</span>
          <span>Answered</span>
        </div>
        <div class="campaign-stat">
          <span class="campaign-stat-val" id="cs-abn-${c.id}">${parseFloat(c.stat_abandon_rate || 0).toFixed(1)}%</span>
          <span>Abandon</span>
        </div>
        <div class="campaign-stat">
          <span class="campaign-stat-val" id="cs-agt-${c.id}">${c.stat_agents_active || 0}</span>
          <span>Agents</span>
        </div>
      </div>
      <div class="flex gap-2" style="margin-top:10px">
        ${startBtn}${pauseBtn}${stopBtn}
      </div>
    </div>`;
  }

  // ── Actions ──────────────────────────────────────────────────────────────────
  async function campaignAction(id, action) {
    if (action === 'stop' && !confirm('Stop this campaign? Active calls will not be affected.')) return;
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
    const res  = await fetch(`/campaigns/${id}/control/`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf },
      body:    `action=${action}`,
    });
    const data = await res.json();
    DF.toast(data.success ? `Campaign ${data.display}` : (data.error || 'Action failed'),
             data.success ? 'success' : 'error');
  }

  async function forceLogout(agentId) {
    if (!confirm('Force this agent offline?')) return;
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
    const res  = await fetch(`/agents/api/force-logout/${agentId}/`, {
      method: 'POST', headers: { 'X-CSRFToken': csrf },
    });
    const data = await res.json();
    DF.toast(data.success ? 'Agent forced offline' : (data.error || 'Failed'),
             data.success ? 'info' : 'error');
  }

  async function monitorCall(agentId, mode) {
    const labels = { listen: 'Silent Monitor', whisper: 'Whisper to agent', barge: 'Barge into call' };
    const ext = prompt(`${labels[mode] || mode}\nEnter your extension number to receive the monitoring call:`);
    if (!ext || !ext.trim()) return;

    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
    const res  = await fetch('/agents/api/monitor/', {
      method:  'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf },
      body:    `agent_id=${agentId}&mode=${mode}&supervisor_ext=${ext.trim()}`,
    });
    const data = await res.json();
    DF.toast(
      data.success ? `${labels[mode]} started — answer your phone (ext ${ext})` : (data.error || 'Failed'),
      data.success ? 'info' : 'error'
    );
  }

  // ── Public API ──────────────────────────────────────────────────────────────
  return {
    buildAgentCard,
    buildCampaignControl,
    campaignAction,
    forceLogout,
    monitorCall,
    titleCase,
    fmtDur,
  };
})();
