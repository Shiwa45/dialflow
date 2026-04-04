# telephony/urls.py
from django.urls import path
from . import views

app_name = 'telephony'

urlpatterns = [
    path('servers/',          views.server_list,   name='server_list'),
    path('servers/<int:pk>/', views.server_detail, name='server_detail'),
    path('phones/',           views.phone_list,    name='phone_list'),
    path('carriers/',         views.carrier_list,  name='carrier_list'),
    path('api/status/',       views.ari_status,    name='ari_status'),
]
