#!/usr/bin/env python3
"""
DialFlow Agent Desktop — integrated softphone + Django backend client.

Requirements:
    pip install customtkinter pillow requests websocket-client
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, simpledialog
import threading
import time
import socket
import hashlib
import random
import re
import queue
import json
from datetime import datetime

import requests as _requests
try:
    import websocket as _websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# THEME
# ─────────────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLORS = {
    "bg_primary":    "#0a0a0f",
    "bg_secondary":  "#111118",
    "bg_card":       "#1a1a2e",
    "bg_card2":      "#16213e",
    "bg_input":      "#0d0d1a",
    "accent_red":    "#ff2d55",
    "accent_green":  "#00d68f",
    "accent_blue":   "#0a84ff",
    "accent_purple": "#bf5af2",
    "accent_orange": "#ff9f0a",
    "accent_cyan":   "#32d74b",
    "text_primary":  "#ffffff",
    "text_secondary":"#8e8ea0",
    "text_dim":      "#48485a",
    "border":        "#2a2a3e",
    "hover":         "#252538",
    "dialpad_bg":    "#1c1c2e",
    "dialpad_hover": "#2a2a42",
}


def blend_with_bg(color, alpha_hex, bg_color=COLORS["bg_card"]):
    try:
        fg_hex = color.lstrip("#")
        bg_hex = bg_color.lstrip("#")
        if len(fg_hex) != 6 or len(bg_hex) != 6:
            return color
        alpha = int(alpha_hex, 16) / 255.0
        fr, fg, fb = int(fg_hex[0:2],16), int(fg_hex[2:4],16), int(fg_hex[4:6],16)
        br, bg_r, bb = int(bg_hex[0:2],16), int(bg_hex[2:4],16), int(bg_hex[4:6],16)
        r = round(fr * alpha + br * (1 - alpha))
        g = round(fg * alpha + bg_r * (1 - alpha))
        b = round(fb * alpha + bb * (1 - alpha))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return color


# ─────────────────────────────────────────────────────────────────────────────
# DJANGO API CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class DialFlowAPI:
    """
    Thin HTTP client wrapping the Django agent REST API.
    Uses session-cookie auth (POST /auth/login/ once, then all requests reuse the session).
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self._session = _requests.Session()
        self._session.headers["X-Requested-With"] = "XMLHttpRequest"
        self.username = ""
        self.logged_in = False

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> dict:
        """Authenticate. Returns {'success': True} or {'error': '...'}."""
        try:
            # Fetch CSRF token first
            r = self._session.get(f"{self.base_url}/auth/login/", timeout=5)
            csrf = r.cookies.get("csrftoken") or self._extract_csrf(r.text)
            if csrf:
                self._session.headers["X-CSRFToken"] = csrf
            r = self._session.post(
                f"{self.base_url}/auth/login/",
                data={"username": username, "password": password},
                timeout=5,
                allow_redirects=False,
            )
            if r.status_code in (302, 200):
                # Follow redirect manually to get the cookie set
                if r.status_code == 302:
                    self._session.get(self.base_url + r.headers.get("Location", "/"), timeout=5)
                # Verify we're authenticated
                check = self._session.get(f"{self.base_url}/agents/api/status/", timeout=5)
                if check.status_code == 200:
                    self.username = username
                    self.logged_in = True
                    # Refresh CSRF after login
                    new_csrf = self._session.cookies.get("csrftoken")
                    if new_csrf:
                        self._session.headers["X-CSRFToken"] = new_csrf
                    return {"success": True, "data": check.json()}
            return {"error": f"Login failed (HTTP {r.status_code})"}
        except _requests.exceptions.ConnectionError:
            return {"error": f"Cannot connect to {self.base_url}"}
        except Exception as e:
            return {"error": str(e)}

    def _extract_csrf(self, html: str) -> str:
        m = re.search(r'csrfmiddlewaretoken.*?value=["\']([^"\']+)', html)
        return m.group(1) if m else ""

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return self._get("/agents/api/status/")

    def set_status(self, status: str, pause_code_id: int = None) -> dict:
        data = {"status": status}
        if pause_code_id:
            data["pause_code_id"] = pause_code_id
        return self._post("/agents/api/set-status/", data)

    def heartbeat(self) -> dict:
        return self._post("/agents/api/heartbeat/", {})

    # ── Lead / calls ─────────────────────────────────────────────────────────

    def get_lead_info(self, lead_id: int = None) -> dict:
        url = "/agents/api/lead-info/"
        if lead_id:
            url += f"?lead_id={lead_id}"
        return self._get(url)

    def get_dispositions(self, campaign_id: int = None) -> dict:
        url = "/agents/api/dispositions/"
        if campaign_id:
            url += f"?campaign_id={campaign_id}"
        return self._get(url)

    def get_call_history(self) -> dict:
        return self._get("/agents/api/call-history/")

    def submit_disposition(self, disposition_id: int, call_log_id: int,
                           notes: str = "", callback_at: str = None) -> dict:
        data = {
            "disposition_id": disposition_id,
            "call_log_id":    call_log_id,
            "notes":          notes,
        }
        if callback_at:
            data["callback_at"] = callback_at
        return self._post("/agents/api/dispose/", data)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get(self, path: str) -> dict:
        if not self.logged_in:
            return {"error": "Not logged in"}
        try:
            r = self._session.get(f"{self.base_url}{path}", timeout=5)
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def _post(self, path: str, data: dict) -> dict:
        if not self.logged_in:
            return {"error": "Not logged in"}
        try:
            # Refresh CSRF before each mutating request
            csrf = self._session.cookies.get("csrftoken")
            if csrf:
                self._session.headers["X-CSRFToken"] = csrf
            r = self._session.post(f"{self.base_url}{path}", data=data, timeout=5)
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET CLIENT (receives push events from Django Channels)
# ─────────────────────────────────────────────────────────────────────────────
class AgentWebSocket:
    """
    Connects to Django Channels AgentConsumer at ws://host/ws/agent/
    Requires the session cookie to be pre-set in a shared requests.Session.
    Forwards received JSON messages to the GUI event queue.
    """

    def __init__(self, base_url: str, session: "_requests.Session", eq: queue.Queue):
        self.base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self._session  = session
        self.eq        = eq
        self._ws: "_websocket.WebSocketApp" = None
        self._thread   = None
        self._running  = False

    def connect(self):
        if not WS_AVAILABLE:
            return
        # Build Cookie header from session
        cookies = "; ".join(f"{k}={v}" for k, v in self._session.cookies.items())
        headers = {"Cookie": cookies}

        self._ws = _websocket.WebSocketApp(
            f"{self.base_url}/ws/agent/",
            header=headers,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._running = True
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 25, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()

    def send(self, payload: dict):
        if self._ws and self._running:
            try:
                self._ws.send(json.dumps(payload))
            except Exception:
                pass

    def disconnect(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _on_open(self, ws):
        self.eq.put(("ws_connected", {}))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            self.eq.put(("ws_event", data))
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.eq.put(("ws_error", str(error)))

    def _on_close(self, ws, code, msg):
        self.eq.put(("ws_disconnected", {}))
        # Auto-reconnect after 5 s if still supposed to be running
        if self._running:
            time.sleep(5)
            self.connect()


# ─────────────────────────────────────────────────────────────────────────────
# SIP CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class SIPClient:
    def __init__(self, event_queue):
        self.eq = event_queue
        self.sock = None
        self.registered = False
        self.server = self.user = self.password = ""
        self.port = 5060
        self.extension = ""
        self.call_id = None
        self.cseq = 1
        self.tag = self._rand_tag()
        self.in_call = False
        self.remote_uri = ""
        self._listen = False
        self._t = None
        self.local_ip = "127.0.0.1"
        self.local_port = 0

    @staticmethod
    def _rand_tag(n=12):
        return ''.join(random.choices('abcdef0123456789', k=n))

    def _md5(self, s):
        return hashlib.md5(s.encode()).hexdigest()

    def _auth_header(self, method, uri, realm, nonce):
        ha1  = self._md5(f"{self.user}:{realm}:{self.password}")
        ha2  = self._md5(f"{method}:{uri}")
        resp = self._md5(f"{ha1}:{nonce}:{ha2}")
        return (f'Authorization: Digest username="{self.user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5\r\n')

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.server, self.port))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def connect(self, server, port, user, password, extension):
        self.server    = server
        self.port      = int(port)
        self.user      = user
        self.password  = password
        self.extension = extension
        self.tag       = self._rand_tag()
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(5)
            self.sock.bind(("", 0))
            self.local_port = self.sock.getsockname()[1]
            self.local_ip   = self._get_local_ip()
            self._send_register()
            self._listen = True
            self._t = threading.Thread(target=self._recv_loop, daemon=True)
            self._t.start()
        except Exception as e:
            self.eq.put(("error", str(e)))

    def _send_register(self, auth_header=""):
        call_id = self._rand_tag(16)
        uri     = f"sip:{self.server}:{self.port}"
        msg = (
            f"REGISTER {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{self._rand_tag()}\r\n"
            f"From: <sip:{self.user}@{self.server}>;tag={self.tag}\r\n"
            f"To: <sip:{self.user}@{self.server}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {self.cseq} REGISTER\r\n"
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
            f"Max-Forwards: 70\r\nExpires: 3600\r\n"
            f"User-Agent: DialFlow-Desktop/3.0\r\n"
            f"{auth_header}Content-Length: 0\r\n\r\n"
        )
        self.cseq += 1
        try:
            self.sock.sendto(msg.encode(), (self.server, self.port))
        except Exception as e:
            self.eq.put(("error", str(e)))

    def _recv_loop(self):
        while self._listen:
            try:
                data, addr = self.sock.recvfrom(4096)
                self._handle(data.decode(errors="ignore"), addr)
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle(self, msg, addr):
        lines = msg.split("\r\n")
        first = lines[0] if lines else ""

        if "401" in first or "407" in first:
            realm = nonce = ""
            for l in lines:
                if "realm=" in l.lower():
                    rm = re.search(r'realm="([^"]+)"', l, re.I)
                    nm = re.search(r'nonce="([^"]+)"', l, re.I)
                    if rm: realm = rm.group(1)
                    if nm: nonce = nm.group(1)
            uri = f"sip:{self.server}:{self.port}"
            self._send_register(self._auth_header("REGISTER", uri, realm, nonce))
            return

        if first.startswith("SIP/2.0 200"):
            if not self.registered:
                self.registered = True
                self.eq.put(("registered", self.extension))
            for l in lines:
                if l.lower().startswith("cseq:"):
                    if "invite" in l.lower():
                        self.eq.put(("call_answered", ""))
                    elif "bye" in l.lower():
                        self.in_call = False
                        self.eq.put(("call_ended", ""))
            return

        if first.startswith("INVITE"):
            call_id = from_hdr = to_hdr = ""
            for l in lines:
                if l.lower().startswith("call-id:"): call_id  = l
                if l.lower().startswith("from:"):    from_hdr = l
                if l.lower().startswith("to:"):      to_hdr   = l
            self.eq.put(("incoming_call", addr[0]))
            self._send_response(addr, "180 Ringing", call_id, from_hdr, to_hdr)
            return

        if first.startswith("BYE"):
            self.in_call = False
            self.eq.put(("call_ended", "Remote hung up"))
        if "100" in first: self.eq.put(("trying",       ""))
        if "180" in first: self.eq.put(("ringing",      ""))
        if "486" in first or "603" in first or "404" in first:
            self.in_call = False
            self.eq.put(("call_failed", first))

    def _send_response(self, addr, status, call_id, from_hdr, to_hdr):
        msg = (
            f"SIP/2.0 {status}\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{self._rand_tag()}\r\n"
            f"{from_hdr}\r\n{to_hdr};tag={self._rand_tag()}\r\n"
            f"{call_id}\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n"
        )
        try:
            self.sock.sendto(msg.encode(), addr)
        except Exception:
            pass

    def make_call(self, number):
        if not self.registered:
            self.eq.put(("error", "Not registered. Configure SIP first."))
            return
        self.call_id    = self._rand_tag(16)
        self.remote_uri = f"sip:{number}@{self.server}"
        sdp = self._sdp()
        msg = (
            f"INVITE {self.remote_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{self._rand_tag()}\r\n"
            f"From: <sip:{self.user}@{self.server}>;tag={self.tag}\r\n"
            f"To: <{self.remote_uri}>\r\n"
            f"Call-ID: {self.call_id}\r\nCSeq: {self.cseq} INVITE\r\n"
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
            f"Max-Forwards: 70\r\nContent-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
        )
        self.cseq += 1
        self.in_call = True
        try:
            self.sock.sendto(msg.encode(), (self.server, self.port))
            self.eq.put(("calling", number))
        except Exception as e:
            self.eq.put(("error", str(e)))

    def _sdp(self):
        return (
            f"v=0\r\no=- {int(time.time())} {int(time.time())} IN IP4 {self.local_ip}\r\n"
            f"s=DialFlow Call\r\nc=IN IP4 {self.local_ip}\r\nt=0 0\r\n"
            f"m=audio 8000 RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\na=sendrecv\r\n"
        )

    def hangup(self):
        if self.in_call and self.call_id:
            msg = (
                f"BYE {self.remote_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{self._rand_tag()}\r\n"
                f"From: <sip:{self.user}@{self.server}>;tag={self.tag}\r\n"
                f"To: <{self.remote_uri}>\r\n"
                f"Call-ID: {self.call_id}\r\nCSeq: {self.cseq} BYE\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            self.cseq += 1
            try:
                self.sock.sendto(msg.encode(), (self.server, self.port))
            except Exception:
                pass
            self.in_call = False
            self.eq.put(("call_ended", "Hung up"))

    def disconnect(self):
        self._listen = False
        self.registered = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM WIDGETS
# ─────────────────────────────────────────────────────────────────────────────
class StatCard(ctk.CTkFrame):
    def __init__(self, parent, title, value, subtitle="", color=None, **kwargs):
        super().__init__(parent, fg_color=COLORS["bg_card"], corner_radius=12,
                         border_width=1, border_color=COLORS["border"], **kwargs)
        self.color = color or COLORS["accent_blue"]
        ctk.CTkFrame(self, height=3, fg_color=self.color, corner_radius=2).pack(fill="x")
        ctk.CTkLabel(self, text=title, font=("SF Pro Display", 10),
                     text_color=COLORS["text_secondary"]).pack(pady=(8, 0))
        self.value_label = ctk.CTkLabel(self, text=value,
                                         font=("SF Pro Display", 24, "bold"),
                                         text_color=COLORS["text_primary"])
        self.value_label.pack()
        if subtitle:
            self.sub_label = ctk.CTkLabel(self, text=subtitle,
                                           font=("SF Pro Display", 9),
                                           text_color=self.color)
            self.sub_label.pack(pady=(0, 8))
        else:
            ctk.CTkLabel(self, text="", font=("SF Pro Display", 9)).pack(pady=(0, 8))

    def update_value(self, value, subtitle=None):
        self.value_label.configure(text=value)
        if subtitle and hasattr(self, "sub_label"):
            self.sub_label.configure(text=subtitle)


class DialButton(ctk.CTkFrame):
    def __init__(self, parent, number, letters="", command=None, **kwargs):
        super().__init__(parent, fg_color=COLORS["dialpad_bg"], corner_radius=14,
                         cursor="hand2", border_width=1, border_color=COLORS["border"], **kwargs)
        self.command = command
        self.num_lbl = ctk.CTkLabel(self, text=number, font=("SF Pro Display", 20, "bold"),
                                     text_color=COLORS["text_primary"])
        self.num_lbl.pack(pady=(10, 0) if letters else (14, 14))
        if letters:
            self.let_lbl = ctk.CTkLabel(self, text=letters, font=("SF Pro Display", 8),
                                         text_color=COLORS["text_secondary"])
            self.let_lbl.pack(pady=(0, 8))
        for w in [self, self.num_lbl] + ([self.let_lbl] if letters else []):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>",    lambda e: self.configure(fg_color=COLORS["dialpad_hover"]))
            w.bind("<Leave>",    lambda e: self.configure(fg_color=COLORS["dialpad_bg"]))

    def _on_click(self, e):
        self.configure(fg_color=COLORS["accent_blue"])
        self.after(120, lambda: self.configure(fg_color=COLORS["dialpad_hover"]))
        if self.command:
            self.command(self.num_lbl.cget("text"))


class StatusBadge(ctk.CTkFrame):
    def __init__(self, parent, text, color, **kwargs):
        super().__init__(parent, fg_color=blend_with_bg(color, "22"), corner_radius=20, **kwargs)
        self.dot   = ctk.CTkLabel(self, text="●", font=("SF Pro Display", 10), text_color=color)
        self.dot.pack(side="left", padx=(8, 2), pady=4)
        self.label = ctk.CTkLabel(self, text=text, font=("SF Pro Display", 10, "bold"), text_color=color)
        self.label.pack(side="left", padx=(0, 10), pady=4)

    def update(self, text, color):
        self.configure(fg_color=blend_with_bg(color, "22"))
        self.dot.configure(text_color=color)
        self.label.configure(text=text, text_color=color)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class AutoDialerApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("DialFlow Agent Desktop")
        self.root.geometry("1500x860")
        self.root.configure(fg_color=COLORS["bg_primary"])
        self.root.resizable(True, True)
        self.root.minsize(1200, 700)

        # ── Core state ────────────────────────────────────────────────────────
        self.eq           = queue.Queue()
        self.sip          = SIPClient(self.eq)
        self.sip_config   = {}
        self.call_active  = False
        self.call_start   = None
        self.muted        = False
        self.on_hold      = False
        self.status       = "offline"
        self.status_since = time.time()
        self.login_since  = time.time()
        self.total_calls  = 0
        self.answered     = 0
        self.talk_secs    = 0
        self.current_num  = ""
        self.call_history = []
        self.notes_text   = ""
        self.active_lead  = {}
        self.dispositions = []
        self.active_campaign_id = None
        self.pending_wrapup_call_log_id = None

        # ── Django API ────────────────────────────────────────────────────────
        self.server_url   = "http://127.0.0.1:8000"
        self.api          = DialFlowAPI(self.server_url)
        self.ws_client    = None

        # ── Vars ──────────────────────────────────────────────────────────────
        self.dial_var      = ctk.StringVar(value="")
        self.auto_answer   = ctk.BooleanVar(value=False)
        self.sip_status_v  = ctk.StringVar(value="Not Connected")
        self.api_status_v  = ctk.StringVar(value="Not logged in")
        self.active_tab    = ctk.StringVar(value="CALL INFO")

        self._build()
        self._poll()
        self._tick()

        # Show login dialog on startup
        self.root.after(300, self._login_dialog)

    # ─────────────────────────────────────────────────────────────────────────
    # LOGIN DIALOG
    # ─────────────────────────────────────────────────────────────────────────
    def _login_dialog(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("DialFlow Login")
        dlg.geometry("420x520")
        dlg.configure(fg_color=COLORS["bg_secondary"])
        dlg.grab_set()
        dlg.resizable(False, False)

        # Header
        hdr = ctk.CTkFrame(dlg, fg_color=COLORS["bg_card"], corner_radius=0, height=80)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="📞  DialFlow Agent Login",
                     font=("SF Pro Display", 16, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.4, anchor="center")
        ctk.CTkLabel(hdr, text="Sign in to connect to the dialer",
                     font=("SF Pro Display", 10),
                     text_color=COLORS["text_secondary"]).place(relx=.5, rely=.75, anchor="center")

        form = ctk.CTkFrame(dlg, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=28, pady=20)

        # Server URL
        ctk.CTkLabel(form, text="Server URL", font=("SF Pro Display", 10, "bold"),
                     text_color=COLORS["text_secondary"], anchor="w").pack(fill="x", pady=(8, 3))
        srv_entry = ctk.CTkEntry(form, placeholder_text="http://127.0.0.1:8000",
                                  fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                                  text_color=COLORS["text_primary"],
                                  font=("SF Pro Display", 12), height=42, corner_radius=10)
        srv_entry.pack(fill="x")
        srv_entry.insert(0, self.server_url)

        # Username
        ctk.CTkLabel(form, text="Username", font=("SF Pro Display", 10, "bold"),
                     text_color=COLORS["text_secondary"], anchor="w").pack(fill="x", pady=(14, 3))
        user_entry = ctk.CTkEntry(form, placeholder_text="agent username",
                                   fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                                   text_color=COLORS["text_primary"],
                                   font=("SF Pro Display", 12), height=42, corner_radius=10)
        user_entry.pack(fill="x")

        # Password
        ctk.CTkLabel(form, text="Password", font=("SF Pro Display", 10, "bold"),
                     text_color=COLORS["text_secondary"], anchor="w").pack(fill="x", pady=(14, 3))
        pass_entry = ctk.CTkEntry(form, placeholder_text="password", show="*",
                                   fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                                   text_color=COLORS["text_primary"],
                                   font=("SF Pro Display", 12), height=42, corner_radius=10)
        pass_entry.pack(fill="x")

        err_label = ctk.CTkLabel(form, text="", font=("SF Pro Display", 10),
                                  text_color=COLORS["accent_red"])
        err_label.pack(pady=(8, 0))

        def _do_login():
            url  = srv_entry.get().strip().rstrip("/")
            user = user_entry.get().strip()
            pwd  = pass_entry.get()
            if not url or not user or not pwd:
                err_label.configure(text="All fields required.")
                return
            self.server_url = url
            self.api = DialFlowAPI(url)
            err_label.configure(text="Connecting…", text_color=COLORS["accent_orange"])
            dlg.update()

            def _bg():
                result = self.api.login(user, pwd)
                self.root.after(0, lambda: _after(result))

            def _after(result):
                if result.get("success"):
                    self.api_status_v.set(f"Logged in as {user}  ·  {url}")
                    if hasattr(self, "api_badge"):
                        self.api_badge.update(f"Logged in · {user}", COLORS["accent_green"])
                    dlg.destroy()
                    self._on_api_login(result.get("data", {}))
                else:
                    err_label.configure(text=result.get("error", "Login failed"),
                                        text_color=COLORS["accent_red"])

            threading.Thread(target=_bg, daemon=True).start()

        def _skip():
            dlg.destroy()

        ctk.CTkButton(form, text="🔐  Login",
                      font=("SF Pro Display", 12, "bold"),
                      fg_color=COLORS["accent_blue"], hover_color="#0070d8",
                      corner_radius=12, height=46,
                      command=_do_login).pack(fill="x", pady=(14, 4))
        ctk.CTkButton(form, text="Skip (SIP only)",
                      font=("SF Pro Display", 11),
                      fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                      corner_radius=12, height=40,
                      command=_skip).pack(fill="x")

        user_entry.focus()
        dlg.bind("<Return>", lambda e: _do_login())

    def _on_api_login(self, status_data: dict):
        """Called after successful login — sync initial state from server."""
        if not status_data:
            return
        server_status = status_data.get("status", "offline")
        self.status = server_status
        self.status_since = time.time()
        self.active_campaign_id = status_data.get("campaign_id")

        # Update UI from server status
        color_map = {
            "ready":    COLORS["accent_green"],
            "break":    COLORS["accent_orange"],
            "training": COLORS["accent_blue"],
            "on_call":  COLORS["accent_cyan"],
            "wrapup":   COLORS["accent_purple"],
            "offline":  COLORS["text_secondary"],
        }
        color = color_map.get(server_status, COLORS["text_secondary"])
        self._update_avail_btn(server_status, color)

        # Pull today's stats
        threading.Thread(target=self._fetch_initial_data, daemon=True).start()

        # Start WebSocket
        if WS_AVAILABLE:
            self.ws_client = AgentWebSocket(self.server_url, self.api._session, self.eq)
            threading.Thread(target=self.ws_client.connect, daemon=True).start()

        # Start heartbeat loop
        self.root.after(20_000, self._send_heartbeat)

    def _fetch_initial_data(self):
        """Background: pull call history + dispositions and push to queue."""
        hist = self.api.get_call_history()
        self.eq.put(("api_history", hist))
        disps = self.api.get_dispositions(self.active_campaign_id)
        self.eq.put(("api_dispositions", disps))

    def _send_heartbeat(self):
        if self.api.logged_in:
            threading.Thread(target=self.api.heartbeat, daemon=True).start()
        self.root.after(20_000, self._send_heartbeat)

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD UI
    # ─────────────────────────────────────────────────────────────────────────
    def _build(self):
        self._topbar()
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._left_panel(body)
        self._center_panel(body)
        self._right_panel(body)

    # ── TOP BAR ──────────────────────────────────────────────────────────────
    def _topbar(self):
        bar = ctk.CTkFrame(self.root, height=64, fg_color=COLORS["bg_secondary"], corner_radius=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Left — brand
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=20, pady=10)
        logo = ctk.CTkFrame(left, fg_color=COLORS["accent_red"], corner_radius=12, width=42, height=42)
        logo.pack(side="left", padx=(0, 14))
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="📞", font=("SF Pro Display", 20)).place(relx=.5, rely=.5, anchor="center")
        info = ctk.CTkFrame(left, fg_color="transparent")
        info.pack(side="left")
        ctk.CTkLabel(info, text="DialFlow Agent Desktop",
                     font=("SF Pro Display", 15, "bold"),
                     text_color=COLORS["text_primary"]).pack(anchor="w")
        ctk.CTkLabel(info, textvariable=self.api_status_v,
                     font=("SF Pro Display", 9),
                     text_color=COLORS["text_secondary"]).pack(anchor="w")

        # Center — nav
        center = ctk.CTkFrame(bar, fg_color="transparent")
        center.pack(side="left", expand=True)
        for txt, cmd in [
            ("📋  Call History", self._show_history),
            ("⚙   SIP Settings", self._sip_dialog),
            ("🔐  Switch Account", self._login_dialog),
        ]:
            ctk.CTkButton(center, text=txt, font=("SF Pro Display", 11),
                          fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                          corner_radius=10, height=36, width=150, command=cmd
                          ).pack(side="left", padx=6)

        # Right — badges
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=20, pady=10)

        self.api_badge = StatusBadge(right, "Not logged in", COLORS["accent_red"])
        self.api_badge.pack(side="left", padx=(0, 8))

        self.sip_badge = StatusBadge(right, "SIP: Disconnected", COLORS["text_dim"])
        self.sip_badge.pack(side="left", padx=(0, 14))

        self.avail_btn = ctk.CTkButton(right, text="⏻ OFFLINE",
                                        font=("SF Pro Display", 11, "bold"),
                                        fg_color=COLORS["text_dim"],
                                        hover_color=COLORS["hover"],
                                        text_color="#fff",
                                        corner_radius=20, height=36, width=140,
                                        command=self._toggle_avail)
        self.avail_btn.pack(side="left")

    # ── LEFT PANEL — SOFTPHONE ────────────────────────────────────────────────
    def _left_panel(self, parent):
        lp = ctk.CTkFrame(parent, width=300, fg_color=COLORS["bg_secondary"], corner_radius=0)
        lp.grid(row=0, column=0, sticky="nsew")
        lp.grid_propagate(False)

        hdr = ctk.CTkFrame(lp, fg_color=COLORS["bg_card"], corner_radius=0, height=70)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="DIALFLOW PHONE",
                     font=("SF Pro Display", 13, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.35, anchor="center")
        ctk.CTkLabel(hdr, text="SIP Softphone v3.0",
                     font=("SF Pro Display", 9),
                     text_color=COLORS["text_secondary"]).place(relx=.5, rely=.72, anchor="center")

        self.sip_strip = ctk.CTkFrame(lp, fg_color=COLORS["bg_card2"], corner_radius=0,
                                       height=36, cursor="hand2")
        self.sip_strip.pack(fill="x")
        self.sip_strip.pack_propagate(False)
        self.sip_strip_lbl = ctk.CTkLabel(self.sip_strip, textvariable=self.sip_status_v,
                                            font=("SF Pro Display", 10),
                                            text_color=COLORS["accent_orange"])
        self.sip_strip_lbl.place(relx=.5, rely=.5, anchor="center")
        self.sip_strip.bind("<Button-1>", lambda e: self._sip_dialog())
        self.sip_strip_lbl.bind("<Button-1>", lambda e: self._sip_dialog())

        aa = ctk.CTkFrame(lp, fg_color=COLORS["bg_card"], corner_radius=12, height=44)
        aa.pack(fill="x", padx=12, pady=10)
        aa.pack_propagate(False)
        ctk.CTkLabel(aa, text="⚡  Auto Answer", font=("SF Pro Display", 11),
                     text_color=COLORS["text_primary"]).pack(side="left", padx=14)
        ctk.CTkSwitch(aa, variable=self.auto_answer, text="",
                      progress_color=COLORS["accent_green"],
                      button_color=COLORS["text_primary"],
                      width=46, height=24).pack(side="right", padx=14)

        screen = ctk.CTkFrame(lp, fg_color=COLORS["bg_input"], corner_radius=16, height=90,
                               border_width=1, border_color=COLORS["border"])
        screen.pack(fill="x", padx=12, pady=(0, 8))
        screen.pack_propagate(False)
        self.phone_status_lbl = ctk.CTkLabel(screen, text="READY",
                                              font=("SF Pro Display", 14, "bold"),
                                              text_color=COLORS["accent_green"])
        self.phone_status_lbl.place(relx=.5, rely=.3, anchor="center")
        self.phone_ext_lbl = ctk.CTkLabel(screen, text="EXTENSION: ─",
                                           font=("SF Pro Display", 9),
                                           text_color=COLORS["text_secondary"])
        self.phone_ext_lbl.place(relx=.5, rely=.65, anchor="center")

        dial_frame = ctk.CTkFrame(lp, fg_color=COLORS["bg_card"], corner_radius=14, height=52,
                                   border_width=1, border_color=COLORS["border"])
        dial_frame.pack(fill="x", padx=12, pady=(0, 8))
        dial_frame.pack_propagate(False)
        self.dial_entry = ctk.CTkEntry(dial_frame, textvariable=self.dial_var,
                                        font=("SF Pro Display", 22, "bold"),
                                        fg_color="transparent", border_width=0,
                                        justify="center",
                                        text_color=COLORS["accent_cyan"],
                                        placeholder_text="Enter number…",
                                        placeholder_text_color=COLORS["text_dim"])
        self.dial_entry.pack(fill="both", expand=True, padx=10)

        pad = ctk.CTkFrame(lp, fg_color="transparent")
        pad.pack(padx=12, pady=(0, 8))
        for i, (num, sub) in enumerate([
            ("1",""), ("2","ABC"), ("3","DEF"),
            ("4","GHI"), ("5","JKL"), ("6","MNO"),
            ("7","PQRS"), ("8","TUV"), ("9","WXYZ"),
            ("*",""), ("0","+"), ("#",""),
        ]):
            r, c = divmod(i, 3)
            DialButton(pad, num, sub, command=lambda d=None, n=num: self.dial_var.set(self.dial_var.get() + n),
                       width=78, height=58).grid(row=r, column=c, padx=3, pady=3)

        self.call_btn = ctk.CTkButton(lp, text="📞   CALL",
                                       font=("SF Pro Display", 14, "bold"),
                                       fg_color=COLORS["accent_green"], hover_color="#00b87a",
                                       text_color="#000", corner_radius=14, height=50,
                                       command=self._call_toggle)
        self.call_btn.pack(fill="x", padx=12, pady=(0, 8))

        ctrl = ctk.CTkFrame(lp, fg_color="transparent")
        ctrl.pack(fill="x", padx=12, pady=(0, 12))
        ctrl.grid_columnconfigure((0,1,2,3), weight=1)

        self.mute_btn = ctk.CTkButton(ctrl, text="🎤\nMUTE",
                                       font=("SF Pro Display", 9, "bold"),
                                       fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                                       corner_radius=10, height=44,
                                       command=self._toggle_mute)
        self.mute_btn.grid(row=0, column=0, padx=2, sticky="ew")

        self.hold_btn = ctk.CTkButton(ctrl, text="⏸\nHOLD",
                                       font=("SF Pro Display", 9, "bold"),
                                       fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                                       corner_radius=10, height=44,
                                       command=self._toggle_hold)
        self.hold_btn.grid(row=0, column=1, padx=2, sticky="ew")

        ctk.CTkButton(ctrl, text="↗\nXFER",
                      font=("SF Pro Display", 9, "bold"),
                      fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                      corner_radius=10, height=44,
                      command=self._transfer
                      ).grid(row=0, column=2, padx=2, sticky="ew")

        ctk.CTkButton(ctrl, text="✖\nCLEAR",
                      font=("SF Pro Display", 9, "bold"),
                      fg_color=blend_with_bg(COLORS["accent_red"], "33"),
                      hover_color=blend_with_bg(COLORS["accent_red"], "55"),
                      text_color=COLORS["accent_red"],
                      corner_radius=10, height=44,
                      command=lambda: self.dial_var.set("")
                      ).grid(row=0, column=3, padx=2, sticky="ew")

    # ── CENTER PANEL ──────────────────────────────────────────────────────────
    def _center_panel(self, parent):
        cp = ctk.CTkFrame(parent, fg_color=COLORS["bg_primary"], corner_radius=0)
        cp.grid(row=0, column=1, sticky="nsew")
        cp.grid_rowconfigure(1, weight=1)
        cp.grid_columnconfigure(0, weight=1)

        call_card = ctk.CTkFrame(cp, fg_color=COLORS["bg_secondary"], corner_radius=20,
                                  border_width=1, border_color=COLORS["border"])
        call_card.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))

        ch = ctk.CTkFrame(call_card, fg_color="transparent")
        ch.pack(fill="x", padx=20, pady=(16, 0))
        ctk.CTkLabel(ch, text="📞  CURRENT CALL",
                     font=("SF Pro Display", 12, "bold"),
                     text_color=COLORS["text_secondary"]).pack(side="left")
        self.call_status_badge = StatusBadge(ch, "NO ACTIVE CALL", COLORS["text_dim"])
        self.call_status_badge.pack(side="right")

        self.call_display = ctk.CTkFrame(call_card, fg_color=COLORS["bg_card"],
                                          corner_radius=16, height=180,
                                          border_width=1, border_color=COLORS["border"])
        self.call_display.pack(fill="x", padx=20, pady=16)
        self.call_display.pack_propagate(False)
        self._show_idle()

        tab_frame = ctk.CTkFrame(cp, fg_color=COLORS["bg_secondary"], corner_radius=16,
                                  border_width=1, border_color=COLORS["border"])
        tab_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        tab_frame.grid_rowconfigure(1, weight=1)
        tab_frame.grid_columnconfigure(0, weight=1)

        tb = ctk.CTkFrame(tab_frame, fg_color=COLORS["bg_card"], corner_radius=12)
        tb.grid(row=0, column=0, sticky="ew", padx=16, pady=12)

        self.tab_btns = {}
        for name in ["CALL INFO", "LEAD", "DISPOSITION", "SCRIPTS", "NOTES"]:
            b = ctk.CTkButton(tb, text=name,
                               font=("SF Pro Display", 10, "bold"),
                               fg_color=COLORS["accent_red"] if name == "CALL INFO" else "transparent",
                               hover_color=COLORS["hover"],
                               text_color=COLORS["text_primary"] if name == "CALL INFO" else COLORS["text_secondary"],
                               corner_radius=10, height=34, width=110,
                               command=lambda n=name: self._switch_tab(n))
            b.pack(side="left", padx=4, pady=4)
            self.tab_btns[name] = b

        self.tab_area = ctk.CTkFrame(tab_frame, fg_color="transparent")
        self.tab_area.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.tab_area.grid_columnconfigure(0, weight=1)
        self.tab_area.grid_rowconfigure(0, weight=1)
        self._tab_call_info()

    # ── RIGHT PANEL ───────────────────────────────────────────────────────────
    def _right_panel(self, parent):
        rp = ctk.CTkScrollableFrame(parent, width=280, fg_color=COLORS["bg_secondary"],
                                     corner_radius=0,
                                     scrollbar_button_color=COLORS["border"])
        rp.grid(row=0, column=2, sticky="nsew")

        def section(title, icon):
            f = ctk.CTkFrame(rp, fg_color=COLORS["bg_card"], corner_radius=16,
                             border_width=1, border_color=COLORS["border"])
            f.pack(fill="x", padx=12, pady=8)
            hdr = ctk.CTkFrame(f, fg_color="transparent")
            hdr.pack(fill="x", padx=14, pady=(12, 8))
            ctk.CTkLabel(hdr, text=f"{icon}  {title}",
                         font=("SF Pro Display", 11, "bold"),
                         text_color=COLORS["text_primary"]).pack(side="left")
            return f

        # Availability
        av = section("AVAILABILITY", "📶")
        statuses = [
            ("✅ Available", "ready",    COLORS["accent_green"]),
            ("☕ Break",     "break",    COLORS["accent_orange"]),
            ("🍽 Lunch",    "break",    "#a29bfe"),
            ("🎓 Training", "training", COLORS["accent_blue"]),
            ("🤝 Meeting",  "break",    "#fd79a8"),
            ("⏻ Offline",  "offline",  COLORS["text_secondary"]),
        ]
        grid = ctk.CTkFrame(av, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(0, 12))
        grid.grid_columnconfigure((0, 1), weight=1)
        self.avail_btns = {}
        for i, (label, key, color) in enumerate(statuses):
            r, c = divmod(i, 2)
            b = ctk.CTkButton(grid, text=label,
                               font=("SF Pro Display", 9, "bold"),
                               fg_color=blend_with_bg(color, "33"),
                               hover_color=blend_with_bg(color, "55"),
                               text_color=color, corner_radius=10, height=34,
                               border_width=1, border_color=blend_with_bg(color, "44"),
                               command=lambda k=key, cl=color: self._set_status(k, cl))
            b.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
            self.avail_btns[key] = b

        # Session Timers
        st = section("SESSION TIMERS", "⏱")
        tf = ctk.CTkFrame(st, fg_color="transparent")
        tf.pack(fill="x", padx=14, pady=(0, 14))
        tf.grid_columnconfigure((0, 1), weight=1)
        for col, (lbl, attr, color) in enumerate([
            ("STATUS TIME", "status_timer", COLORS["accent_red"]),
            ("LOGIN TIME",  "login_timer",  COLORS["accent_blue"]),
        ]):
            ctk.CTkLabel(tf, text=lbl, font=("SF Pro Display", 8),
                         text_color=COLORS["text_secondary"]).grid(row=0, column=col, sticky="w", padx=4)
            t = ctk.CTkLabel(tf, text="00:00", font=("SF Pro Display", 22, "bold"), text_color=color)
            t.grid(row=1, column=col, sticky="w", padx=4)
            setattr(self, attr, t)
        self.status_name_lbl = ctk.CTkLabel(tf, text="Offline",
                                             font=("SF Pro Display", 9),
                                             text_color=COLORS["text_secondary"])
        self.status_name_lbl.grid(row=2, column=0, sticky="w", padx=4, pady=(2, 0))

        # Performance
        pf = section("TODAY'S PERFORMANCE", "📈")
        pg = ctk.CTkFrame(pf, fg_color="transparent")
        pg.pack(fill="x", padx=12, pady=(0, 12))
        pg.grid_columnconfigure((0, 1), weight=1)
        self.perf_cards = {}
        for i, (label, val, color) in enumerate([
            ("TOTAL CALLS",  "0",  COLORS["accent_blue"]),
            ("ANSWERED",     "0",  COLORS["accent_green"]),
            ("TALK TIME",    "0s", COLORS["accent_purple"]),
            ("CONTACT RATE", "0%", COLORS["accent_orange"]),
        ]):
            r, c = divmod(i, 2)
            card = StatCard(pg, label, val, color=color)
            card.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            self.perf_cards[label] = card

        # SIP Connection
        sc = section("SIP CONNECTION", "🔌")
        sf = ctk.CTkFrame(sc, fg_color="transparent")
        sf.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(sf, textvariable=self.sip_status_v,
                     font=("SF Pro Display", 9),
                     text_color=COLORS["text_secondary"], wraplength=220).pack(pady=(0, 8))
        ctk.CTkButton(sf, text="🔗  Configure & Connect",
                      font=("SF Pro Display", 10, "bold"),
                      fg_color=COLORS["accent_blue"], hover_color="#0070d8",
                      corner_radius=10, height=38,
                      command=self._sip_dialog).pack(fill="x", pady=2)
        ctk.CTkButton(sf, text="🔌  Disconnect SIP",
                      font=("SF Pro Display", 10),
                      fg_color=blend_with_bg(COLORS["accent_red"], "33"),
                      hover_color=blend_with_bg(COLORS["accent_red"], "55"),
                      text_color=COLORS["accent_red"],
                      corner_radius=10, height=38,
                      border_width=1, border_color=blend_with_bg(COLORS["accent_red"], "55"),
                      command=self._sip_disconnect).pack(fill="x", pady=2)

        # Callbacks
        cb = section("CALLBACKS", "📅")
        ctk.CTkLabel(cb, text="No pending callbacks.",
                     font=("SF Pro Display", 10),
                     text_color=COLORS["text_secondary"]).pack(pady=(0, 14))

    # ─────────────────────────────────────────────────────────────────────────
    # CALL DISPLAY STATES
    # ─────────────────────────────────────────────────────────────────────────
    def _clear_call_display(self):
        for w in self.call_display.winfo_children():
            w.destroy()

    def _show_idle(self):
        self._clear_call_display()
        ctk.CTkLabel(self.call_display, text="🎧", font=("SF Pro Display", 40),
                     text_color=COLORS["text_dim"]).place(relx=.5, rely=.35, anchor="center")
        ctk.CTkLabel(self.call_display, text="Waiting for a call…",
                     font=("SF Pro Display", 14),
                     text_color=COLORS["text_secondary"]).place(relx=.5, rely=.60, anchor="center")
        ctk.CTkLabel(self.call_display, text="Set status to Available to receive calls.",
                     font=("SF Pro Display", 10),
                     text_color=COLORS["text_dim"]).place(relx=.5, rely=.78, anchor="center")

    def _show_calling(self, number):
        self._clear_call_display()
        ctk.CTkLabel(self.call_display, text="📲", font=("SF Pro Display", 36),
                     text_color=COLORS["accent_orange"]).place(relx=.5, rely=.2, anchor="center")
        ctk.CTkLabel(self.call_display, text=number, font=("SF Pro Display", 26, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.45, anchor="center")
        self.ring_lbl = ctk.CTkLabel(self.call_display, text="Dialing…",
                                      font=("SF Pro Display", 12),
                                      text_color=COLORS["accent_orange"])
        self.ring_lbl.place(relx=.5, rely=.63, anchor="center")
        ctk.CTkButton(self.call_display, text="📵  CANCEL",
                      font=("SF Pro Display", 11, "bold"),
                      fg_color=COLORS["accent_red"], hover_color="#cc0033",
                      corner_radius=20, height=38, width=160,
                      command=self._hangup).place(relx=.5, rely=.83, anchor="center")

    def _show_active(self, number):
        self._clear_call_display()
        ctk.CTkLabel(self.call_display, text="●", font=("SF Pro Display", 16),
                     text_color=COLORS["accent_green"]).place(relx=.5, rely=.1, anchor="center")
        ctk.CTkLabel(self.call_display, text=number, font=("SF Pro Display", 28, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.3, anchor="center")
        self.call_dur_lbl = ctk.CTkLabel(self.call_display, text="00:00:00",
                                          font=("SF Pro Display", 36, "bold"),
                                          text_color=COLORS["accent_green"])
        self.call_dur_lbl.place(relx=.5, rely=.55, anchor="center")
        ctrl_row = ctk.CTkFrame(self.call_display, fg_color="transparent")
        ctrl_row.place(relx=.5, rely=.85, anchor="center")
        ctk.CTkButton(ctrl_row, text="📵", font=("SF Pro Display", 18),
                      fg_color=COLORS["accent_red"], hover_color="#cc0033",
                      corner_radius=30, height=44, width=44,
                      command=self._hangup).pack(side="left", padx=8)

    def _show_wrapup(self, number):
        self._clear_call_display()
        ctk.CTkLabel(self.call_display, text="📋", font=("SF Pro Display", 36),
                     text_color=COLORS["accent_purple"]).place(relx=.5, rely=.2, anchor="center")
        ctk.CTkLabel(self.call_display, text=f"Wrap-up: {number}",
                     font=("SF Pro Display", 18, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.45, anchor="center")
        ctk.CTkLabel(self.call_display, text="Submit a disposition to return to Ready",
                     font=("SF Pro Display", 10),
                     text_color=COLORS["text_secondary"]).place(relx=.5, rely=.63, anchor="center")
        ctk.CTkButton(self.call_display, text="📋  Dispose",
                      font=("SF Pro Display", 11, "bold"),
                      fg_color=COLORS["accent_purple"], hover_color="#a040d0",
                      corner_radius=20, height=38, width=160,
                      command=lambda: self._switch_tab("DISPOSITION")
                      ).place(relx=.5, rely=.83, anchor="center")

    # ─────────────────────────────────────────────────────────────────────────
    # TABS
    # ─────────────────────────────────────────────────────────────────────────
    def _clear_tab(self):
        for w in self.tab_area.winfo_children():
            w.destroy()

    def _switch_tab(self, name):
        self.active_tab.set(name)
        for n, b in self.tab_btns.items():
            if n == name:
                b.configure(fg_color=COLORS["accent_red"], text_color=COLORS["text_primary"])
            else:
                b.configure(fg_color="transparent", text_color=COLORS["text_secondary"])
        self._clear_tab()
        {
            "CALL INFO":   self._tab_call_info,
            "LEAD":        self._tab_lead,
            "DISPOSITION": self._tab_disposition,
            "SCRIPTS":     self._tab_scripts,
            "NOTES":       self._tab_notes,
        }[name]()

    def _tab_call_info(self):
        f = ctk.CTkFrame(self.tab_area, fg_color="transparent")
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_columnconfigure((0,1,2,3), weight=1)

        ctk.CTkLabel(f, text="EXTENSION & STATS",
                     font=("SF Pro Display", 11, "bold"),
                     text_color=COLORS["text_secondary"]).grid(
                     row=0, column=0, columnspan=4, sticky="w", pady=(8, 12))

        ext = self.sip_config.get("extension", "─")
        self.info_cards = {}
        for i, (title, val, color) in enumerate([
            ("EXTENSION",   ext,                     COLORS["accent_blue"]),
            ("CAMPAIGN",    "None",                  COLORS["accent_purple"]),
            ("CALLS TODAY", str(self.total_calls),   COLORS["accent_orange"]),
            ("ANSWERED",    str(self.answered),       COLORS["accent_green"]),
        ]):
            card = StatCard(f, title, val, color=color, height=100)
            card.grid(row=1, column=i, padx=6, pady=4, sticky="nsew")
            self.info_cards[title] = card

        detail = ctk.CTkFrame(f, fg_color=COLORS["bg_card"], corner_radius=12,
                               border_width=1, border_color=COLORS["border"])
        detail.grid(row=2, column=0, columnspan=4, sticky="ew", padx=0, pady=(12, 0))

        dh = ctk.CTkFrame(detail, fg_color="transparent")
        dh.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(dh, text="SIP CONNECTION DETAILS",
                     font=("SF Pro Display", 10, "bold"),
                     text_color=COLORS["text_secondary"]).pack(side="left")
        self.conn_indicator = ctk.CTkLabel(dh, text="● DISCONNECTED",
                                            font=("SF Pro Display", 10, "bold"),
                                            text_color=COLORS["accent_red"])
        self.conn_indicator.pack(side="right")

        dg = ctk.CTkFrame(detail, fg_color="transparent")
        dg.pack(fill="x", padx=16, pady=(0, 12))
        self.detail_labels = {}
        for i, (label, val) in enumerate([
            ("Server",    self.sip_config.get("server",    "─")),
            ("Port",      self.sip_config.get("port",      "─")),
            ("Username",  self.sip_config.get("user",      "─")),
            ("Extension", self.sip_config.get("extension", "─")),
        ]):
            r, c = divmod(i, 2)
            lf = ctk.CTkFrame(dg, fg_color="transparent")
            lf.grid(row=r, column=c, padx=20, pady=4, sticky="w")
            ctk.CTkLabel(lf, text=label + ":", font=("SF Pro Display", 9),
                         text_color=COLORS["text_dim"]).pack(anchor="w")
            lbl = ctk.CTkLabel(lf, text=val, font=("SF Pro Display", 12, "bold"),
                                text_color=COLORS["text_primary"])
            lbl.pack(anchor="w")
            self.detail_labels[label] = lbl

    def _tab_lead(self):
        f = ctk.CTkScrollableFrame(self.tab_area, fg_color="transparent")
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(f, text="LEAD INFORMATION",
                     font=("SF Pro Display", 11, "bold"),
                     text_color=COLORS["text_secondary"]).grid(
                     row=0, column=0, columnspan=2, sticky="w", pady=(8, 12))

        lead = self.active_lead
        fields = [
            ("First Name",  lead.get("first_name", "")),
            ("Last Name",   lead.get("last_name",  "")),
            ("Phone",       lead.get("phone",      "")),
            ("Email",       lead.get("email",      "")),
            ("Company",     lead.get("company",    "")),
            ("City",        lead.get("city",       "")),
        ]
        for ri, (name, val) in enumerate(fields):
            r, c = divmod(ri, 2)
            box = ctk.CTkFrame(f, fg_color=COLORS["bg_card"], corner_radius=10)
            box.grid(row=r+1, column=c, padx=6, pady=5, sticky="ew")
            ctk.CTkLabel(box, text=name, font=("SF Pro Display", 9),
                         text_color=COLORS["text_secondary"]).pack(anchor="w", padx=12, pady=(8, 0))
            e = ctk.CTkEntry(box, fg_color=COLORS["bg_input"], border_width=0,
                              font=("SF Pro Display", 11),
                              text_color=COLORS["text_primary"], height=34)
            e.pack(fill="x", padx=10, pady=(2, 10))
            if val:
                e.insert(0, str(val))

        # Load from server button
        ctk.CTkButton(f, text="🔄  Refresh from Server",
                      font=("SF Pro Display", 10, "bold"),
                      fg_color=COLORS["accent_blue"], hover_color="#0070d8",
                      corner_radius=10, height=38,
                      command=self._refresh_lead).grid(
                      row=len(fields)//2 + 2, column=0, columnspan=2,
                      sticky="ew", padx=6, pady=8)

    def _tab_disposition(self):
        f = ctk.CTkFrame(self.tab_area, fg_color="transparent")
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(f, text="SUBMIT DISPOSITION",
                     font=("SF Pro Display", 11, "bold"),
                     text_color=COLORS["text_secondary"]).grid(
                     row=0, column=0, sticky="w", pady=(8, 8))

        if not self.dispositions:
            ctk.CTkLabel(f, text="No dispositions available.\nLogin and assign a campaign.",
                         font=("SF Pro Display", 11),
                         text_color=COLORS["text_dim"]).grid(row=1, column=0)
            return

        scroll = ctk.CTkScrollableFrame(f, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure((0, 1), weight=1)

        # Notes
        notes_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_card"], corner_radius=12)
        notes_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0, pady=(0, 10))
        ctk.CTkLabel(notes_frame, text="Call Notes",
                     font=("SF Pro Display", 9), text_color=COLORS["text_secondary"]
                     ).pack(anchor="w", padx=12, pady=(8, 0))
        self._disp_notes = ctk.CTkTextbox(notes_frame, fg_color=COLORS["bg_input"],
                                           font=("SF Pro Display", 11),
                                           text_color=COLORS["text_primary"],
                                           height=70, corner_radius=8, border_width=0)
        self._disp_notes.pack(fill="x", padx=10, pady=(4, 10))

        # Disposition buttons
        for i, d in enumerate(self.dispositions):
            r, c = divmod(i, 2)
            color = d.get("color", "#6B7280")
            ctk.CTkButton(scroll, text=d["name"],
                           font=("SF Pro Display", 10, "bold"),
                           fg_color=blend_with_bg(color, "55"),
                           hover_color=blend_with_bg(color, "88"),
                           text_color=color,
                           corner_radius=10, height=42,
                           border_width=1, border_color=blend_with_bg(color, "66"),
                           command=lambda did=d["id"], dn=d["name"]: self._submit_disposition(did, dn)
                           ).grid(row=r+1, column=c, padx=4, pady=4, sticky="ew")

    def _tab_scripts(self):
        f = ctk.CTkFrame(self.tab_area, fg_color="transparent")
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text="CALL SCRIPTS",
                     font=("SF Pro Display", 11, "bold"),
                     text_color=COLORS["text_secondary"]).grid(
                     row=0, column=0, sticky="w", pady=(8, 8))
        scripts = {
            "Opening Script": (
                "Hello, this is [Agent Name] calling from DialFlow.\n\n"
                "May I please speak with [Contact Name]?\n\n"
                "I'm reaching out today because [Reason for Call].\n\n"
                "Is this a good time to talk for a few minutes?"
            ),
            "Objection Handler": (
                "I completely understand your concern.\n\n"
                "Many of our clients felt the same way at first.\n\n"
                "What they found was that [Solution].\n\n"
                "Would it make sense to explore this further?"
            ),
        }
        nb = ctk.CTkTabview(f, fg_color=COLORS["bg_card"],
                             segmented_button_fg_color=COLORS["bg_card2"],
                             segmented_button_selected_color=COLORS["accent_blue"],
                             corner_radius=12)
        nb.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        for name, script in scripts.items():
            tab = nb.add(name)
            txt = ctk.CTkTextbox(tab, fg_color=COLORS["bg_input"],
                                  font=("SF Pro Display", 11),
                                  text_color=COLORS["text_primary"],
                                  corner_radius=10, border_width=0)
            txt.pack(fill="both", expand=True, padx=4, pady=4)
            txt.insert("1.0", script)

    def _tab_notes(self):
        f = ctk.CTkFrame(self.tab_area, fg_color="transparent")
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", pady=(8, 8))
        ctk.CTkLabel(hdr, text="CALL NOTES",
                     font=("SF Pro Display", 11, "bold"),
                     text_color=COLORS["text_secondary"]).pack(side="left")
        ctk.CTkLabel(hdr, text=f"  {datetime.now().strftime('%b %d, %Y')}",
                     font=("SF Pro Display", 9),
                     text_color=COLORS["text_dim"]).pack(side="left", pady=4)
        self.notes_box = ctk.CTkTextbox(f, fg_color=COLORS["bg_card"],
                                         font=("SF Pro Display", 11),
                                         text_color=COLORS["text_primary"],
                                         corner_radius=12, border_width=1,
                                         border_color=COLORS["border"])
        self.notes_box.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        if self.notes_text:
            self.notes_box.insert("1.0", self.notes_text)
        bf = ctk.CTkFrame(f, fg_color="transparent")
        bf.grid(row=2, column=0, sticky="ew")
        ctk.CTkButton(bf, text="💾  Save Notes",
                      font=("SF Pro Display", 10, "bold"),
                      fg_color=COLORS["accent_green"], hover_color="#00b87a",
                      text_color="#000", corner_radius=10, height=38,
                      command=self._save_notes).pack(side="left")
        ctk.CTkButton(bf, text="🗑  Clear",
                      font=("SF Pro Display", 10),
                      fg_color=blend_with_bg(COLORS["accent_red"], "33"),
                      hover_color=blend_with_bg(COLORS["accent_red"], "55"),
                      text_color=COLORS["accent_red"],
                      corner_radius=10, height=38,
                      command=lambda: self.notes_box.delete("1.0", "end")
                      ).pack(side="left", padx=8)

    def _save_notes(self):
        if hasattr(self, "notes_box"):
            self.notes_text = self.notes_box.get("1.0", "end")
        messagebox.showinfo("Saved", "Notes saved successfully!")

    # ─────────────────────────────────────────────────────────────────────────
    # SIP SETTINGS DIALOG
    # ─────────────────────────────────────────────────────────────────────────
    def _sip_dialog(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("SIP Account Configuration")
        dlg.geometry("500x600")
        dlg.configure(fg_color=COLORS["bg_secondary"])
        dlg.grab_set()
        dlg.resizable(False, False)

        hdr = ctk.CTkFrame(dlg, fg_color=COLORS["bg_card"], corner_radius=0, height=80)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="🔌  SIP Account Setup",
                     font=("SF Pro Display", 16, "bold"),
                     text_color=COLORS["text_primary"]).place(relx=.5, rely=.4, anchor="center")
        ctk.CTkLabel(hdr, text="Configure your SIP credentials to register",
                     font=("SF Pro Display", 10),
                     text_color=COLORS["text_secondary"]).place(relx=.5, rely=.72, anchor="center")

        form = ctk.CTkScrollableFrame(dlg, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=24, pady=16)

        entries = {}
        for label, key, placeholder, is_pass in [
            ("SIP Server / Domain", "server",    "sip.example.com", False),
            ("SIP Port",            "port",      "5060",            False),
            ("Username",            "user",      "4002",            False),
            ("Password",            "password",  "••••••",          True),
            ("Display Name",        "display",   "Agent Name",      False),
            ("Extension",           "extension", "4002",            False),
        ]:
            ctk.CTkLabel(form, text=label, font=("SF Pro Display", 10, "bold"),
                         text_color=COLORS["text_secondary"], anchor="w").pack(fill="x", pady=(10, 3))
            e = ctk.CTkEntry(form, placeholder_text=placeholder,
                              font=("SF Pro Display", 12),
                              fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                              text_color=COLORS["text_primary"],
                              show="*" if is_pass else "", height=42, corner_radius=10)
            e.pack(fill="x")
            if key in self.sip_config:
                e.insert(0, self.sip_config[key])
            entries[key] = e

        bf = ctk.CTkFrame(dlg, fg_color="transparent")
        bf.pack(fill="x", padx=24, pady=16)
        bf.grid_columnconfigure((0, 1), weight=1)

        def _connect():
            cfg = {k: e.get().strip() for k, e in entries.items()}
            if not cfg["server"] or not cfg["user"] or not cfg["password"]:
                messagebox.showerror("Error", "Server, Username and Password are required.", parent=dlg)
                return
            if not cfg["port"]:
                cfg["port"] = "5060"
            self.sip_config = cfg
            self.sip.disconnect()
            self.sip = SIPClient(self.eq)
            self.sip_status_v.set(f"Connecting to {cfg['server']}…")
            self.sip_badge.update(f"SIP: Connecting…", COLORS["accent_orange"])
            self.phone_status_lbl.configure(text="CONNECTING…", text_color=COLORS["accent_orange"])
            self.phone_ext_lbl.configure(text=f"EXTENSION: {cfg['extension']}")
            dlg.destroy()
            threading.Thread(
                target=self.sip.connect,
                args=(cfg["server"], cfg["port"], cfg["user"], cfg["password"], cfg["extension"]),
                daemon=True,
            ).start()

        ctk.CTkButton(bf, text="🔗  Connect & Register",
                      font=("SF Pro Display", 12, "bold"),
                      fg_color=COLORS["accent_green"], hover_color="#00b87a",
                      text_color="#000", corner_radius=12, height=46,
                      command=_connect).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(bf, text="Cancel",
                      font=("SF Pro Display", 12),
                      fg_color=COLORS["bg_card"], hover_color=COLORS["hover"],
                      corner_radius=12, height=46,
                      command=dlg.destroy).grid(row=0, column=1, padx=(6, 0), sticky="ew")

    def _sip_disconnect(self):
        self.sip.disconnect()
        self.sip_status_v.set("Disconnected")
        self.sip_badge.update("SIP: Disconnected", COLORS["accent_red"])
        self.phone_status_lbl.configure(text="DISCONNECTED", text_color=COLORS["accent_red"])
        if hasattr(self, "conn_indicator"):
            self.conn_indicator.configure(text="● DISCONNECTED", text_color=COLORS["accent_red"])

    # ─────────────────────────────────────────────────────────────────────────
    # CALL CONTROLS
    # ─────────────────────────────────────────────────────────────────────────
    def _call_toggle(self):
        if self.call_active:
            self._hangup()
        else:
            num = self.dial_var.get().strip()
            if not num:
                messagebox.showwarning("No Number", "Enter a number to call.")
                return
            if not self.sip.registered:
                if messagebox.askyesno("Not Registered", "SIP not registered. Open settings?"):
                    self._sip_dialog()
                return
            self.current_num = num
            self._show_calling(num)
            self.call_btn.configure(text="📵   HANG UP", fg_color=COLORS["accent_red"],
                                    hover_color="#cc0033", text_color="#fff")
            self.call_status_badge.update(f"CALLING {num}", COLORS["accent_orange"])
            self.sip.make_call(num)

    def _hangup(self):
        self.sip.hangup()
        self._end_call()

    def _end_call(self):
        if self.call_start:
            self.talk_secs += int(time.time() - self.call_start)
        self.call_active = False
        self.call_start  = None
        self.call_btn.configure(text="📞   CALL", fg_color=COLORS["accent_green"],
                                 hover_color="#00b87a", text_color="#000")
        self._show_idle()
        self.call_status_badge.update("NO ACTIVE CALL", COLORS["text_dim"])
        self._update_perf()
        if self.active_tab.get() == "CALL INFO":
            self._switch_tab("CALL INFO")

    def _toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self.mute_btn.configure(text="🔇\nMUTED",
                                    fg_color=blend_with_bg(COLORS["accent_red"], "44"),
                                    text_color=COLORS["accent_red"])
        else:
            self.mute_btn.configure(text="🎤\nMUTE", fg_color=COLORS["bg_card"],
                                    text_color=COLORS["text_primary"])

    def _toggle_hold(self):
        self.on_hold = not self.on_hold
        if self.on_hold:
            self.hold_btn.configure(text="▶\nRESUME",
                                    fg_color=blend_with_bg(COLORS["accent_orange"], "44"),
                                    text_color=COLORS["accent_orange"])
        else:
            self.hold_btn.configure(text="⏸\nHOLD", fg_color=COLORS["bg_card"],
                                    text_color=COLORS["text_primary"])

    def _transfer(self):
        if not self.call_active:
            messagebox.showinfo("Transfer", "No active call to transfer.")
            return
        num = simpledialog.askstring("Transfer Call",
                                     "Enter extension or number to transfer to:",
                                     parent=self.root)
        if num:
            messagebox.showinfo("Transfer", f"Transferring call to {num}…")

    # ─────────────────────────────────────────────────────────────────────────
    # AVAILABILITY — synced to Django backend
    # ─────────────────────────────────────────────────────────────────────────
    def _set_status(self, key: str, color: str):
        """Set agent status locally + push to Django API."""
        self.status = key
        self.status_since = time.time()
        self.status_name_lbl.configure(text=key.title())
        self._update_avail_btn(key, color)

        if self.api.logged_in and key in ("ready", "break", "training"):
            def _push():
                result = self.api.set_status(key)
                if result.get("error"):
                    self.eq.put(("api_error", f"Status sync failed: {result['error']}"))
            threading.Thread(target=_push, daemon=True).start()

    def _update_avail_btn(self, key: str, color: str):
        label_map = {
            "ready":    "● AVAILABLE",
            "break":    "● ON BREAK",
            "training": "● TRAINING",
            "on_call":  "● ON CALL",
            "wrapup":   "● WRAP-UP",
            "offline":  "⏻ OFFLINE",
        }
        label = label_map.get(key, f"● {key.upper()}")
        self.avail_btn.configure(text=label, fg_color=color, hover_color=color,
                                  text_color="#000" if key == "ready" else "#fff")

    def _toggle_avail(self):
        if self.status.lower() in ("ready", "available"):
            self._set_status("break", COLORS["accent_orange"])
        else:
            self._set_status("ready", COLORS["accent_green"])

    # ─────────────────────────────────────────────────────────────────────────
    # DISPOSITION SUBMISSION
    # ─────────────────────────────────────────────────────────────────────────
    def _submit_disposition(self, disposition_id: int, disp_name: str):
        notes = ""
        if hasattr(self, "_disp_notes"):
            notes = self._disp_notes.get("1.0", "end").strip()

        call_log_id = self.pending_wrapup_call_log_id

        if not call_log_id:
            messagebox.showinfo("Disposition", f"Disposition '{disp_name}' noted (no active wrapup call).")
            return

        def _push():
            result = self.api.submit_disposition(disposition_id, call_log_id, notes)
            if result.get("error"):
                self.eq.put(("api_error", f"Disposition failed: {result['error']}"))
            else:
                self.eq.put(("disposition_ok", disp_name))

        if self.api.logged_in:
            threading.Thread(target=_push, daemon=True).start()
        else:
            messagebox.showinfo("Disposition", f"Disposition: {disp_name}\nNotes: {notes}")

    # ─────────────────────────────────────────────────────────────────────────
    # LEAD REFRESH
    # ─────────────────────────────────────────────────────────────────────────
    def _refresh_lead(self):
        if not self.api.logged_in:
            messagebox.showinfo("Not logged in", "Login to the server first.")
            return

        def _fetch():
            result = self.api.get_lead_info()
            self.eq.put(("api_lead", result))

        threading.Thread(target=_fetch, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # CALL HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    def _show_history(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Call History")
        dlg.geometry("860x520")
        dlg.configure(fg_color=COLORS["bg_secondary"])
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="📋  Call History",
                     font=("SF Pro Display", 15, "bold"),
                     text_color=COLORS["text_primary"]).pack(pady=16)

        # Reload from server
        def _reload():
            if self.api.logged_in:
                def _bg():
                    hist = self.api.get_call_history()
                    self.eq.put(("api_history", hist))
                threading.Thread(target=_bg, daemon=True).start()

        ctk.CTkButton(dlg, text="🔄  Reload from Server",
                      font=("SF Pro Display", 10),
                      fg_color=COLORS["accent_blue"], hover_color="#0070d8",
                      corner_radius=10, height=34,
                      command=_reload).pack(pady=(0, 8))

        hdr = ctk.CTkFrame(dlg, fg_color=COLORS["bg_card"], corner_radius=10, height=38)
        hdr.pack(fill="x", padx=20)
        hdr.pack_propagate(False)
        for col, w in [("TIME", 130), ("NUMBER", 180), ("LEAD", 160), ("DURATION", 100),
                       ("STATUS", 120), ("DISPOSITION", 140)]:
            ctk.CTkLabel(hdr, text=col, font=("SF Pro Display", 9, "bold"),
                         text_color=COLORS["text_secondary"], width=w
                         ).pack(side="left", padx=10, pady=10)

        rows_frame = ctk.CTkScrollableFrame(dlg, fg_color="transparent")
        rows_frame.pack(fill="both", expand=True, padx=20, pady=8)

        status_colors = {
            "completed":  COLORS["accent_green"],
            "Answered":   COLORS["accent_green"],
            "no_answer":  COLORS["accent_orange"],
            "No Answer":  COLORS["accent_orange"],
            "dropped":    COLORS["accent_red"],
            "Missed":     COLORS["accent_red"],
            "failed":     COLORS["text_secondary"],
        }

        # Use server call history if available, else local
        rows = []
        for c in self.call_history:
            if isinstance(c, dict):
                rows.append((
                    c.get("started_at", "─")[:16] if c.get("started_at") else "─",
                    c.get("phone", "─"),
                    c.get("lead_name", "─"),
                    f"{c.get('duration', 0)}s",
                    c.get("status", "─"),
                    c.get("disposition") or "─",
                ))
            else:
                rows.append(tuple(c) + ("─",) * max(0, 6 - len(c)))

        if not rows:
            ctk.CTkLabel(rows_frame, text="No call history yet.",
                         font=("SF Pro Display", 12),
                         text_color=COLORS["text_secondary"]).pack(pady=40)
            return

        for i, row in enumerate(rows):
            rf = ctk.CTkFrame(rows_frame,
                               fg_color=COLORS["bg_card"] if i % 2 == 0 else COLORS["bg_card2"],
                               corner_radius=8, height=42)
            rf.pack(fill="x", pady=1)
            rf.pack_propagate(False)
            for j, (val, w) in enumerate(zip(row, [130, 180, 160, 100, 120, 140])):
                color = status_colors.get(str(val), COLORS["text_primary"]) if j == 4 else COLORS["text_primary"]
                ctk.CTkLabel(rf, text=str(val)[:20], font=("SF Pro Display", 10),
                              text_color=color, width=w
                              ).pack(side="left", padx=10, pady=10)

    # ─────────────────────────────────────────────────────────────────────────
    # PERFORMANCE
    # ─────────────────────────────────────────────────────────────────────────
    def _update_perf(self):
        rate = f"{int(self.answered/self.total_calls*100)}%" if self.total_calls else "0%"
        m, s = divmod(self.talk_secs, 60)
        talk = f"{m}m {s}s" if m else f"{s}s"
        self.perf_cards["TOTAL CALLS"].update_value(str(self.total_calls))
        self.perf_cards["ANSWERED"].update_value(str(self.answered))
        self.perf_cards["TALK TIME"].update_value(talk)
        self.perf_cards["CONTACT RATE"].update_value(rate)
        if hasattr(self, "info_cards"):
            self.info_cards["CALLS TODAY"].update_value(str(self.total_calls))
            self.info_cards["ANSWERED"].update_value(str(self.answered))

    # ─────────────────────────────────────────────────────────────────────────
    # EVENT QUEUE POLL (SIP + API + WebSocket events)
    # ─────────────────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                ev, data = self.eq.get_nowait()
                self._dispatch(ev, data)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _dispatch(self, ev: str, data):
        # ── SIP events ────────────────────────────────────────────────────────
        if ev == "registered":
            ext = self.sip_config.get("extension", data)
            self.sip_status_v.set(f"Registered  ·  Ext {ext}  ·  {self.sip_config.get('server','')}")
            self.sip_badge.update(f"SIP: Ext {ext}", COLORS["accent_green"])
            self.phone_status_lbl.configure(text="REGISTERED", text_color=COLORS["accent_green"])
            self.phone_ext_lbl.configure(text=f"EXTENSION: {ext}")
            if hasattr(self, "conn_indicator"):
                self.conn_indicator.configure(text="● CONNECTED", text_color=COLORS["accent_green"])
            if hasattr(self, "detail_labels"):
                for k, label in [("Server","server"),("Port","port"),("Username","user"),("Extension","extension")]:
                    self.detail_labels[k].configure(text=self.sip_config.get(label, "─"))
            if hasattr(self, "info_cards"):
                self.info_cards["EXTENSION"].update_value(ext, "Registered")

        elif ev == "error":
            self.sip_status_v.set(f"Error: {data}")
            self.sip_badge.update("SIP: Error", COLORS["accent_red"])
            self.phone_status_lbl.configure(text="ERROR", text_color=COLORS["accent_red"])
            messagebox.showerror("SIP Error", data)

        elif ev == "calling":
            self.total_calls += 1
            self._update_perf()

        elif ev == "trying":
            if hasattr(self, "ring_lbl"):
                self.ring_lbl.configure(text="Trying…", text_color=COLORS["accent_orange"])

        elif ev == "ringing":
            if hasattr(self, "ring_lbl"):
                self.ring_lbl.configure(text="Ringing…", text_color=COLORS["accent_cyan"])

        elif ev == "call_answered":
            self.call_active = True
            self.call_start  = time.time()
            self.answered   += 1
            self._show_active(self.current_num)
            self.call_status_badge.update(f"IN CALL · {self.current_num}", COLORS["accent_green"])
            # Fetch lead info for this number in background
            if self.api.logged_in:
                threading.Thread(target=lambda: self.eq.put(("api_lead", self.api.get_lead_info())),
                                 daemon=True).start()

        elif ev == "call_ended":
            self.call_history.append({
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "phone":      self.current_num,
                "lead_name":  self.active_lead.get("full_name", self.current_num),
                "duration":   int(time.time() - self.call_start) if self.call_start else 0,
                "status":     "completed",
                "disposition": None,
            })
            self._end_call()

        elif ev == "call_failed":
            self._end_call()
            messagebox.showwarning("Call Failed", f"Call failed:\n{data}")

        elif ev == "incoming_call":
            self.total_calls += 1
            self.current_num = data
            if self.auto_answer.get():
                self.call_active = True
                self.call_start  = time.time()
                self.answered   += 1
                self._show_active(data)
                self.call_status_badge.update(f"IN CALL · {data}", COLORS["accent_green"])
            else:
                self._incoming_popup(data)

        # ── API events ────────────────────────────────────────────────────────
        elif ev == "api_lead":
            if "error" not in data:
                self.active_lead = data
                if self.active_tab.get() == "LEAD":
                    self._switch_tab("LEAD")

        elif ev == "api_dispositions":
            disps = data.get("dispositions", [])
            if disps:
                self.dispositions = disps
                if self.active_tab.get() == "DISPOSITION":
                    self._switch_tab("DISPOSITION")

        elif ev == "api_history":
            calls = data.get("calls", [])
            if calls:
                self.call_history = calls
                total   = len(calls)
                answered = sum(1 for c in calls if c.get("status") == "completed")
                talk_s   = sum(c.get("duration", 0) for c in calls)
                self.total_calls = total
                self.answered    = answered
                self.talk_secs   = talk_s
                self._update_perf()

        elif ev == "api_error":
            # Non-fatal — just log to status
            self.api_status_v.set(str(data)[:80])

        elif ev == "disposition_ok":
            self.pending_wrapup_call_log_id = None
            messagebox.showinfo("Disposition", f"Disposition '{data}' submitted successfully!")
            if self.active_tab.get() == "DISPOSITION":
                self._switch_tab("CALL INFO")

        # ── WebSocket events ──────────────────────────────────────────────────
        elif ev == "ws_connected":
            self.api_status_v.set(
                self.api_status_v.get().replace("(WS: off)", "") + " · Live"
            )

        elif ev == "ws_disconnected":
            pass  # auto-reconnect handled in AgentWebSocket

        elif ev == "ws_event":
            self._handle_ws_event(data)

        elif ev == "ws_error":
            pass  # auto-reconnect handles it

    def _handle_ws_event(self, data: dict):
        """Handle push events from Django Channels AgentConsumer."""
        etype = data.get("type", "")

        if etype == "snapshot":
            # Initial state sync on WS connect
            server_status = data.get("status", self.status)
            self.status = server_status
            self.status_since = time.time()
            self.active_campaign_id = data.get("active_campaign_id")
            color_map = {
                "ready":    COLORS["accent_green"],
                "break":    COLORS["accent_orange"],
                "training": COLORS["accent_blue"],
                "on_call":  COLORS["accent_cyan"],
                "wrapup":   COLORS["accent_purple"],
                "offline":  COLORS["text_secondary"],
            }
            self._update_avail_btn(server_status, color_map.get(server_status, COLORS["text_secondary"]))

            # Today's stats from snapshot
            stats = data.get("stats_today", {})
            if stats:
                self.total_calls = stats.get("calls", self.total_calls)
                self.answered    = stats.get("answered", self.answered)
                self.talk_secs   = stats.get("talk_sec", self.talk_secs)
                self._update_perf()

            # Dispositions
            disps = data.get("dispositions", [])
            if disps:
                self.dispositions = disps

            # Campaign name
            campaigns = data.get("campaigns", [])
            if campaigns and hasattr(self, "info_cards"):
                self.info_cards["CAMPAIGN"].update_value(campaigns[0].get("name", "None"))

            # Wrapup pending
            self.pending_wrapup_call_log_id = data.get("pending_call_log_id")
            if server_status == "wrapup" and self.pending_wrapup_call_log_id:
                pending = data.get("pending_call", {})
                phone = pending.get("phone", self.current_num) if pending else self.current_num
                self._show_wrapup(phone)
                self.call_status_badge.update("WRAP-UP", COLORS["accent_purple"])
                self._switch_tab("DISPOSITION")

        elif etype == "incoming_call":
            phone = data.get("phone_number", data.get("caller_id", "Unknown"))
            self.eq.put(("incoming_call", phone))

        elif etype == "call_connected":
            self.call_active = True
            self.call_start  = time.time()
            self.current_num = data.get("phone_number", self.current_num)
            self.answered   += 1
            self._show_active(self.current_num)
            self.call_status_badge.update(f"IN CALL · {self.current_num}", COLORS["accent_green"])
            if self.api.logged_in:
                threading.Thread(target=lambda: self.eq.put(("api_lead", self.api.get_lead_info())),
                                 daemon=True).start()

        elif etype == "call_ended":
            self._end_call()

        elif etype == "wrapup_started":
            self.pending_wrapup_call_log_id = data.get("call_log_id")
            phone = data.get("phone_number", self.current_num)
            self._end_call()
            self._show_wrapup(phone)
            self.call_status_badge.update("WRAP-UP", COLORS["accent_purple"])
            self._switch_tab("DISPOSITION")

        elif etype == "status_changed":
            new_status = data.get("status", self.status)
            self.status = new_status
            self.status_since = time.time()
            self.status_name_lbl.configure(text=new_status.title())

        elif etype == "force_logout":
            messagebox.showwarning("Logged Out", data.get("reason", "Supervisor action"))
            self._set_status("offline", COLORS["text_secondary"])

        elif etype == "pong":
            pass  # heartbeat ack

        elif etype == "dispose_ok":
            self.pending_wrapup_call_log_id = None

    # ─────────────────────────────────────────────────────────────────────────
    # INCOMING CALL POPUP
    # ─────────────────────────────────────────────────────────────────────────
    def _incoming_popup(self, number):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Incoming Call")
        dlg.geometry("360x240")
        dlg.configure(fg_color=COLORS["bg_secondary"])
        dlg.attributes("-topmost", True)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="📲", font=("SF Pro Display", 48)).pack(pady=(20, 0))
        ctk.CTkLabel(dlg, text="Incoming Call", font=("SF Pro Display", 12),
                     text_color=COLORS["text_secondary"]).pack()
        ctk.CTkLabel(dlg, text=number, font=("SF Pro Display", 22, "bold"),
                     text_color=COLORS["text_primary"]).pack(pady=4)

        bf = ctk.CTkFrame(dlg, fg_color="transparent")
        bf.pack(pady=16)

        def _ans():
            self.call_active = True
            self.call_start  = time.time()
            self.answered   += 1
            self._show_active(number)
            self.call_status_badge.update(f"IN CALL · {number}", COLORS["accent_green"])
            dlg.destroy()

        ctk.CTkButton(bf, text="✅  Answer",
                      font=("SF Pro Display", 12, "bold"),
                      fg_color=COLORS["accent_green"], hover_color="#00b87a",
                      text_color="#000", corner_radius=20, height=44, width=130,
                      command=_ans).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="❌  Reject",
                      font=("SF Pro Display", 12, "bold"),
                      fg_color=COLORS["accent_red"], hover_color="#cc0033",
                      corner_radius=20, height=44, width=130,
                      command=dlg.destroy).pack(side="left", padx=8)

    # ─────────────────────────────────────────────────────────────────────────
    # CLOCK TICK
    # ─────────────────────────────────────────────────────────────────────────
    def _tick(self):
        now = time.time()

        lt = int(now - self.login_since)
        h, m, s = lt // 3600, (lt % 3600) // 60, lt % 60
        self.login_timer.configure(text=f"{h:02}:{m:02}:{s:02}")

        st = int(now - self.status_since)
        sm, ss = st // 60, st % 60
        self.status_timer.configure(text=f"{sm:02}:{ss:02}")

        if self.call_active and self.call_start and hasattr(self, "call_dur_lbl"):
            ct = int(now - self.call_start)
            ch, cm, cs = ct // 3600, (ct % 3600) // 60, ct % 60
            self.call_dur_lbl.configure(text=f"{ch:02}:{cm:02}:{cs:02}")

        self.root.after(1000, self._tick)

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = AutoDialerApp()
    app.run()
