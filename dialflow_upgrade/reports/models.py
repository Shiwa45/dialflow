# reports/models.py
from django.db import models
from django.conf import settings
from core.models import TimestampedModel


class DailySnapshot(TimestampedModel):
    """Daily rollup of call stats per campaign — generated at midnight."""
    date             = models.DateField()
    campaign         = models.ForeignKey('campaigns.Campaign', on_delete=models.CASCADE, related_name='snapshots')
    calls_total      = models.PositiveIntegerField(default=0)
    calls_answered   = models.PositiveIntegerField(default=0)
    calls_dropped    = models.PositiveIntegerField(default=0)
    calls_no_answer  = models.PositiveIntegerField(default=0)
    avg_talk_time    = models.PositiveIntegerField(default=0, help_text='seconds')
    total_talk_time  = models.PositiveIntegerField(default=0, help_text='seconds')
    agents_logged_in = models.PositiveIntegerField(default=0)
    abandon_rate     = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        unique_together = ('date', 'campaign')
        ordering = ['-date']
        verbose_name = 'Daily Snapshot'

    def __str__(self):
        return f'{self.campaign.name} — {self.date}'


class AgentDailyLog(models.Model):
    """Per-agent per-day rollup for advanced reporting."""
    date                  = models.DateField()
    agent                 = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='daily_logs'
    )
    campaign              = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    login_time            = models.PositiveIntegerField(default=0, help_text='Total login seconds')
    talk_time             = models.PositiveIntegerField(default=0, help_text='Total talk seconds')
    break_time            = models.PositiveIntegerField(default=0, help_text='Total break seconds')
    wrapup_time           = models.PositiveIntegerField(default=0, help_text='Total wrapup seconds')
    calls_dialed          = models.PositiveIntegerField(default=0)
    calls_answered        = models.PositiveIntegerField(default=0)
    calls_transferred     = models.PositiveIntegerField(default=0)
    dispositions_sale     = models.PositiveIntegerField(default=0)
    dispositions_dnc      = models.PositiveIntegerField(default=0)
    dispositions_callback = models.PositiveIntegerField(default=0)
    dispositions_other    = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('date', 'agent', 'campaign')
        ordering = ['-date']
        verbose_name = 'Agent Daily Log'

    def __str__(self):
        return f'{self.agent.username} — {self.date}'

    @property
    def login_time_display(self):
        h, rem = divmod(self.login_time, 3600)
        m, _ = divmod(rem, 60)
        return f'{h}h {m}m'

    @property
    def talk_time_display(self):
        h, rem = divmod(self.talk_time, 3600)
        m, _ = divmod(rem, 60)
        return f'{h}h {m}m'
