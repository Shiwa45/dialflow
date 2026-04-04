# calls/models.py
from django.db import models
from django.conf import settings
from core.models import TimestampedModel


class CallLog(TimestampedModel):
    """
    Permanent record of every call attempt — created when ARI originates,
    updated as the call progresses, finalised on hangup.
    """

    STATUS_INITIATED  = 'initiated'
    STATUS_RINGING    = 'ringing'
    STATUS_ANSWERED   = 'answered'
    STATUS_COMPLETED  = 'completed'
    STATUS_NO_ANSWER  = 'no_answer'
    STATUS_BUSY       = 'busy'
    STATUS_FAILED     = 'failed'
    STATUS_DROPPED    = 'dropped'   # answered but no agent available
    STATUS_ABANDONED  = 'abandoned' # agent hung up before customer answered

    STATUSES = [
        (STATUS_INITIATED,  'Initiated'),
        (STATUS_RINGING,    'Ringing'),
        (STATUS_ANSWERED,   'Answered'),
        (STATUS_COMPLETED,  'Completed'),
        (STATUS_NO_ANSWER,  'No Answer'),
        (STATUS_BUSY,       'Busy'),
        (STATUS_FAILED,     'Failed'),
        (STATUS_DROPPED,    'Dropped'),
        (STATUS_ABANDONED,  'Abandoned'),
    ]

    DIRECTION_OUTBOUND = 'outbound'
    DIRECTION_INBOUND  = 'inbound'
    DIRECTIONS = [
        (DIRECTION_OUTBOUND, 'Outbound'),
        (DIRECTION_INBOUND,  'Inbound'),
    ]

    # ── Call identity ─────────────────────────────────────────────────────────
    channel_id   = models.CharField(max_length=100, db_index=True, blank=True)
    bridge_id    = models.CharField(max_length=100, blank=True)
    direction    = models.CharField(max_length=10, choices=DIRECTIONS, default=DIRECTION_OUTBOUND)

    # ── Relationships ─────────────────────────────────────────────────────────
    campaign = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='call_logs',
    )
    lead = models.ForeignKey(
        'leads.Lead', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='call_logs',
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='call_logs',
    )
    disposition = models.ForeignKey(
        'campaigns.Disposition', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='call_logs',
    )

    # ── Call info ─────────────────────────────────────────────────────────────
    phone_number  = models.CharField(max_length=30, db_index=True)
    status        = models.CharField(max_length=20, choices=STATUSES, default=STATUS_INITIATED)

    # ── Timestamps ────────────────────────────────────────────────────────────
    started_at    = models.DateTimeField(null=True, blank=True)
    answered_at   = models.DateTimeField(null=True, blank=True)
    ended_at      = models.DateTimeField(null=True, blank=True)
    duration      = models.PositiveIntegerField(default=0, help_text='Talk duration in seconds')
    ring_duration = models.PositiveIntegerField(default=0, help_text='Ring time in seconds')

    # ── Agent notes / recording ───────────────────────────────────────────────
    agent_notes    = models.TextField(blank=True)
    recording_path = models.CharField(max_length=500, blank=True)

    # ── AMD result ────────────────────────────────────────────────────────────
    amd_result = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name = 'Call Log'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['campaign', '-started_at']),
            models.Index(fields=['agent', '-started_at']),
            models.Index(fields=['lead', '-started_at']),
            models.Index(fields=['status', '-started_at']),
            models.Index(fields=['channel_id']),
        ]

    def __str__(self):
        return f'{self.phone_number} | {self.status} | {self.started_at}'

    @property
    def duration_display(self):
        m, s = divmod(self.duration or 0, 60)
        return f'{m}:{s:02d}'

    @property
    def recording_url(self):
        if not self.recording_path:
            return None
        from django.conf import settings
        prefix = settings.DIALER.get('RECORDING_URL_PREFIX', '/recordings/')
        import os
        return f"{prefix}{os.path.basename(self.recording_path)}"
