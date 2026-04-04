# core/api_urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

# Routers will be registered by each app's api.py (imported below)
from agents.api import router as agents_router
from campaigns.api import router as campaigns_router
from leads.api import router as leads_router

router.registry.extend(agents_router.registry)
router.registry.extend(campaigns_router.registry)
router.registry.extend(leads_router.registry)

urlpatterns = [
    path('', include(router.urls)),
]
