# core/apps.py
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
    verbose_name = 'Core'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # Import signal handlers
        import core.signals  # noqa: F401
