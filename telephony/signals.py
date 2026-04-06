# telephony/signals.py
import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Phone, Carrier

logger = logging.getLogger('dialflow')


@receiver(post_save, sender=Phone)
def sync_phone_to_asterisk(sender, instance, **kwargs):
    """
    Every time a Phone is saved (created or updated), write the PJSIP
    realtime rows immediately. Asterisk reads these from the database
    via ODBC — no reload command needed.
    """
    if instance.is_active:
        try:
            instance.sync_to_asterisk()
            logger.info(f'Phone {instance.extension} synced to Asterisk realtime tables.')
        except Exception as e:
            logger.error(f'Failed to sync phone {instance.extension} to Asterisk: {e}')
    else:
        # Deactivated phone — remove from Asterisk so it can't register
        try:
            instance.remove_from_asterisk()
            logger.info(f'Phone {instance.extension} removed from Asterisk realtime (deactivated).')
        except Exception as e:
            logger.error(f'Failed to remove phone {instance.extension} from Asterisk: {e}')


@receiver(post_delete, sender=Phone)
def remove_phone_from_asterisk(sender, instance, **kwargs):
    """Remove PJSIP realtime rows when a Phone is deleted."""
    try:
        instance.remove_from_asterisk()
        logger.info(f'Phone {instance.extension} removed from Asterisk realtime (deleted).')
    except Exception as e:
        logger.error(f'Failed to remove phone {instance.extension} on delete: {e}')


# ─── Carrier signals ──────────────────────────────────────────────────────────

@receiver(post_save, sender=Carrier)
def sync_carrier_to_asterisk(sender, instance, **kwargs):
    """
    Every time a Carrier is saved, write its PJSIP trunk rows
    (ps_endpoints / ps_auths / ps_aors / ps_contacts) so Asterisk
    picks it up immediately via ODBC — no reload needed.
    """
    if instance.is_active:
        try:
            instance.sync_to_asterisk()
            logger.info(f'Carrier {instance.name} ({instance.endpoint_id}) synced to Asterisk.')
        except Exception as e:
            logger.error(f'Failed to sync carrier {instance.name} to Asterisk: {e}')
    else:
        try:
            instance.remove_from_asterisk()
            logger.info(f'Carrier {instance.name} removed from Asterisk (deactivated).')
        except Exception as e:
            logger.error(f'Failed to remove carrier {instance.name} from Asterisk: {e}')


@receiver(post_delete, sender=Carrier)
def remove_carrier_from_asterisk(sender, instance, **kwargs):
    """Remove PJSIP trunk rows when a Carrier is deleted."""
    try:
        instance.remove_from_asterisk()
        logger.info(f'Carrier {instance.name} removed from Asterisk (deleted).')
    except Exception as e:
        logger.error(f'Failed to remove carrier {instance.name} on delete: {e}')

