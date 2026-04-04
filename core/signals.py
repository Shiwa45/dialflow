# core/signals.py
# System-wide Django signals.
# App-specific signals live in each app's signals.py.

import logging
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

logger = logging.getLogger('dialflow')


@receiver(user_logged_in)
def on_user_login(sender, request, user, **kwargs):
    logger.info(f'User logged in: {user.username} (id={user.pk})')


@receiver(user_logged_out)
def on_user_logout(sender, request, user, **kwargs):
    if user:
        logger.info(f'User logged out: {user.username} (id={user.pk})')


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request, **kwargs):
    logger.warning(f'Login failed for username: {credentials.get("username", "unknown")}')
