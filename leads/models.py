# leads/models.py
from django.db import models
from django.conf import settings
from core.models import TimestampedModel


class LeadBatch(TimestampedModel):
    """A reusable imported lead list that can later be assigned to campaigns."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    source_file = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lead_batches_created',
    )

    class Meta:
        verbose_name = 'Lead Batch'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return self.name


class Lead(TimestampedModel):
    """
    A contact/prospect to be dialed.
    Leads belong to one or many campaigns via the campaigns M2M.
    """
    # Personal info
    first_name    = models.CharField(max_length=100)
    last_name     = models.CharField(max_length=100, blank=True)
    email         = models.EmailField(blank=True)
    company       = models.CharField(max_length=200, blank=True)

    # Phone numbers
    primary_phone  = models.CharField(max_length=30, db_index=True)
    alt_phone_1    = models.CharField(max_length=30, blank=True)
    alt_phone_2    = models.CharField(max_length=30, blank=True)

    # Location
    address   = models.TextField(blank=True)
    city      = models.CharField(max_length=100, blank=True)
    state     = models.CharField(max_length=100, blank=True)
    zip_code  = models.CharField(max_length=20, blank=True)
    country   = models.CharField(max_length=100, blank=True, default='IN')
    timezone  = models.CharField(max_length=50, blank=True, default='Asia/Kolkata')

    # Campaign membership
    campaigns = models.ManyToManyField(
        'campaigns.Campaign', blank=True, related_name='leads',
    )
    batches = models.ManyToManyField(
        LeadBatch, blank=True, related_name='leads',
    )

    # Status
    is_active    = models.BooleanField(default=True)
    do_not_call  = models.BooleanField(default=False)
    priority     = models.SmallIntegerField(default=5, help_text='1=highest, 10=lowest')

    # Import metadata
    source       = models.CharField(max_length=100, blank=True, help_text='Import source / list name')
    external_id  = models.CharField(max_length=100, blank=True, db_index=True,
                                    help_text='ID from external CRM system')

    # Custom fields (flexible JSON for campaign-specific data)
    custom_fields = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = 'Lead'
        ordering = ['-priority', 'created_at']
        indexes = [
            models.Index(fields=['primary_phone']),
            models.Index(fields=['external_id']),
            models.Index(fields=['is_active', 'do_not_call']),
        ]

    def __str__(self):
        return f'{self.first_name} {self.last_name} ({self.primary_phone})'

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    def get_all_phones(self):
        return [p for p in [self.primary_phone, self.alt_phone_1, self.alt_phone_2] if p]

    def mark_dnc(self, campaign=None, added_by=None, reason='Agent requested'):
        """Add to DNC and mark this lead as do_not_call."""
        from campaigns.models import DNCEntry
        self.do_not_call = True
        self.save(update_fields=['do_not_call', 'updated_at'])
        DNCEntry.objects.get_or_create(
            phone_number=self.primary_phone,
            campaign=campaign,
            defaults={'added_by': added_by, 'reason': reason},
        )


class LeadAttempt(TimestampedModel):
    """
    Records every dial attempt for a lead within a campaign.
    Used by the hopper to enforce max_attempts limits.
    """
    lead           = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='attempts')
    campaign       = models.ForeignKey('campaigns.Campaign', on_delete=models.CASCADE, related_name='lead_attempts')
    attempt_number = models.PositiveSmallIntegerField(default=1)
    phone_number   = models.CharField(max_length=30)
    call_log       = models.ForeignKey('calls.CallLog', on_delete=models.SET_NULL, null=True, blank=True)
    result         = models.CharField(max_length=50, blank=True)  # answered, no_answer, busy, etc.
    attempted_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Lead Attempt'
        ordering = ['-attempted_at']
        indexes = [
            models.Index(fields=['campaign', 'lead', '-attempt_number']),
        ]

    def __str__(self):
        return f'{self.lead} — attempt {self.attempt_number}'


class LeadNote(TimestampedModel):
    """Agent notes on a lead (visible across campaigns)."""
    lead      = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='notes')
    agent     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    campaign  = models.ForeignKey('campaigns.Campaign', on_delete=models.SET_NULL, null=True, blank=True)
    note      = models.TextField()

    class Meta:
        verbose_name = 'Lead Note'
        ordering = ['-created_at']

    def __str__(self):
        return f'Note on {self.lead} by {self.agent}'
