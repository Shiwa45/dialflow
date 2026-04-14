"""
python manage.py ari_diag

Diagnostic command — connects to Asterisk ARI directly and prints:
  • Connection status (can we reach ARI?)
  • Active channels (what calls are live in Asterisk right now)
  • Agent status in Django DB
  • Whether ARI_HOST resolves correctly (WSL2 check)

Run this from the DialFlow venv when bridge creation isn't working.
"""
import sys
import subprocess
import requests
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Diagnose ARI/WSL2 connectivity and show live call state.'

    def handle(self, *args, **options):
        cfg = getattr(settings, 'ASTERISK', {})
        host = cfg.get('ARI_HOST', '127.0.0.1')
        port = cfg.get('ARI_PORT', 8088)
        user = cfg.get('ARI_USERNAME', 'asterisk')
        pw   = cfg.get('ARI_PASSWORD', '')
        auth = (user, pw)
        base = f'http://{host}:{port}/ari'

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'DialFlow ARI Diagnostic')
        self.stdout.write(f'{"="*60}')

        # ── 1. Detect WSL2 IP ─────────────────────────────────────────────────
        wsl2_ip = None
        if sys.platform == 'win32':
            try:
                r = subprocess.run(
                    ['wsl', 'bash', '-c', "hostname -I | awk '{print $1}'"],
                    capture_output=True, text=True, timeout=5,
                )
                wsl2_ip = r.stdout.strip()
            except Exception as e:
                wsl2_ip = f'(detection failed: {e})'

        self.stdout.write(f'\n[1] WSL2 IP:   {wsl2_ip or "N/A (not Windows)"}')
        self.stdout.write(f'    ARI_HOST:  {host}:{port}')

        if sys.platform == 'win32' and host in ('127.0.0.1', 'localhost'):
            self.stdout.write(
                self.style.WARNING(
                    '    WARNING: ARI_HOST is 127.0.0.1 on Windows — this uses portproxy\n'
                    '    to reach WSL2 and is unreliable for short-lived REST calls.\n'
                    '    Re-run start_dialflow.bat (as Admin) to fix ARI_HOST → WSL2 IP.'
                )
            )

        # ── 2. ARI connectivity ───────────────────────────────────────────────
        self.stdout.write(f'\n[2] ARI connectivity ({base}):')
        try:
            resp = requests.get(f'{base}/asterisk/info', auth=auth, timeout=4)
            if resp.status_code == 200:
                info = resp.json()
                sys_info = info.get('system', {})
                self.stdout.write(self.style.SUCCESS(
                    f'    OK — Asterisk {sys_info.get("version","?")} '
                    f'entity={info.get("id","?")}'
                ))
            else:
                self.stdout.write(self.style.ERROR(f'    HTTP {resp.status_code}: {resp.text[:200]}'))
        except requests.exceptions.ConnectionError as e:
            self.stdout.write(self.style.ERROR(f'    CONNECTION REFUSED: {e}'))
            if wsl2_ip and wsl2_ip != host:
                self.stdout.write(f'    Trying WSL2 IP directly ({wsl2_ip})...')
                try:
                    r2 = requests.get(f'http://{wsl2_ip}:{port}/ari/asterisk/info', auth=auth, timeout=4)
                    if r2.status_code == 200:
                        self.stdout.write(self.style.SUCCESS(
                            f'    Direct WSL2 connection WORKS at {wsl2_ip}:{port}\n'
                            f'    FIX: re-run start_dialflow.bat to update ARI_HOST in .env'
                        ))
                    else:
                        self.stdout.write(f'    Direct WSL2 returned HTTP {r2.status_code}')
                except Exception as e2:
                    self.stdout.write(f'    Direct WSL2 also failed: {e2}')
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'    Error: {e}'))
            return

        # ── 3. Live channels ──────────────────────────────────────────────────
        self.stdout.write(f'\n[3] Live Asterisk channels:')
        try:
            channels = requests.get(f'{base}/channels', auth=auth, timeout=4).json()
            if not channels:
                self.stdout.write('    (none)')
            for ch in channels:
                name  = ch.get('name', '?')
                state = ch.get('state', '?')
                caller = ch.get('caller', {}).get('number', '?')
                self.stdout.write(f'    {ch["id"]}  {name}  state={state}  caller={caller}')
        except Exception as e:
            self.stdout.write(f'    Error: {e}')

        # ── 4. Live bridges ───────────────────────────────────────────────────
        self.stdout.write(f'\n[4] Live Asterisk bridges:')
        try:
            bridges = requests.get(f'{base}/bridges', auth=auth, timeout=4).json()
            if not bridges:
                self.stdout.write('    (none)')
            for br in bridges:
                self.stdout.write(
                    f'    {br["id"]}  channels={br.get("channels",[])}  '
                    f'type={br.get("bridge_type","?")}  name={br.get("name","?")}'
                )
        except Exception as e:
            self.stdout.write(f'    Error: {e}')

        # ── 5. PJSIP endpoints ────────────────────────────────────────────────
        self.stdout.write(f'\n[5] PJSIP endpoint registration state:')
        try:
            endpoints = requests.get(f'{base}/endpoints/PJSIP', auth=auth, timeout=4).json()
            if not endpoints:
                self.stdout.write('    (none)')
            for ep in endpoints:
                eid    = ep.get('resource', '?')
                state  = ep.get('state', '?')
                chans  = ep.get('channel_ids', [])
                color  = self.style.SUCCESS if state == 'online' else self.style.WARNING
                self.stdout.write(color(f'    {eid}  state={state}  channels={chans}'))
        except Exception as e:
            self.stdout.write(f'    Error: {e}')

        # ── 6. Django agent DB state ──────────────────────────────────────────
        self.stdout.write(f'\n[6] Django agent status (DB):')
        try:
            from agents.models import AgentStatus
            for st in AgentStatus.objects.select_related('user', 'active_campaign').all():
                color = self.style.SUCCESS if st.status == 'ready' else (
                    self.style.WARNING if st.status in ('on_call', 'wrapup') else self.style.ERROR
                )
                camp = st.active_campaign.name if st.active_campaign else '--'
                self.stdout.write(color(
                    f'    {st.user.username}  status={st.status}  '
                    f'campaign={camp}  channel={st.active_channel_id or "--"}'
                ))
        except Exception as e:
            self.stdout.write(f'    Error: {e}')

        # ── 7. PJSIP realtime rows ────────────────────────────────────────────
        self.stdout.write(f'\n[7] ps_endpoints (Django ORM view):')
        try:
            from telephony.models import PjsipEndpoint, PjsipAuth, PjsipAor
            for ep in PjsipEndpoint.objects.all().order_by('id'):
                has_auth = PjsipAuth.objects.filter(id=ep.id).exists()
                has_aor  = PjsipAor.objects.filter(id=ep.id).exists()
                self.stdout.write(
                    f'    {ep.id}  transport={ep.transport}  '
                    f'dtls_auto_cert={ep.dtls_auto_generate_cert}  '
                    f'rtcp_mux={ep.rtcp_mux}  '
                    f'auth={"OK" if has_auth else "MISSING"}  '
                    f'aor={"OK" if has_aor else "MISSING"}'
                )
        except Exception as e:
            self.stdout.write(f'    Error: {e}')

        self.stdout.write(f'\n{"="*60}\n')
