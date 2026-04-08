# reports/urls.py
from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('',                views.report_home,       name='home'),
    path('intraday/',       views.intraday_report,   name='intraday'),
    path('agents/',         views.agent_report,      name='agents'),
    path('campaigns/',      views.campaign_report,   name='campaigns'),
    path('dispositions/',   views.disposition_report, name='dispositions'),
    path('hourly/',         views.hourly_report,      name='hourly'),
    path('cdr/',            views.cdr_report,         name='cdr'),
    path('dnc/',            views.dnc_report,         name='dnc'),
    path('leads/',          views.lead_monitor,       name='lead_monitor'),
    path('api/live/',       views.live_stats_api,     name='live_stats'),
    path('api/hopper/',     views.hopper_stats_api,   name='hopper_stats'),
]
