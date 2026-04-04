# leads/urls.py
from django.urls import path
from . import views

app_name = 'leads'

urlpatterns = [
    path('',               views.lead_list,    name='list'),
    path('<int:pk>/',      views.lead_detail,  name='detail'),
    path('import/',        views.lead_import,  name='import'),
    path('export/',        views.lead_export,  name='export'),
    path('<int:pk>/note/', views.lead_add_note, name='add_note'),
]
