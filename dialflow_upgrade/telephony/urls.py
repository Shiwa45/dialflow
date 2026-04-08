# telephony/urls.py
from django.urls import path
from . import views

app_name = 'telephony'

urlpatterns = [
    # Server CRUD
    path('servers/',                   views.server_list,    name='server_list'),
    path('servers/<int:pk>/',          views.server_detail,  name='server_detail'),
    path('servers/create/',            views.server_create,  name='server_create'),
    path('servers/<int:pk>/edit/',     views.server_edit,    name='server_edit'),
    path('servers/<int:pk>/delete/',   views.server_delete,  name='server_delete'),

    # Phone CRUD
    path('phones/',                    views.phone_list,     name='phone_list'),
    path('phones/create/',             views.phone_create,   name='phone_create'),
    path('phones/<int:pk>/edit/',      views.phone_edit,     name='phone_edit'),
    path('phones/<int:pk>/delete/',    views.phone_delete,   name='phone_delete'),
    path('phones/<int:pk>/sync/',      views.phone_sync,     name='phone_sync'),

    # Carrier CRUD
    path('carriers/',                  views.carrier_list,   name='carrier_list'),
    path('carriers/create/',           views.carrier_create, name='carrier_create'),
    path('carriers/<int:pk>/edit/',    views.carrier_edit,   name='carrier_edit'),
    path('carriers/<int:pk>/delete/',  views.carrier_delete, name='carrier_delete'),
    path('carriers/<int:pk>/sync/',    views.carrier_sync,   name='carrier_sync'),

    # Status API
    path('api/status/',                views.ari_status,     name='ari_status'),
]
