# dialflow/settings/test.py
"""
Test settings — fast, isolated, no external services required.
Used automatically when DJANGO_SETTINGS_MODULE=dialflow.settings.test
"""
from .base import *  # noqa

DEBUG = True
SECRET_KEY = 'test-secret-key-not-for-production'

# Use SQLite for tests (fast, no PostgreSQL needed)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# In-memory channel layer (no Redis needed)
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

# Disable caching
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

# Celery runs tasks synchronously in tests
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Skip ARI worker in tests (TelephonyConfig.ready() checks argv[1])
# Tests are run via 'pytest' not 'runserver', so the guard in apps.py handles this.

# Override Asterisk config to dummy values
ASTERISK = {
    'ARI_HOST':     'localhost',
    'ARI_PORT':     8088,
    'ARI_USERNAME': 'test',
    'ARI_PASSWORD': 'test',
    'ARI_APP_NAME': 'dialflow_test',
    'AMI_HOST':     'localhost',
    'AMI_PORT':     5038,
    'AMI_USERNAME': 'test',
    'AMI_PASSWORD': 'test',
    'RECONNECT_DELAY': 1,
    'MAX_RECONNECTS': 1,
}

DIALER = {
    'RECORDING_PATH':       '/tmp/dialflow_test_recordings',
    'RECORDING_URL_PREFIX': '/recordings/',
    'MAX_CONCURRENT_CALLS': 10,
    'HOPPER_FILL_INTERVAL': 30,
    'ZOMBIE_TIMEOUT':       120,
    'HEARTBEAT_INTERVAL':   30,
}

WEBRTC = {
    'WS_URL':      'ws://localhost:8088/ws',
    'DOMAIN':      'localhost',
    'STUN_SERVER': 'stun:stun.l.google.com:19302',
}

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',  # Fast hashing for tests
]

# Disable session caching (use DB sessions in tests)
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
