# dialflow/settings/prod.py
from .base import *  # noqa
from decouple import config

DEBUG = False
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='').split(',')

# Security
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Email
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST          = config('EMAIL_HOST',          default='localhost')
EMAIL_PORT          = config('EMAIL_PORT',           default=587, cast=int)
EMAIL_USE_TLS       = config('EMAIL_USE_TLS',        default=True, cast=bool)
EMAIL_HOST_USER     = config('EMAIL_HOST_USER',      default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD',  default='')
DEFAULT_FROM_EMAIL  = config('DEFAULT_FROM_EMAIL',   default='noreply@dialflow.app')

# Static (WhiteNoise or S3 in production)
STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'

# Sentry error tracking (optional)
SENTRY_DSN = config('SENTRY_DSN', default='')
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)
