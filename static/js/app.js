/* DialFlow Pro — app.js
   Shared utilities. No framework dependency.
   All real-time state handled by page-specific WS connections.
*/
'use strict';

// ── Timer helpers ────────────────────────────────────────────────────────────
window.DF = {

  // Format seconds → MM:SS or HH:MM:SS
  formatDuration(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  },

  // Elapsed seconds since an ISO timestamp
  elapsed(isoString) {
    if (!isoString) return 0;
    return Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  },

  // Run a callback every second, return cancel fn
  ticker(fn) {
    fn();
    const id = setInterval(fn, 1000);
    return () => clearInterval(id);
  },

  // Safe JSON parse
  parse(str, fallback = null) {
    try { return JSON.parse(str); } catch { return fallback; }
  },

  // Debounce
  debounce(fn, delay = 300) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), delay); };
  },

  // Toast (programmatic)
  toast(msg, type = 'info', duration = 4000) {
    const icons = { success: 'fa-circle-check', error: 'fa-circle-xmark',
                    warning: 'fa-triangle-exclamation', info: 'fa-circle-info' };
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.innerHTML = `<i class="fa-solid ${icons[type] || icons.info}"></i>
      <span>${msg}</span>
      <button onclick="this.parentElement.remove()"><i class="fa-solid fa-xmark"></i></button>`;
    let container = document.querySelector('.messages-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'messages-container';
      document.querySelector('.page-body')?.prepend(container);
    }
    container.appendChild(el);
    if (duration > 0) setTimeout(() => el.remove(), duration);
  },

  // POST with CSRF
  async post(url, data = {}) {
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(data),
    });
    return res.json();
  },

  // GET JSON
  async get(url) {
    const res = await fetch(url);
    return res.json();
  },
};

// ── Robust WebSocket wrapper ─────────────────────────────────────────────────
class ReconnectingWS {
  constructor(url, handlers = {}) {
    this.url      = url;
    this.handlers = handlers;
    this.ws       = null;
    this.delay    = 1000;
    this.maxDelay = 30000;
    this._closed  = false;
    this._hbInterval = null;
    this.connect();
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}${this.url}`);

    this.ws.onopen = () => {
      this.delay = 1000;
      setConnected(true);
      this.handlers.onopen?.();
      // Heartbeat every 25s
      this._hbInterval = setInterval(() => {
        this.send({ action: 'heartbeat' });
      }, 25000);
    };

    this.ws.onmessage = (evt) => {
      const data = DF.parse(evt.data, {});
      this.handlers.onmessage?.(data);
    };

    this.ws.onclose = (evt) => {
      clearInterval(this._hbInterval);
      setConnected(false);
      this.handlers.onclose?.(evt);
      if (!this._closed) {
        setTimeout(() => this.connect(), this.delay);
        this.delay = Math.min(this.delay * 1.5, this.maxDelay);
      }
    };

    this.ws.onerror = () => {
      setConnected(false);
    };
  }

  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  close() {
    this._closed = true;
    clearInterval(this._hbInterval);
    this.ws?.close();
  }
}

window.ReconnectingWS = ReconnectingWS;
