# core/models.py
import uuid
from django.db import models


class TimestampedModel(models.Model):
    """Abstract base model with created_at / updated_at timestamps."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Abstract base model with UUID primary key."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimestampedUUIDModel(UUIDModel, TimestampedModel):
    """Combined UUID pk + timestamps."""
    class Meta:
        abstract = True
