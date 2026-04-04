# dialflow/wsgi.py
# WSGI entrypoint — for Gunicorn or uWSGI (no WebSocket support).
# For full WebSocket support use asgi.py with Daphne.
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dialflow.settings')
application = get_wsgi_application()
