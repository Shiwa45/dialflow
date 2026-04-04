# core/middleware.py
import logging
import pytz
from django.utils import timezone

logger = logging.getLogger('dialflow')


class TimezoneMiddleware:
    """Activate the authenticated user's timezone for each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tz_name = None
        if request.user.is_authenticated:
            tz_name = getattr(request.user, 'timezone', None)
        if tz_name:
            try:
                timezone.activate(pytz.timezone(tz_name))
            except Exception:
                timezone.deactivate()
        else:
            timezone.deactivate()
        response = self.get_response(request)
        timezone.deactivate()
        return response


class RequestLogMiddleware:
    """Log every non-static request at DEBUG level."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(('/static/', '/media/', '/favicon')):
            logger.debug(f'{request.method} {request.path}')
        return self.get_response(request)
