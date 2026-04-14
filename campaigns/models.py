# campaigns/models.py
import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone
from core.models import TimestampedModel


# ─── Disposition ──────────────────────────────────────────────────────────────

class Disposition(TimestampedModel):
    """
    Dynamic disposition — fully configurable per campaign.
    No hardcoded choices. Admin creates/edits via UI.
    """
    CATEGORY_SALE          = 'sale'
    CATEGORY_NO_ANSWER     = 'no_answer'
    CATEGORY_BUSY          = 'busy'
    CATEGORY_CALLBACK      = 'callback'
    CATEGORY_DNC           = 'dnc'
    CATEGORY_NOT_INTERESTED = 'not_interested'
    CATEGORY_OTHER         = 'other'

    CATEGORY_CHOICES = [
        (CATEGORY_SALE,           'Sale / Converted'),
        (CATEGORY_NO_ANSWER,      'No Answer'),
        (CATEGORY_BUSY,           'Busy'),
        (CATEGORY_CALLBACK,       'Callback'),
        (CATEGORY_DNC,            'Do Not Call'),
        (CATEGORY_NOT_INTERESTED, 'Not Interested'),
        (CATEGORY_OTHER,          'Other'),
    ]

    OUTCOME_COMPLETE = 'complete'
    OUTCOME_RECYCLE  = 'recycle'
    OUTCOME_DNC      = 'dnc'
    OUTCOME_CALLBACK = 'callback'

    OUTCOME_CHOICES = [
        (OUTCOME_COMPLETE, 'Mark complete — no further attempts'),
        (OUTCOME_RECYCLE,  'Recycle — attempt again later'),
        (OUTCOME_DNC,      'Add to DNC list'),
        (OUTCOME_CALLBACK, 'Schedule callback'),
    ]

    name         = models.CharField(max_length=100)
    category     = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER)
    outcome      = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default=OUTCOME_RECYCLE)
    color        = models.CharField(max_length=7, default='#6B7280', help_text='Hex color for UI badge')
    hotkey       = models.CharField(max_length=1, blank=True, help_text='Single keyboard shortcut key')
    is_system    = models.BooleanField(default=False, help_text='System dispositions cannot be deleted')
    is_active    = models.BooleanField(default=True)
    sort_order   = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Disposition'
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


# ─── Campaign ─────────────────────────────────────────────────────────────────

class Campaign(TimestampedModel):
    """
    Autodialer campaign. Every dial setting is stored here.
    Changes take effect immediately — no restart needed.
    """

    DIAL_MODE_PREVIEW     = 'preview'
    DIAL_MODE_PROGRESSIVE = 'progressive'
    DIAL_MODE_PREDICTIVE  = 'predictive'
    DIAL_MODES = [
        (DIAL_MODE_PREVIEW,     'Preview — agent dials manually'),
        (DIAL_MODE_PROGRESSIVE, 'Progressive — one call per available agent'),
        (DIAL_MODE_PREDICTIVE,  'Predictive — algorithm dials ahead'),
    ]

    STATUS_ACTIVE   = 'active'
    STATUS_PAUSED   = 'paused'
    STATUS_STOPPED  = 'stopped'
    STATUS_DRAFT    = 'draft'
    STATUSES = [
        (STATUS_ACTIVE,  'Active'),
        (STATUS_PAUSED,  'Paused'),
        (STATUS_STOPPED, 'Stopped'),
        (STATUS_DRAFT,   'Draft'),
    ]

    LEAD_ORDER_CHOICES = [
        ('sequential', 'Sequential'),
        ('random',     'Random'),
        ('priority',   'Priority (highest first)'),
        ('newest',     'Newest leads first'),
        ('oldest',     'Oldest leads first'),
    ]

    # Identity
    name        = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    status      = models.CharField(max_length=20, choices=STATUSES, default=STATUS_DRAFT)
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='campaigns_created',
    )

    # Telephony
    asterisk_server = models.ForeignKey(
        'telephony.AsteriskServer', on_delete=models.PROTECT, related_name='campaigns',
    )
    carrier = models.ForeignKey(
        'telephony.Carrier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='campaigns',
    )
    caller_id   = models.CharField(max_length=50, blank=True, help_text='Override carrier caller ID')
    dial_prefix = models.CharField(max_length=20, blank=True, help_text='Prefix added before every number')

    # Dial settings
    dial_mode       = models.CharField(max_length=20, choices=DIAL_MODES, default=DIAL_MODE_PREDICTIVE)
    dial_ratio      = models.DecimalField(max_digits=4, decimal_places=2, default=1.5,
                                          help_text='Calls per available agent (predictive)')
    max_dial_ratio  = models.DecimalField(max_digits=4, decimal_places=2, default=3.0)
    min_dial_ratio  = models.DecimalField(max_digits=4, decimal_places=2, default=1.0)
    dial_timeout    = models.PositiveIntegerField(default=30, help_text='Ring timeout in seconds')
    abandon_rate    = models.DecimalField(max_digits=5, decimal_places=2, default=3.0,
                                          help_text='Target max abandon rate %')
    # CPS throttle — caps the number of ARI originations per second.
    # 0 = unlimited (not recommended in production). Default 10 is safe for
    # most Asterisk deployments; raise for high-volume trunks.
    cps_limit       = models.PositiveSmallIntegerField(
        default=10,
        help_text='Maximum call originations per second (0 = unlimited)',
    )

    # Hopper
    hopper_level = models.PositiveIntegerField(default=100,
                                               help_text='Target leads to keep in hopper')
    hopper_size  = models.PositiveIntegerField(default=500,
                                               help_text='Maximum hopper capacity')
    lead_order   = models.CharField(max_length=20, choices=LEAD_ORDER_CHOICES, default='sequential')

    # AMD (Answering Machine Detection)
    amd_enabled  = models.BooleanField(default=False)
    amd_action   = models.CharField(
        max_length=20,
        choices=[('hangup','Hang up'),('message','Leave message'),('agent','Send to agent anyway')],
        default='hangup',
    )

    # Recording
    enable_recording   = models.BooleanField(default=True)
    recording_mode     = models.CharField(
        max_length=20,
        choices=[('all','All calls'),('answered','Answered only')],
        default='answered',
    )

    # Call hours
    call_hour_start = models.TimeField(default='09:00')
    call_hour_end   = models.TimeField(default='21:00')
    respect_timezone = models.BooleanField(default=True,
                                           help_text='Use lead timezone for call hours')

    # Wrapup
    wrapup_timeout       = models.PositiveIntegerField(default=60,
                                                       help_text='Seconds agent stays in wrapup')
    auto_wrapup_enabled  = models.BooleanField(default=False)
    auto_wrapup_timeout  = models.PositiveIntegerField(default=120)
    auto_wrapup_disposition = models.ForeignKey(
        Disposition, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='auto_wrapup_campaigns',
        help_text='Disposition applied on auto-wrapup timeout',
    )

    # Attempt limits
    max_attempts        = models.PositiveIntegerField(default=3)
    retry_delay_minutes = models.PositiveIntegerField(default=60)

    # Dispositions available to agents for this campaign
    dispositions = models.ManyToManyField(
        Disposition, blank=True,
        related_name='campaigns',
        help_text='Dispositions agents can choose for this campaign',
    )

    # DNC
    use_system_dnc   = models.BooleanField(default=True)
    use_campaign_dnc = models.BooleanField(default=True)

    # Stats (updated by Celery task — read by supervisor WS)
    stat_calls_today    = models.PositiveIntegerField(default=0)
    stat_answered_today = models.PositiveIntegerField(default=0)
    stat_agents_active  = models.PositiveIntegerField(default=0)
    stat_abandon_rate   = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        verbose_name = 'Campaign'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} [{self.get_status_display()}]'

    @property
    def is_active(self):
        return self.status == self.STATUS_ACTIVE

    def start(self):
        self.status = self.STATUS_ACTIVE
        self.save(update_fields=['status', 'updated_at'])

    def pause(self):
        self.status = self.STATUS_PAUSED
        self.save(update_fields=['status', 'updated_at'])

    def stop(self):
        self.status = self.STATUS_STOPPED
        self.save(update_fields=['status', 'updated_at'])

    def get_active_agents_count(self):
        from agents.models import AgentStatus
        assigned = self.agents.values_list('agent_id', flat=True)
        return AgentStatus.objects.filter(
            user_id__in=assigned,
            status__in=['ready', 'ringing', 'on_call', 'wrapup'],
        ).count()

    def get_hopper_count(self):
        return self.hopper_entries.filter(status='queued').count()


# ─── Campaign ↔ Agent assignment ─────────────────────────────────────────────

class CampaignAgent(TimestampedModel):
    """Many-to-many through model: which agents are assigned to a campaign."""
    campaign   = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='agents')
    agent      = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='campaign_assignments',
    )
    is_active  = models.BooleanField(default=True)
    joined_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('campaign', 'agent')
        verbose_name = 'Campaign Agent'

    def __str__(self):
        return f'{self.agent.username} → {self.campaign.name}'


# ─── Hopper ───────────────────────────────────────────────────────────────────

class HopperEntry(TimestampedModel):
    """
    A single lead slot in the dial queue (hopper).
    Redis caches the queue; this table is the durable backing store.
    """
    STATUS_QUEUED    = 'queued'
    STATUS_DIALING   = 'dialing'
    STATUS_ANSWERED  = 'answered'
    STATUS_COMPLETED = 'completed'
    STATUS_DROPPED   = 'dropped'
    STATUS_FAILED    = 'failed'
    STATUS_EXPIRED   = 'expired'
    STATUSES = [
        (STATUS_QUEUED,    'Queued'),
        (STATUS_DIALING,   'Dialing'),
        (STATUS_ANSWERED,  'Answered'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_DROPPED,   'Dropped — no agent'),
        (STATUS_FAILED,    'Failed — no answer / busy'),
        (STATUS_EXPIRED,   'Expired'),
    ]

    campaign        = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='hopper_entries')
    lead            = models.ForeignKey('leads.Lead', on_delete=models.CASCADE, related_name='hopper_entries')
    phone_number    = models.CharField(max_length=30)
    status          = models.CharField(max_length=20, choices=STATUSES, default=STATUS_QUEUED)
    priority        = models.SmallIntegerField(default=5)  # 1=highest, 10=lowest
    attempt_number  = models.PositiveSmallIntegerField(default=1)
    channel_id      = models.CharField(max_length=100, blank=True)
    queued_at       = models.DateTimeField(auto_now_add=True)
    dialed_at       = models.DateTimeField(null=True, blank=True)
    completed_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Hopper Entry'
        indexes = [
            models.Index(fields=['campaign', 'status', '-priority', 'queued_at']),
            models.Index(fields=['status', 'queued_at']),
        ]
        ordering = ['-priority', 'queued_at']

    def __str__(self):
        return f'{self.campaign.name} | {self.phone_number} | {self.status}'

    def mark_dialing(self, channel_id=''):
        self.status = self.STATUS_DIALING
        self.channel_id = channel_id
        self.dialed_at = timezone.now()
        self.save(update_fields=['status', 'channel_id', 'dialed_at', 'updated_at'])

    def mark_completed(self):
        self.status = self.STATUS_COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_dropped(self):
        self.status = self.STATUS_DROPPED
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_failed(self):
        self.status = self.STATUS_FAILED
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])


# ─── DNC ─────────────────────────────────────────────────────────────────────

class DNCEntry(TimestampedModel):
    """Do Not Call list. System-wide and per-campaign entries."""
    phone_number = models.CharField(max_length=30, db_index=True)
    campaign     = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name='dnc_entries',
        null=True, blank=True,
        help_text='Null = system-wide DNC',
    )
    added_by     = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    reason       = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'DNC Entry'
        unique_together = ('phone_number', 'campaign')
        indexes = [models.Index(fields=['phone_number'])]

    def __str__(self):
        scope = self.campaign.name if self.campaign else 'SYSTEM'
        return f'{self.phone_number} [{scope}]'

    @classmethod
    def is_dnc(cls, phone_number: str, campaign_id: int = None) -> bool:
        """Check if a number is on the DNC list (system-wide or campaign-specific)."""
        qs = cls.objects.filter(phone_number=phone_number)
        if campaign_id:
            return qs.filter(
                models.Q(campaign__isnull=True) | models.Q(campaign_id=campaign_id)
            ).exists()
        return qs.filter(campaign__isnull=True).exists()
