# calls/urls.py
from django.urls import path
from . import views

app_name = 'calls'

urlpatterns = [
    path('',              views.call_list,      name='list'),
    path('<int:pk>/',     views.call_detail,    name='detail'),
    path('<int:pk>/recording/', views.serve_recording, name='recording'),
    path('api/stats/',    views.call_stats_api, name='stats_api'),
]
