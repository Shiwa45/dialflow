# agents/models_patch.py
# ─────────────────────────────────────────────────────────────────────────────
# ADD these models to agents/models.py (append after existing models)
# Also ADD the AgentLoginLog import and signal wiring.
# ─────────────────────────────────────────────────────────────────────────────

"""
NEW MODEL: AgentLoginLog
Tracks agent login/logout sessions. Login time is calculated only for the
current date — sessions spanning midnight are split automatically.

NEW MODEL: PauseCode
Named break/pause reasons for better reporting.
"""

# ── Paste into agents/models.py ──────────────────────────────────────────────

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
    login_time is calculated for the CURRENT DATE only.
    If a session spans midnight, the duration is split per day.
    """
    user          = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='login_logs'
    )
    login_at      = models.DateTimeField(default=timezone.now)
    logout_at     = models.DateTimeField(null=True, blank=True)
    ip_address    = models.GenericIPAddressField(null=True, blank=True)
    user_agent    = models.TextField(blank=True)

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
        """
        Return seconds of this session that fall within `target_date`.
        Handles sessions that span midnight correctly.
        """
        from datetime import datetime, time as dtime
        import pytz

        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(
            datetime.combine(target_date, dtime.min), tz
        )
        day_end = timezone.make_aware(
            datetime.combine(target_date, dtime.max), tz
        )

        effective_start = max(self.login_at, day_start)
        effective_end = min(self.logout_at or timezone.now(), day_end)

        if effective_end <= effective_start:
            return 0
        return int((effective_end - effective_start).total_seconds())

    @classmethod
    def get_today_login_time(cls, user):
        """Total login seconds for user on the CURRENT DATE only."""
        today = timezone.now().date()
        from datetime import datetime, time as dtime
        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime.combine(today, dtime.min), tz)

        sessions = cls.objects.filter(
            user=user,
            login_at__date__lte=today,
        ).filter(
            models.Q(logout_at__isnull=True) |
            models.Q(logout_at__date__gte=today)
        )

        total = 0
        for session in sessions:
            total += session.duration_for_date(today)
        return total

    @classmethod
    def get_today_login_time_display(cls, user):
        s = cls.get_today_login_time(user)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f'{h}h {m}m'


# ── Also update AgentStatus model — add pause_code field ──────────────────────
# Add this field to AgentStatus:
#   pause_code = models.ForeignKey(
#       'PauseCode', on_delete=models.SET_NULL, null=True, blank=True
#   )
#   login_log = models.ForeignKey(
#       'AgentLoginLog', on_delete=models.SET_NULL, null=True, blank=True,
#       related_name='agent_statuses',
#   )
