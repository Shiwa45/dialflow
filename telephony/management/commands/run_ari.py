from django.core.management.base import BaseCommand
from django.conf import settings
import asyncio
import logging
from telephony.ari_worker import run_ari_worker

logger = logging.getLogger('telephony.ari_worker')

class Command(BaseCommand):
    help = 'Runs the ARI Worker as a standalone process.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting ARI Worker process...'))
        
        cfg = getattr(settings, 'ASTERISK', {})
        if not cfg.get('ARI_HOST'):
            self.stdout.write(self.style.ERROR('ASTERISK config missing in settings.'))
            return

        try:
            asyncio.run(run_ari_worker(cfg))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('ARI Worker stopped by user.'))
        except Exception as e:
            logger.exception(f'ARI Worker process crashed: {e}')
            self.stdout.write(self.style.ERROR(f'ARI Worker crashed: {e}'))
