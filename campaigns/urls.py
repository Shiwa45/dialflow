# campaigns/urls.py
from django.urls import path
from . import views

app_name = 'campaigns'

urlpatterns = [
    # Campaign CRUD
    path('',                            views.campaign_list,      name='list'),
    path('create/',                     views.campaign_create,    name='create'),
    path('<int:pk>/',                   views.campaign_detail,    name='detail'),
    path('<int:pk>/edit/',              views.campaign_edit,      name='edit'),

    # Campaign controls
    path('<int:pk>/control/',           views.campaign_control,   name='control'),
    path('<int:pk>/stats/',             views.campaign_stats_api, name='stats_api'),

    # Agent assignment
    path('<int:pk>/agents/',            views.agent_assignment,   name='agents'),
    path('<int:pk>/agents/assign/',     views.agent_assign,       name='agent_assign'),
    path('<int:pk>/agents/unassign/',   views.agent_unassign,     name='agent_unassign'),

    # Hopper
    path('<int:pk>/hopper/refill/',     views.hopper_refill,      name='hopper_refill'),

    # Dispositions
    path('dispositions/',               views.disposition_list,   name='dispositions'),
    path('dispositions/create/',        views.disposition_create, name='disposition_create'),
    path('dispositions/<int:pk>/delete/', views.disposition_delete, name='disposition_delete'),

    # DNC
    path('dnc/',                        views.dnc_list,           name='dnc_list'),
    path('dnc/add/',                    views.dnc_add,            name='dnc_add'),
    path('dnc/import/',                 views.dnc_import,         name='dnc_import'),

    # Callbacks
    path('callbacks/',                  views.callback_list,      name='callbacks'),

    # Monitoring APIs
    path('api/hopper-stats/',           views.hopper_stats_api_all, name='hopper_stats_api'),
]
