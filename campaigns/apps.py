# campaigns/apps.py
from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    name = 'campaigns'
    verbose_name = 'Campaigns'

    def ready(self):
        import campaigns.signals  # noqa: F401
