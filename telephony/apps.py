# telephony/apps.py
"""
TelephonyConfig.ready() is called once when Django finishes loading.
We start the ARI worker thread here — no separate management command.

Guard: we only start in the main process (not in Celery workers, test
runners, or manage.py migrate). We detect the main process by checking
RUN_MAIN (set by Django's autoreloader) or by falling back to a
module-level flag that prevents double-start in non-reloader environments.
"""
import logging
import os

from django.apps import AppConfig

logger = logging.getLogger('telephony.ari_worker')

# Module-level guard — prevents double-start when AppConfig.ready()
# is called more than once (e.g. during test setup).
_ari_started = False


class TelephonyConfig(AppConfig):
    name         = 'telephony'
    verbose_name = 'Telephony'

    def ready(self):
        # Import signals so Phone → Asterisk sync is wired up
        import telephony.signals  # noqa: F401

        # ── ARI Worker auto-start ─────────────────────────────────────────────
        self._start_ari_worker_if_needed()

    def _start_ari_worker_if_needed(self):
        global _ari_started

        if _ari_started:
            return

        # Don't start in:
        #   • test runner (manage.py test)
        #   • migrate / makemigrations
        #   • shell / shell_plus
        #   • celery worker processes
        import sys
        skip_commands = {'test', 'migrate', 'makemigrations', 'shell', 'shell_plus',
                         'createsuperuser', 'collectstatic', 'check', 'inspectdb',
                         'dbshell', 'showmigrations', 'sqlmigrate'}
        argv = sys.argv
        if len(argv) > 1 and argv[1] in skip_commands:
            logger.debug(f'ARI worker skipped for command: {argv[1]}')
            return

        # In Django dev server with autoreload, ready() is called twice
        # (once in the reloader process, once in the worker).
        # RUN_MAIN is set only in the actual worker process.
        reloader_active = os.environ.get('RUN_MAIN')  # 'true' in worker process
        if 'runserver' in argv and not reloader_active:
            logger.debug('ARI worker skipped — autoreloader parent process.')
            return

        # Load ARI config from settings
        from django.conf import settings
        cfg = getattr(settings, 'ASTERISK', {})

        if not cfg.get('ARI_HOST'):
            logger.warning('ASTERISK config missing in settings. ARI worker not started.')
            return

        # Check if ARI is actually reachable (non-blocking quick check)
        # from telephony.ari_worker import start_ari_worker_thread
        # start_ari_worker_thread(cfg)
        # _ari_started = True
        # logger.info('TelephonyConfig.ready() -> ARI worker thread launched [OK]')
        logger.info('TelephonyConfig.ready() -> Standalone ARI process expected.')
