# core/management/commands/create_agent.py
"""
Quick agent creation with phone extension.

Usage:
    python manage.py create_agent --username ravi --extension 1001 --password secret123
    python manage.py create_agent --username priya --extension 1002 --first-name Priya --last-name Kumar
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create an agent user with a phone extension in one step'

    def add_arguments(self, parser):
        parser.add_argument('--username',   required=True)
        parser.add_argument('--extension',  required=True)
        parser.add_argument('--password',   default='changeme123')
        parser.add_argument('--first-name', default='')
        parser.add_argument('--last-name',  default='')
        parser.add_argument('--email',      default='')
        parser.add_argument('--phone-type', default='webrtc', choices=['webrtc', 'sip'])

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from telephony.models import AsteriskServer, Phone

        User = get_user_model()

        # Validate
        if User.objects.filter(username=options['username']).exists():
            raise CommandError(f"User '{options['username']}' already exists.")

        server = AsteriskServer.objects.filter(is_active=True).first()
        if not server:
            raise CommandError("No active Asterisk server found. Run setup_initial_data first.")

        if Phone.objects.filter(extension=options['extension']).exists():
            raise CommandError(f"Extension {options['extension']} already exists.")

        # Create user
        user = User.objects.create_user(
            username   = options['username'],
            password   = options['password'],
            first_name = options['first_name'],
            last_name  = options['last_name'],
            email      = options['email'],
            role       = 'agent',
        )

        # Create phone extension (signal will sync to Asterisk realtime)
        phone = Phone.objects.create(
            extension       = options['extension'],
            name            = user.get_full_name() or user.username,
            phone_type      = options['phone_type'],
            user            = user,
            asterisk_server = server,
            context         = 'agents',
            allow_codecs    = 'opus,ulaw,alaw',
            is_active       = True,
        )

        self.stdout.write(self.style.SUCCESS(
            f'\n✓ Agent created successfully\n'
            f'  Username  : {user.username}\n'
            f'  Password  : {options["password"]}\n'
            f'  Extension : {phone.extension}\n'
            f'  SIP secret: {phone.secret}\n'
            f'  Type      : {phone.get_phone_type_display()}\n'
            f'  Server    : {server.name}\n'
        ))
        self.stdout.write(
            '  Extension is live in Asterisk realtime immediately.\n'
            '  No asterisk reload needed.\n'
        )
