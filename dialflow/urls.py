# dialflow/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth
    path('auth/', include('users.urls', namespace='users')),

    # Dashboard (core landing)
    path('', include('core.urls', namespace='core')),

    # Feature apps
    path('campaigns/', include('campaigns.urls', namespace='campaigns')),
    path('leads/',     include('leads.urls',     namespace='leads')),
    path('agents/',    include('agents.urls',    namespace='agents')),
    path('calls/',     include('calls.urls',     namespace='calls')),
    path('telephony/', include('telephony.urls', namespace='telephony')),
    path('reports/',   include('reports.urls',   namespace='reports')),

    # Internal API (DRF)
    path('api/v1/', include('core.api_urls')),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Customize admin site
admin.site.site_header = 'DialFlow Pro Admin'
admin.site.site_title  = 'DialFlow'
admin.site.index_title = 'Control Panel'
