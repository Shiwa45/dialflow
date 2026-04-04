# dialflow/settings/dev.py
from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ['*']

# During local development Redis may not be running, so fall back to in-process cache.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'dialflow-dev-cache',
    }
}

# Show emails in console during dev
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Verbose SQL logging (set to DEBUG to see queries)
# LOGGING['loggers']['django.db.backends'] = {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False}

# Django Debug Toolbar (install separately if needed)
# INSTALLED_APPS += ['debug_toolbar']
# MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')
# INTERNAL_IPS = ['127.0.0.1']
