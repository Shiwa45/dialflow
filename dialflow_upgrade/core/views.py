# core/views.py
import json
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone


def dashboard_redirect(request):
    if not request.user.is_authenticated:
        return redirect('users:login')
    role = getattr(request.user, 'role', 'agent')
    if role == 'agent':
        return redirect('agents:dashboard')
    else:
        # Admin and supervisor see the CRM (campaigns)
        return redirect('campaigns:list')


@login_required
def dashboard(request):
    return dashboard_redirect(request)


def health_check(request):
    from django.db import connection
    import redis as redis_lib
    from django.conf import settings

    checks = {'db': False, 'redis': False}
    try:
        connection.ensure_connection()
        checks['db'] = True
    except Exception:
        pass
    try:
        r = redis_lib.from_url(settings.REDIS_URL)
        r.ping()
        checks['redis'] = True
    except Exception:
        pass

    status = 200 if all(checks.values()) else 503
    return JsonResponse({
        'status': 'ok' if status == 200 else 'degraded',
        'checks': checks,
        'timestamp': timezone.now().isoformat(),
    }, status=status)
