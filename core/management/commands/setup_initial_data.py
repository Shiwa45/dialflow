# core/management/commands/setup_initial_data.py
"""
Setup Initial Data
==================
Run once after migrations to seed:
  • System dispositions (sale, callback, DNC, not interested, no answer, busy)
  • Asterisk server (from settings / .env)
  • Sample campaign
  • Admin user's phone extension
  • Celery beat schedule entries

Usage:
    python manage.py setup_initial_data
    python manage.py setup_initial_data --reset    # wipe and re-seed
"""
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Seed initial data for a new DialFlow Pro installation'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing data before seeding')

    def handle(self, *args, **options):
        self.reset = options['reset']
        self.stdout.write(self.style.SUCCESS('\n── DialFlow Pro Setup ──────────────────────────\n'))

        self._seed_dispositions()
        self._seed_asterisk_server()
        self._seed_admin_phone()
        self._seed_sample_campaign()
        self._setup_beat_schedule()
        self._print_summary()

    # ── Dispositions ──────────────────────────────────────────────────────────

    def _seed_dispositions(self):
        from campaigns.models import Disposition

        self.stdout.write('  Seeding dispositions…')

        DISPOSITIONS = [
            # name, category, outcome, color, hotkey, sort
            ('Sale',            'sale',           'complete',  '#10B981', 's', 1),
            ('Callback',        'callback',       'callback',  '#3B82F6', 'c', 2),
            ('Not Interested',  'not_interested', 'recycle',   '#6B7280', 'n', 3),
            ('No Answer',       'no_answer',      'recycle',   '#F59E0B', 'a', 4),
            ('Busy',            'busy',           'recycle',   '#F97316', 'b', 5),
            ('Voicemail',       'other',          'recycle',   '#8B5CF6', 'v', 6),
            ('Wrong Number',    'other',          'complete',  '#EC4899', 'w', 7),
            ('Do Not Call',     'dnc',            'dnc',       '#EF4444', 'd', 8),
            ('Language Barrier','other',          'recycle',   '#06B6D4', 'l', 9),
            ('Not Dispositioned','other',         'recycle',   '#9CA3AF', '',  10),
        ]

        if self.reset:
            Disposition.objects.filter(is_system=True).delete()

        created = 0
        for name, category, outcome, color, hotkey, sort in DISPOSITIONS:
            _, c = Disposition.objects.get_or_create(
                name=name,
                defaults={
                    'category':   category,
                    'outcome':    outcome,
                    'color':      color,
                    'hotkey':     hotkey,
                    'sort_order': sort,
                    'is_system':  True,
                    'is_active':  True,
                }
            )
            if c:
                created += 1

        self.stdout.write(self.style.SUCCESS(f'    ✓ {created} dispositions created'))

    # ── Asterisk Server ───────────────────────────────────────────────────────

    def _seed_asterisk_server(self):
        from telephony.models import AsteriskServer
        self.stdout.write('  Seeding Asterisk server…')

        cfg = settings.ASTERISK

        server, created = AsteriskServer.objects.get_or_create(
            name='Primary Asterisk',
            defaults={
                'description':  'Auto-created by setup_initial_data',
                'server_ip':    cfg.get('ARI_HOST', '127.0.0.1'),
                'ari_host':     cfg.get('ARI_HOST', '127.0.0.1'),
                'ari_port':     cfg.get('ARI_PORT', 8088),
                'ari_username': cfg.get('ARI_USERNAME', 'asterisk'),
                'ari_password': cfg.get('ARI_PASSWORD', 'asterisk'),
                'ari_app_name': cfg.get('ARI_APP_NAME', 'dialflow'),
                'ami_host':     cfg.get('AMI_HOST', '127.0.0.1'),
                'ami_port':     cfg.get('AMI_PORT', 5038),
                'ami_username': cfg.get('AMI_USERNAME', 'admin'),
                'ami_password': cfg.get('AMI_PASSWORD', 'admin'),
                'is_active':    True,
            }
        )
        self._asterisk_server = server
        status = 'created' if created else 'already exists'
        self.stdout.write(self.style.SUCCESS(f'    ✓ Asterisk server "{server.name}" {status}'))

    # ── Admin Phone Extension ─────────────────────────────────────────────────

    def _seed_admin_phone(self):
        from telephony.models import Phone
        from django.contrib.auth import get_user_model
        User = get_user_model()

        self.stdout.write('  Seeding admin phone extension…')

        try:
            admin = User.objects.filter(is_superuser=True).first()
            if not admin:
                self.stdout.write('    ⚠ No superuser found — skipping phone extension')
                return

            phone, created = Phone.objects.get_or_create(
                extension='1000',
                defaults={
                    'name':           f'{admin.get_full_name() or admin.username} (Admin)',
                    'phone_type':     'webrtc',
                    'user':           admin,
                    'asterisk_server': self._asterisk_server,
                    'context':        'agents',
                    'allow_codecs':   'opus,ulaw,alaw',
                    'is_active':      True,
                }
            )
            status = 'created' if created else 'already exists'
            self.stdout.write(self.style.SUCCESS(
                f'    ✓ Extension 1000 for {admin.username} {status} (password: {phone.secret})'
            ))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'    ⚠ Phone seed skipped: {e}'))

    # ── Sample Campaign ───────────────────────────────────────────────────────

    def _seed_sample_campaign(self):
        from campaigns.models import Campaign, Disposition
        from django.contrib.auth import get_user_model
        User = get_user_model()

        self.stdout.write('  Seeding sample campaign…')

        admin = User.objects.filter(is_superuser=True).first()

        campaign, created = Campaign.objects.get_or_create(
            name='Sample Campaign',
            defaults={
                'description':    'Auto-created sample campaign. Edit settings before use.',
                'status':         Campaign.STATUS_DRAFT,
                'asterisk_server': self._asterisk_server,
                'dial_mode':      Campaign.DIAL_MODE_PREDICTIVE,
                'dial_ratio':     '1.5',
                'min_dial_ratio': '1.0',
                'max_dial_ratio': '3.0',
                'dial_timeout':   30,
                'abandon_rate':   '3.0',
                'hopper_level':   100,
                'hopper_size':    500,
                'enable_recording': True,
                'auto_wrapup_enabled': True,
                'auto_wrapup_timeout': 120,
                'max_attempts':   3,
                'retry_delay_minutes': 60,
                'created_by':     admin,
            }
        )

        if created:
            # Attach all system dispositions
            all_disps = Disposition.objects.filter(is_system=True)
            campaign.dispositions.set(all_disps)

            # Set auto-wrapup disposition to "Not Dispositioned"
            nd = Disposition.objects.filter(name='Not Dispositioned').first()
            if nd:
                campaign.auto_wrapup_disposition = nd
                campaign.save(update_fields=['auto_wrapup_disposition'])

        status = 'created' if created else 'already exists'
        self.stdout.write(self.style.SUCCESS(f'    ✓ Sample campaign {status}'))

    # ── Celery Beat Schedule ─────────────────────────────────────────────────

    def _setup_beat_schedule(self):
        self.stdout.write('  Setting up Celery beat schedule…')
        try:
            from django.core.management import call_command
            call_command('setup_beat_schedule', verbosity=0)
            self.stdout.write(self.style.SUCCESS('    ✓ Beat schedule configured'))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'    ⚠ Beat schedule skipped: {e}'))
            self.stdout.write('      (Run: python manage.py setup_beat_schedule manually)')

    # ── Summary ───────────────────────────────────────────────────────────────

    def _print_summary(self):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('── Setup complete! ─────────────────────────────'))
        self.stdout.write('')
        self.stdout.write('  Next steps:')
        self.stdout.write('  1. Update .env with your Asterisk host/credentials')
        self.stdout.write('  2. Configure Asterisk PJSIP realtime (ODBC → dialflow_db)')
        self.stdout.write('  3. Assign agents to the Sample Campaign via Admin')
        self.stdout.write('  4. Run:')
        self.stdout.write('       python manage.py runserver')
        self.stdout.write('       celery -A dialflow worker -l info')
        self.stdout.write('       celery -A dialflow beat -l info')
        self.stdout.write('')
        self.stdout.write('  Access the system:')
        self.stdout.write('    Admin panel : http://localhost:8000/admin/')
        self.stdout.write('    Campaigns   : http://localhost:8000/campaigns/')
        self.stdout.write('    Monitor     : http://localhost:8000/agents/supervisor/')
        self.stdout.write('')
