# agents/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone
from core.models import TimestampedModel


class PauseCode(TimestampedModel):
    """Named pause/break reasons (e.g., Lunch, Bio Break, Meeting)."""
    name       = models.CharField(max_length=100, unique=True)
    code       = models.CharField(max_length=20, unique=True)
    is_active  = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Pause Code'

    def __str__(self):
        return f'{self.code} — {self.name}'


class AgentLoginLog(TimestampedModel):
    """
    Tracks each agent login session.
    Login time is calculated for the CURRENT DATE only.
    """
    user       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='login_logs'
    )
    login_at   = models.DateTimeField(default=timezone.now)
    logout_at  = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        ordering = ['-login_at']
        verbose_name = 'Agent Login Log'
        indexes = [
            models.Index(fields=['user', '-login_at']),
            models.Index(fields=['user', 'login_at']),
        ]

    def __str__(self):
        return f'{self.user.username} — {self.login_at:%Y-%m-%d %H:%M}'

    @property
    def duration_seconds(self):
        end = self.logout_at or timezone.now()
        return max(0, int((end - self.login_at).total_seconds()))

    @property
    def duration_display(self):
        s = self.duration_seconds
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f'{h}h {m}m {sec}s'

    def duration_for_date(self, target_date):
        """Return seconds of this session that fall within target_date."""
        from datetime import datetime, time as dtime
        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime.combine(target_date, dtime.min), tz)
        day_end = timezone.make_aware(datetime.combine(target_date, dtime.max), tz)
        effective_start = max(self.login_at, day_start)
        effective_end = min(self.logout_at or timezone.now(), day_end)
        if effective_end <= effective_start:
            return 0
        return int((effective_end - effective_start).total_seconds())

    @classmethod
    def get_today_login_time(cls, user):
        """Total login seconds for user on the CURRENT DATE only."""
        today = timezone.now().date()
        sessions = cls.objects.filter(
            user=user, login_at__date__lte=today,
        ).filter(
            models.Q(logout_at__isnull=True) | models.Q(logout_at__date__gte=today)
        )
        return sum(s.duration_for_date(today) for s in sessions)

    @classmethod
    def get_today_login_time_display(cls, user):
        s = cls.get_today_login_time(user)
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        return f'{h}h {m}m'


class AgentStatus(TimestampedModel):
    """
    Single row per agent. The authoritative source of agent state.
    Everything — dashboard, supervisor panel, predictive dialer — reads from here.
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

    user   = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_status'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OFFLINE)
    status_changed_at = models.DateTimeField(default=timezone.now)

    active_campaign    = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='active_agents',
    )
    active_channel_id  = models.CharField(max_length=100, null=True, blank=True)
    active_lead_id     = models.IntegerField(null=True, blank=True)
    active_call_log_id = models.IntegerField(null=True, blank=True)
    call_started_at    = models.DateTimeField(null=True, blank=True)

    wrapup_started_at  = models.DateTimeField(null=True, blank=True)
    wrapup_call_log_id = models.IntegerField(null=True, blank=True)

    last_heartbeat     = models.DateTimeField(null=True, blank=True)

    # Session stats (today) — reset at midnight
    calls_today        = models.PositiveIntegerField(default=0)
    talk_time_today    = models.PositiveIntegerField(default=0, help_text='seconds')
    break_time_today   = models.PositiveIntegerField(default=0, help_text='seconds')

    # NEW: Pause code and login log references
    pause_code = models.ForeignKey(
        'PauseCode', on_delete=models.SET_NULL, null=True, blank=True
    )
    login_log = models.ForeignKey(
        'AgentLoginLog', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='agent_statuses',
    )

    class Meta:
        verbose_name = 'Agent Status'
        verbose_name_plural = 'Agent Statuses'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['active_campaign', 'status']),
        ]

    def __str__(self):
        return f'{self.user.username} → {self.status}'

    def set_status(self, new_status: str, **extra_fields):
        """Change agent status in DB and broadcast to WS."""
        old_status = self.status
        self.status = new_status
        self.status_changed_at = timezone.now()

        for k, v in extra_fields.items():
            setattr(self, k, v)

        update_fields = ['status', 'status_changed_at', 'updated_at'] + list(extra_fields.keys())
        self.save(update_fields=update_fields)

        # Broadcast via WS
        from core.ws_utils import send_to_agent, broadcast_supervisor
        send_to_agent(self.user_id, {
            'type':   'status_changed',
            'status': new_status,
            'display': self.get_status_display(),
            'since':  self.status_changed_at.isoformat(),
        })
        broadcast_supervisor({
            'type':      'agent_status',
            'agent_id':  self.user_id,
            'username':  self.user.username,
            'status':    new_status,
            'campaign':  self.active_campaign.name if self.active_campaign else None,
            'since':     self.status_changed_at.isoformat(),
        })

    def go_ready(self):
        self.set_status('ready', pause_code=None)

    def go_break(self, pause_code=None):
        self.set_status('break', pause_code=pause_code)

    def go_training(self):
        self.set_status('training', pause_code=None)

    def go_offline(self):
        self.set_status('offline',
            active_channel_id=None, active_lead_id=None,
            active_call_log_id=None, call_started_at=None,
            pause_code=None,
        )
        # Close login session
        if self.login_log_id:
            AgentLoginLog.objects.filter(
                id=self.login_log_id, logout_at__isnull=True
            ).update(logout_at=timezone.now())
            self.login_log = None
            self.save(update_fields=['login_log'])

    @property
    def wrapup_elapsed_seconds(self):
        if self.status != 'wrapup' or not self.wrapup_started_at:
            return 0
        return int((timezone.now() - self.wrapup_started_at).total_seconds())

    def get_wrapup_seconds_remaining(self):
        if self.status != 'wrapup':
            return -1
        if not self.active_campaign:
            return -1
        timeout = self.active_campaign.wrapup_timeout
        elapsed = self.wrapup_elapsed_seconds
        return max(0, timeout - elapsed)

    @property
    def today_login_time_display(self):
        return AgentLoginLog.get_today_login_time_display(self.user)


class CallDisposition(TimestampedModel):
    """Records the disposition an agent selected after a call."""
    call_log    = models.OneToOneField(
        'calls.CallLog', on_delete=models.CASCADE, related_name='disposition_record'
    )
    agent       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    campaign    = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.SET_NULL, null=True, blank=True
    )
    lead        = models.ForeignKey(
        'leads.Lead', on_delete=models.SET_NULL, null=True, blank=True
    )
    disposition = models.ForeignKey(
        'campaigns.Disposition', on_delete=models.SET_NULL, null=True
    )
    notes       = models.TextField(blank=True)
    callback_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Call Disposition'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.call_log} → {self.disposition}'
