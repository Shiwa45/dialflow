# calls/apps.py
from django.apps import AppConfig


class CallsConfig(AppConfig):
    name = 'calls'
    verbose_name = 'Calls'

    def ready(self):
        # Register signal handlers that update Redis real-time counters
        # and set the is_abandoned flag on CallLog status transitions.
        import calls.signals  # noqa: F401
