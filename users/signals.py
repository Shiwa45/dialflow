# users/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings

User = settings.AUTH_USER_MODEL


@receiver(post_save, sender='users.User')
def create_agent_profile(sender, instance, created, **kwargs):
    """Auto-create AgentStatus row for every new agent user."""
    if created and instance.role == 'agent':
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=instance)
