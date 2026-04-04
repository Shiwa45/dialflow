# core/management/commands/dialer_status.py
"""
Print a live status summary of the dialer system.

Usage:
    python manage.py dialer_status
    python manage.py dialer_status --watch   # refresh every 5s
"""
import time
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Show live status of agents, campaigns, and hopper'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true',
                            help='Refresh every 5 seconds (Ctrl+C to stop)')

    def handle(self, *args, **options):
        while True:
            self._print_status()
            if not options['watch']:
                break
            time.sleep(5)
            self.stdout.write('\033[2J\033[H')  # clear screen

    def _print_status(self):
        from agents.models import AgentStatus
        from campaigns.models import Campaign
        from campaigns.hopper import get_hopper_stats
        from telephony.models import AsteriskServer
        from django.utils import timezone

        now = timezone.now().strftime('%H:%M:%S')
        self.stdout.write(f'\n── DialFlow Status  {now} ─────────────────────────────\n')

        # Asterisk servers
        self.stdout.write('Asterisk Servers:')
        for srv in AsteriskServer.objects.filter(is_active=True):
            icon = '●' if srv.connection_status == 'connected' else '○'
            self.stdout.write(f'  {icon} {srv.name} ({srv.server_ip}) — {srv.connection_status}')

        # Agents
        self.stdout.write('\nAgents:')
        statuses = AgentStatus.objects.select_related('user', 'active_campaign').all()
        if not statuses:
            self.stdout.write('  (none)')
        for s in statuses:
            elapsed = ''
            if s.status_changed_at:
                secs = int((timezone.now() - s.status_changed_at).total_seconds())
                m, ss = divmod(secs, 60)
                elapsed = f' {m}:{ss:02d}'
            camp = f' → {s.active_campaign.name}' if s.active_campaign else ''
            self.stdout.write(f'  {s.user.username:<15} {s.status:<10}{elapsed}{camp}')

        # Campaigns
        self.stdout.write('\nCampaigns:')
        for camp in Campaign.objects.filter(status__in=['active', 'paused']).order_by('status'):
            h = get_hopper_stats(camp.id)
            self.stdout.write(
                f'  [{camp.status.upper():<7}] {camp.name:<25} '
                f'hopper={h["queued"]:>4}  inflight={h["in_flight"]:>3}  '
                f'calls={camp.stat_calls_today:>5}  answered={camp.stat_answered_today:>5}'
            )
        if not Campaign.objects.filter(status__in=['active', 'paused']).exists():
            self.stdout.write('  (no active campaigns)')

        self.stdout.write('')
