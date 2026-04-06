# dialflow/settings/dev.py
import os

from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ['*']

# Show emails in console during dev
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Verbose SQL logging (set to DEBUG to see queries)
# LOGGING['loggers']['django.db.backends'] = {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False}

# Django Debug Toolbar (install separately if needed)
# INSTALLED_APPS += ['debug_toolbar']
# MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')
# INTERNAL_IPS = ['127.0.0.1']

# ─── Local Development Overrides ────────────────────────────────────────────────
if os.environ.get('DIALFLOW_USE_REDIS', '').lower() not in ('1', 'true', 'yes'):
    # Use in-memory cache/sessions when Redis isn't running locally.
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'dialflow-dev-cache',
            'TIMEOUT': 300,
        }
    }
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        }
    }
