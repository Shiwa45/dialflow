# users/models.py
import pytz
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

TIMEZONE_CHOICES = [(tz, tz) for tz in pytz.common_timezones]


class User(AbstractUser):
    """
    Extended user model.

    Roles
    -----
    admin       — full access, manages everything
    supervisor  — manages campaigns, monitors agents
    agent       — makes calls, disposes leads
    """
    ROLE_ADMIN      = 'admin'
    ROLE_SUPERVISOR = 'supervisor'
    ROLE_AGENT      = 'agent'
    ROLE_CHOICES = [
        (ROLE_ADMIN,      'Admin'),
        (ROLE_SUPERVISOR, 'Supervisor'),
        (ROLE_AGENT,      'Agent'),
    ]

    role          = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_AGENT)
    phone_number  = models.CharField(max_length=20, blank=True)
    timezone      = models.CharField(max_length=50, choices=TIMEZONE_CHOICES, default='Asia/Kolkata')
    avatar   = models.ImageField(upload_to='avatars/', blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # Soft-delete / deactivation note
    deactivated_at   = models.DateTimeField(null=True, blank=True)
    deactivated_note = models.TextField(blank=True)

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['username']

    def __str__(self):
        return f'{self.get_full_name() or self.username} ({self.role})'

    # ── Role helpers ──────────────────────────────────────────────────────────
    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN or self.is_superuser

    @property
    def is_supervisor(self):
        return self.role in (self.ROLE_SUPERVISOR, self.ROLE_ADMIN) or self.is_superuser

    @property
    def is_agent(self):
        return self.role == self.ROLE_AGENT

    def deactivate(self, note=''):
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.deactivated_note = note
        self.save(update_fields=['is_active', 'deactivated_at', 'deactivated_note'])
