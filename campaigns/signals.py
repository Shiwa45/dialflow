# campaigns/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Campaign


@receiver(post_save, sender=Campaign)
def on_campaign_save(sender, instance, **kwargs):
    """Broadcast campaign status change to all supervisor WebSocket clients."""
    from core.ws_utils import broadcast_supervisor
    broadcast_supervisor({
        'type':   'campaign_updated',
        'id':     instance.id,
        'name':   instance.name,
        'status': instance.status,
        'dial_mode': instance.dial_mode,
    })
