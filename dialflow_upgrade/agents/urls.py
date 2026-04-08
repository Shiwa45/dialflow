# agents/urls.py
from django.urls import path
from . import views

app_name = 'agents'

urlpatterns = [
    # Dashboards
    path('',                                    views.dashboard,            name='dashboard'),
    path('supervisor/',                         views.supervisor_dashboard, name='supervisor'),

    # Agent call history (HTML)
    path('calls/',                              views.call_history_page,    name='call_history_page'),

    # Agent JSON API
    path('api/status/',                         views.get_my_status,        name='status'),
    path('api/set-status/',                     views.set_status,           name='set_status'),
    path('api/heartbeat/',                      views.submit_heartbeat,     name='heartbeat'),
    path('api/lead-info/',                      views.get_lead_info,        name='lead_info'),
    path('api/dispositions/',                   views.get_dispositions,     name='dispositions'),
    path('api/call-history/',                   views.call_history,         name='call_history'),
    path('api/pause-codes/',                    views.get_pause_codes,      name='pause_codes'),

    # Supervisor actions
    path('api/force-logout/<int:agent_id>/',    views.force_logout_agent,   name='force_logout'),
    path('api/monitor/',                        views.supervisor_monitor,   name='monitor'),
]
