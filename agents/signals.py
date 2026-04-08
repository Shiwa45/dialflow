# agents/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='agents.CallDisposition')
def on_disposition_saved(sender, instance, created, **kwargs):
    """After a disposition is saved, push stat refresh to supervisor."""
    if created:
        from core.ws_utils import broadcast_supervisor
        broadcast_supervisor({
            'type':        'disposition_recorded',
            'agent_id':    instance.agent_id,
            'campaign_id': instance.campaign_id,
            'disposition': instance.disposition.name,
            'auto':        getattr(instance, 'auto_applied', False),
        })
