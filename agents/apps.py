# agents/apps.py
from django.apps import AppConfig


class AgentsConfig(AppConfig):
    name = 'agents'
    verbose_name = 'Agents'

    def ready(self):
        import agents.signals  # noqa: F401
