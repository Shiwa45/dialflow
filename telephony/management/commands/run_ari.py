from django.core.management.base import BaseCommand
from django.conf import settings
import asyncio
import logging
import sys
import subprocess
from telephony.ari_worker import run_ari_worker

logger = logging.getLogger('telephony.ari_worker')


def _get_wsl2_ip():
    """
    On Windows, the Django process connects to Asterisk (in WSL2) via
    127.0.0.1 → portproxy → WSL2 IP.  Windows portproxy is unreliable for
    short-lived TCP connections (each ARI REST call opens a new socket), so
    we detect the WSL2 IP directly and bypass portproxy entirely.

    Returns the WSL2 IP string, or None if detection fails.
    """
    try:
        result = subprocess.run(
            ['wsl', 'bash', '-c', "hostname -I | awk '{print $1}'"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        if ip and not ip.startswith('127.'):
            return ip
    except Exception as e:
        logger.warning('WSL2 IP detection failed: %s', e)
    return None


def _verify_ari_reachable(host, port, username, password):
    """Quick HTTP check: can we reach Asterisk ARI at host:port?"""
    import requests
    try:
        r = requests.get(
            f'http://{host}:{port}/ari/asterisk/info',
            auth=(username, password),
            timeout=3,
        )
        return r.status_code == 200
    except Exception:
        return False


class Command(BaseCommand):
    help = 'Runs the ARI Worker as a standalone process.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting ARI Worker process...'))

        cfg = dict(getattr(settings, 'ASTERISK', {}))
        if not cfg.get('ARI_HOST'):
            self.stdout.write(self.style.ERROR('ASTERISK config missing in settings.'))
            return

        # ── WSL2 direct-IP fix ───────────────────────────────────────────────
        # If running on Windows and ARI_HOST is a loopback address, the
        # Django process reaches Asterisk (in WSL2) via netsh portproxy.
        # portproxy is reliable for the long-lived ARI WebSocket but NOT for
        # short-lived REST calls (each call opens a new TCP socket).  Detect
        # the WSL2 IP and connect directly — no portproxy needed.
        if sys.platform == 'win32' and cfg['ARI_HOST'] in ('127.0.0.1', 'localhost'):
            self.stdout.write('Windows detected — checking direct WSL2 connection...')
            wsl2_ip = _get_wsl2_ip()
            if wsl2_ip:
                direct_ok = _verify_ari_reachable(wsl2_ip, cfg['ARI_PORT'],
                                                  cfg['ARI_USERNAME'], cfg['ARI_PASSWORD'])
                if direct_ok:
                    self.stdout.write(self.style.SUCCESS(
                        f'Using WSL2 IP directly: {wsl2_ip}:{cfg["ARI_PORT"]} (bypassing portproxy)'
                    ))
                    cfg['ARI_HOST'] = wsl2_ip
                else:
                    # Try via configured host (portproxy path)
                    proxy_ok = _verify_ari_reachable(cfg['ARI_HOST'], cfg['ARI_PORT'],
                                                     cfg['ARI_USERNAME'], cfg['ARI_PASSWORD'])
                    if proxy_ok:
                        self.stdout.write(f'WSL2 direct unreachable; using portproxy path: '
                                          f'{cfg["ARI_HOST"]}:{cfg["ARI_PORT"]}')
                    else:
                        self.stdout.write(self.style.WARNING(
                            f'ARI unreachable at both {wsl2_ip} and {cfg["ARI_HOST"]} '
                            f'(port {cfg["ARI_PORT"]}). Asterisk may not be running yet — '
                            f'will keep retrying...'
                        ))
                        # Fall through — run_ari_worker has its own reconnect loop
            else:
                self.stdout.write('Could not detect WSL2 IP — using configured ARI_HOST')
        # ────────────────────────────────────────────────────────────────────

        self.stdout.write(
            f'ARI host: {cfg["ARI_HOST"]}:{cfg["ARI_PORT"]}  '
            f'app={cfg["ARI_APP_NAME"]}  user={cfg["ARI_USERNAME"]}'
        )

        try:
            asyncio.run(run_ari_worker(cfg))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('ARI Worker stopped by user.'))
        except Exception as e:
            logger.exception(f'ARI Worker process crashed: {e}')
            self.stdout.write(self.style.ERROR(f'ARI Worker crashed: {e}'))
