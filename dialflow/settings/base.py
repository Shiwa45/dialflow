# dialflow/settings/base.py
# Base settings shared across all environments

from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',

    # Third-party
    'channels',
    'rest_framework',
    'django_celery_beat',
    'django_celery_results',
    'django_extensions',

    # Local apps (order matters — core first)
    'core.apps.CoreConfig',
    'users.apps.UsersConfig',
    'telephony.apps.TelephonyConfig',
    'campaigns.apps.CampaignsConfig',
    'leads.apps.LeadsConfig',
    'agents.apps.AgentsConfig',
    'calls.apps.CallsConfig',
    'reports.apps.ReportsConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.TimezoneMiddleware',
    'core.middleware.RequestLogMiddleware',
]

ROOT_URLCONF = 'dialflow.urls'
ASGI_APPLICATION = 'dialflow.asgi.application'
WSGI_APPLICATION = 'dialflow.wsgi.application'
AUTH_USER_MODEL = 'users.User'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.global_context',
            ],
        },
    },
]

# ─── Database ────────────────────────────────────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME':     config('DB_NAME',     default='dialflow_db'),
        'USER':     config('DB_USER',     default='dialflow_user'),
        'PASSWORD': config('DB_PASSWORD', default='dialflow_pass'),
        'HOST':     config('DB_HOST',     default='localhost'),
        'PORT':     config('DB_PORT',     default='5432'),
        'CONN_MAX_AGE': 60,
        'OPTIONS': {'connect_timeout': 10},
    }
}

# ─── Redis / Cache ────────────────────────────────────────────────────────────
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'CONNECTION_POOL_KWARGS': {'max_connections': 50},
        },
        'KEY_PREFIX': 'dialflow',
        'TIMEOUT': 300,
    }
}

# ─── Channels (WebSocket) ─────────────────────────────────────────────────────
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_URL],
            'capacity': 1500,
            'expiry': 10,
        },
    },
}

# ─── Celery ───────────────────────────────────────────────────────────────────
CELERY_BROKER_URL         = config('CELERY_BROKER_URL',    default='redis://localhost:6379/1')
CELERY_RESULT_BACKEND     = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/2')
CELERY_ACCEPT_CONTENT     = ['json']
CELERY_TASK_SERIALIZER    = 'json'
CELERY_RESULT_SERIALIZER  = 'json'
CELERY_TIMEZONE           = config('TIME_ZONE', default='Asia/Kolkata')
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT    = 300
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # Important for call tasks — prevents starvation

# ─── Auth ─────────────────────────────────────────────────────────────────────
LOGIN_URL          = '/auth/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/auth/login/'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]

# ─── Internationalisation ─────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = config('TIME_ZONE', default='Asia/Kolkata')
USE_I18N = True
USE_TZ   = True

# ─── Static / Media ───────────────────────────────────────────────────────────
STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ─── REST Framework ───────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

# ─── Session ──────────────────────────────────────────────────────────────────
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'
SESSION_COOKIE_AGE = 86400 * 7  # 7 days
SESSION_SAVE_EVERY_REQUEST = False

# ─── Asterisk Config (read from .env, used by telephony app) ─────────────────
ASTERISK = {
    'ARI_HOST':     config('ARI_HOST',     default='127.0.0.1'),
    'ARI_PORT':     config('ARI_PORT',     default=8088, cast=int),
    'ARI_USERNAME': config('ARI_USERNAME', default='asterisk'),
    'ARI_PASSWORD': config('ARI_PASSWORD', default='asterisk'),
    'ARI_APP_NAME': config('ARI_APP_NAME', default='dialflow'),
    'AMI_HOST':     config('AMI_HOST',     default='127.0.0.1'),
    'AMI_PORT':     config('AMI_PORT',     default=5038, cast=int),
    'AMI_USERNAME': config('AMI_USERNAME', default='admin'),
    'AMI_PASSWORD': config('AMI_PASSWORD', default='admin'),
    'RECONNECT_DELAY': 5,     # seconds between reconnect attempts
    'MAX_RECONNECTS': 0,      # 0 = infinite
}

# ─── WebRTC Config ────────────────────────────────────────────────────────────
WEBRTC = {
    'WS_URL':      config('WEBRTC_WS_URL',      default='ws://127.0.0.1:8088/ws'),
    'DOMAIN':      config('WEBRTC_DOMAIN',       default='127.0.0.1'),
    'STUN_SERVER': config('WEBRTC_STUN_SERVER',  default='stun:stun.l.google.com:19302'),
}

# ─── Dialer Config ────────────────────────────────────────────────────────────
DIALER = {
    'RECORDING_PATH':       config('RECORDING_PATH',       default='/var/spool/asterisk/monitor'),
    'RECORDING_URL_PREFIX': config('RECORDING_URL_PREFIX', default='/recordings/'),
    'MAX_CONCURRENT_CALLS': config('MAX_CONCURRENT_CALLS', default=200, cast=int),
    'HOPPER_FILL_INTERVAL': 60,    # seconds
    'ZOMBIE_TIMEOUT':       120,   # seconds before offline agent is marked away
    'HEARTBEAT_INTERVAL':   30,    # seconds (frontend sends heartbeat)
}

# ─── Logging ─────────────────────────────────────────────────────────────────
import os
os.makedirs(BASE_DIR / 'logs', exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'dialflow.log',
            'maxBytes': 10 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'ari_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'ari_worker.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 3,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'dialflow': {'handlers': ['console', 'file'], 'level': 'DEBUG', 'propagate': False},
        'telephony.ari_worker': {'handlers': ['console', 'ari_file'], 'level': 'DEBUG', 'propagate': False},
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'channels': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'celery': {'handlers': ['console', 'file'], 'level': 'INFO', 'propagate': False},
    },
}

# ── Sarvam AI (Indian voice AI calling) ──────────────────────────────────────
SARVAM_AI = {
    'API_KEY':   config('SARVAM_AI_API_KEY', default=''),
    'BASE_URL':  'https://api.sarvam.ai',
    'DEFAULT_VOICE':    'meera',       # meera | arjun | anushka | kalpana
    'DEFAULT_LANGUAGE': 'hi-IN',       # hi-IN | en-IN | ta-IN | te-IN ...
    'DEFAULT_SPEED':    1.0,
}

# ── Dialer settings ───────────────────────────────────────────────────────────
DIALER = {
    'RECORDING_PATH':        config('RECORDING_PATH', default='/var/spool/asterisk/monitor'),
    'RECORDING_URL_PREFIX':  '/recordings/',
    'ZOMBIE_TIMEOUT':        90,        # seconds before agent marked offline
    'HEARTBEAT_INTERVAL':    25,        # seconds between WS heartbeats
    'HOPPER_FILL_INTERVAL':  30,        # seconds between hopper fills
    'MAX_CONCURRENT_CALLS':  200,       # global cap
    'TARGET_UTILISATION':    0.90,      # target agent utilisation for Erlang-C
    'DEFAULT_TIMEZONE':      'Asia/Kolkata',
}
