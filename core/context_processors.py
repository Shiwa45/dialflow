# core/context_processors.py
from django.conf import settings


def global_context(request):
    """Inject global variables into every template."""
    ctx = {
        'APP_NAME': 'DialFlow Pro',
        'DEBUG': settings.DEBUG,
        'WEBRTC_WS_URL':  settings.WEBRTC.get('WS_URL', ''),
        'WEBRTC_DOMAIN':  settings.WEBRTC.get('DOMAIN', ''),
        'WEBRTC_STUN':    settings.WEBRTC.get('STUN_SERVER', 'stun:stun.l.google.com:19302'),
    }
    if request.user.is_authenticated:
        ctx['current_user_role'] = getattr(request.user, 'role', 'agent')
    return ctx
