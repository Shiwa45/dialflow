# reports/models.py
from django.db import models
from core.models import TimestampedModel


class DailySnapshot(TimestampedModel):
    """Daily rollup of call stats per campaign — generated at midnight."""
    date          = models.DateField()
    campaign      = models.ForeignKey('campaigns.Campaign', on_delete=models.CASCADE, related_name='snapshots')
    calls_total   = models.PositiveIntegerField(default=0)
    calls_answered = models.PositiveIntegerField(default=0)
    calls_dropped  = models.PositiveIntegerField(default=0)
    calls_no_answer = models.PositiveIntegerField(default=0)
    avg_talk_time  = models.PositiveIntegerField(default=0, help_text='seconds')
    total_talk_time = models.PositiveIntegerField(default=0, help_text='seconds')
    agents_logged_in = models.PositiveIntegerField(default=0)
    abandon_rate   = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        unique_together = ('date', 'campaign')
        ordering = ['-date']
        verbose_name = 'Daily Snapshot'
