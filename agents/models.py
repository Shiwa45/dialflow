# agents/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone
from core.models import TimestampedModel


class AgentStatus(TimestampedModel):
    """
    Single row per agent. The authoritative source of agent state.
    Everything — dashboard, supervisor panel, predictive dialer — reads from here.
    No virtual JS state. No polling. WS broadcasts whenever this row changes.
    """

    STATUS_OFFLINE  = 'offline'
    STATUS_READY    = 'ready'
    STATUS_ON_CALL  = 'on_call'
    STATUS_WRAPUP   = 'wrapup'
    STATUS_BREAK    = 'break'
    STATUS_TRAINING = 'training'

    STATUS_CHOICES = [
        (STATUS_OFFLINE,  'Offline'),
        (STATUS_READY,    'Ready'),
        (STATUS_ON_CALL,  'On Call'),
        (STATUS_WRAPUP,   'Wrap-up'),
        (STATUS_BREAK,    'Break'),
        (STATUS_TRAINING, 'Training'),
    ]

    # ── Core ──────────────────────────────────────────────────────────────────
    user   = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_status'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OFFLINE)
    status_changed_at = models.DateTimeField(default=timezone.now)

    # ── Active call tracking ──────────────────────────────────────────────────
    active_campaign    = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='active_agents',
    )
    active_channel_id  = models.CharField(max_length=100, blank=True)
    active_lead_id     = models.IntegerField(null=True, blank=True)
    active_call_log_id = models.IntegerField(null=True, blank=True)
    call_started_at    = models.DateTimeField(null=True, blank=True)

    # ── Wrapup tracking ───────────────────────────────────────────────────────
    wrapup_started_at  = models.DateTimeField(null=True, blank=True)
    wrapup_call_log_id = models.IntegerField(null=True, blank=True)

    # ── Heartbeat (zombie detection) ──────────────────────────────────────────
    last_heartbeat     = models.DateTimeField(null=True, blank=True)

    # ── Session stats (today) — reset at midnight ─────────────────────────────
    calls_today        = models.PositiveIntegerField(default=0)
    talk_time_today    = models.PositiveIntegerField(default=0, help_text='seconds')
    break_time_today   = models.PositiveIntegerField(default=0, help_text='seconds')

    class Meta:
        verbose_name = 'Agent Status'
        verbose_name_plural = 'Agent Statuses'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['active_campaign', 'status']),
        ]

    def __str__(self):
        return f'{self.user.username} → {self.status}'

    # ── Status transition helpers ─────────────────────────────────────────────

    def set_status(self, new_status: str, **extra_fields):
        """
        Change agent status in DB and broadcast to WS.
        Always use this method — never update status directly.
        """
        old_status = self.status
        self.status           = new_status
        self.status_changed_at = timezone.now()

        for key, val in extra_fields.items():
            setattr(self, key, val)

        fields = ['status', 'status_changed_at'] + list(extra_fields.keys())
        self.save(update_fields=fields + ['updated_at'])

        # WS broadcast — agent sees their own status update
        self._broadcast_status(old_status, new_status)

        # Supervisor broadcast
        self._broadcast_supervisor()

    def go_ready(self, campaign_id=None):
        self.set_status(
            self.STATUS_READY,
            active_campaign_id=campaign_id or self.active_campaign_id,
            active_channel_id='',
            active_lead_id=None,
            active_call_log_id=None,
            call_started_at=None,
            wrapup_started_at=None,
            wrapup_call_log_id=None,
        )

    def go_on_call(self, channel_id: str, lead_id: int, call_log_id: int):
        self.set_status(
            self.STATUS_ON_CALL,
            active_channel_id=channel_id,
            active_lead_id=lead_id,
            active_call_log_id=call_log_id,
            call_started_at=timezone.now(),
            wrapup_started_at=None,
        )

    def go_wrapup(self, call_log_id: int):
        """Enter wrapup state — disposition modal will open on agent's screen."""
        self.set_status(
            self.STATUS_WRAPUP,
            wrapup_started_at=timezone.now(),
            wrapup_call_log_id=call_log_id,
            active_channel_id='',
            active_lead_id=None,
            active_call_log_id=None,
            call_started_at=None,
        )

    def go_break(self):
        self.set_status(self.STATUS_BREAK)

    def go_offline(self):
        self.set_status(
            self.STATUS_OFFLINE,
            active_campaign_id=None,
            active_channel_id='',
            active_lead_id=None,
            active_call_log_id=None,
            call_started_at=None,
            wrapup_started_at=None,
        )

    def update_heartbeat(self):
        self.last_heartbeat = timezone.now()
        self.save(update_fields=['last_heartbeat'])

    # ── Wrapup helpers ────────────────────────────────────────────────────────

    @property
    def wrapup_elapsed_seconds(self) -> int:
        if self.status != self.STATUS_WRAPUP or not self.wrapup_started_at:
            return 0
        return int((timezone.now() - self.wrapup_started_at).total_seconds())

    def get_wrapup_seconds_remaining(self) -> int:
        """Seconds until auto-wrapup fires. -1 if not in wrapup / auto disabled."""
        if self.status != self.STATUS_WRAPUP:
            return -1
        campaign = self.active_campaign
        if not campaign or not campaign.auto_wrapup_enabled:
            return -1
        timeout = campaign.auto_wrapup_timeout
        elapsed = self.wrapup_elapsed_seconds
        return max(0, timeout - elapsed)

    # ── Private broadcast helpers ─────────────────────────────────────────────

    def _broadcast_status(self, old_status: str, new_status: str):
        from core.ws_utils import send_to_agent
        send_to_agent(self.user_id, {
            'type':    'status_changed',
            'status':  new_status,
            'display': self.get_status_display(),
            'since':   self.status_changed_at.isoformat(),
        })

    def _broadcast_supervisor(self):
        from core.ws_utils import broadcast_supervisor
        broadcast_supervisor({
            'type':         'agent_status_changed',
            'agent_id':     self.user_id,
            'username':     self.user.username,
            'full_name':    self.user.get_full_name(),
            'status':       self.status,
            'display':      self.get_status_display(),
            'campaign_id':  self.active_campaign_id,
            'since':        self.status_changed_at.isoformat(),
        })


class CallDisposition(TimestampedModel):
    """
    Records the agent's disposition choice after each call.
    One row per call — the permanent call outcome record.
    """
    call_log    = models.OneToOneField(
        'calls.CallLog', on_delete=models.CASCADE, related_name='disposition_record'
    )
    agent       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='dispositions_made'
    )
    campaign    = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL, null=True,
    )
    lead        = models.ForeignKey(
        'leads.Lead', on_delete=models.SET_NULL, null=True, related_name='dispositions'
    )
    disposition = models.ForeignKey(
        'campaigns.Disposition', on_delete=models.PROTECT
    )
    notes       = models.TextField(blank=True)
    callback_at = models.DateTimeField(null=True, blank=True,
                                       help_text='Scheduled callback time (if disposition = callback)')
    auto_applied = models.BooleanField(default=False,
                                       help_text='True if applied by auto-wrapup timeout')

    class Meta:
        verbose_name = 'Call Disposition'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.agent} → {self.disposition} on {self.call_log}'
