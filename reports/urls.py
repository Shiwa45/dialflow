# reports/urls.py
from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('',              views.report_home,       name='home'),
    path('intraday/',     views.intraday_report,   name='intraday'),
    path('agents/',       views.agent_report,      name='agents'),
    path('campaigns/',    views.campaign_report,   name='campaigns'),
    path('leads/',        views.lead_monitor,      name='lead_monitor'),
    path('api/live/',     views.live_stats_api,    name='live_stats'),
    path('api/hopper/',   views.hopper_stats_api,  name='hopper_stats'),
]
