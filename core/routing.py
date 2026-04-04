# core/routing.py
from django.urls import re_path
from agents import consumers as agent_consumers
from campaigns import consumers as campaign_consumers

websocket_urlpatterns = [
    # Agent dashboard — one socket per agent session
    re_path(r'^ws/agent/$', agent_consumers.AgentConsumer.as_asgi()),

    # Supervisor monitor — real-time overview of all agents + campaigns
    re_path(r'^ws/supervisor/$', campaign_consumers.SupervisorConsumer.as_asgi()),

    # Per-campaign stats stream
    re_path(r'^ws/campaign/(?P<campaign_id>\d+)/$', campaign_consumers.CampaignConsumer.as_asgi()),
]
