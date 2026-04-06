# leads/urls.py
from django.urls import path
from . import views

app_name = 'leads'

urlpatterns = [
    path('',                   views.lead_list,      name='list'),
    path('detail/<int:pk>/',   views.lead_detail,    name='detail'),
    path('import/',            views.import_page,    name='import_page'),
    path('import/upload/',     views.import_mapped,  name='import_mapped'),
    path('import/legacy/',     views.lead_import,    name='import'),
    path('export/',            views.lead_export,    name='export'),
    path('recycle/',           views.recycle_page,   name='recycle'),
    path('recycle/submit/',    views.recycle_leads,  name='recycle'),
    path('api/add-note/',      views.lead_add_note,  name='add_note'),
]
