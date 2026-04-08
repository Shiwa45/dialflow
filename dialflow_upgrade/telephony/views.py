# telephony/views.py
import json
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from .models import AsteriskServer, Phone, Carrier


def admin_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_supervisor:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden()
        return view_func(request, *args, **kwargs)
    return _wrapped


# ── Asterisk Server CRUD ─────────────────────────────────────────────────────

@login_required
@admin_required
def server_list(request):
    servers = AsteriskServer.objects.all()
    phones = Phone.objects.select_related('user', 'asterisk_server').all()
    carriers = Carrier.objects.select_related('asterisk_server').all()

    from django.contrib.auth import get_user_model
    User = get_user_model()
    available_agents = User.objects.filter(role='agent', is_active=True, phone__isnull=True)

    return render(request, 'telephony/server_list.html', {
        'servers': servers, 'phones': phones, 'carriers': carriers,
        'available_agents': available_agents,
        'active_tab': request.GET.get('tab', 'servers'),
    })


@login_required
@admin_required
def server_detail(request, pk):
    server = get_object_or_404(AsteriskServer, pk=pk)
    return render(request, 'telephony/server_detail.html', {'server': server})


@login_required
@admin_required
@require_POST
def server_create(request):
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    try:
        server = AsteriskServer.objects.create(
            name=data.get('name', '').strip(),
            description=data.get('description', ''),
            server_ip=data.get('server_ip', '127.0.0.1'),
            is_active=data.get('is_active', True),
            ari_host=data.get('ari_host', '127.0.0.1'),
            ari_port=int(data.get('ari_port', 8088)),
            ari_username=data.get('ari_username', ''),
            ari_password=data.get('ari_password', ''),
            ari_app_name=data.get('ari_app_name', 'dialflow'),
            ami_host=data.get('ami_host', '127.0.0.1'),
            ami_port=int(data.get('ami_port', 5038)),
            ami_username=data.get('ami_username', ''),
            ami_password=data.get('ami_password', ''),
        )
        return JsonResponse({'success': True, 'id': server.pk})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@admin_required
@require_POST
def server_edit(request, pk):
    server = get_object_or_404(AsteriskServer, pk=pk)
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    for field in ['name', 'description', 'server_ip', 'ari_host', 'ari_username',
                  'ari_password', 'ari_app_name', 'ami_host', 'ami_username', 'ami_password']:
        if field in data:
            setattr(server, field, data[field])
    if 'ari_port' in data:
        server.ari_port = int(data['ari_port'])
    if 'ami_port' in data:
        server.ami_port = int(data['ami_port'])
    if 'is_active' in data:
        server.is_active = data['is_active'] in (True, 'true', '1', 'on')

    server.save()
    return JsonResponse({'success': True})


@login_required
@admin_required
@require_POST
def server_delete(request, pk):
    server = get_object_or_404(AsteriskServer, pk=pk)
    if server.campaigns.exists():
        return JsonResponse({'error': 'Cannot delete server with active campaigns'}, status=400)
    server.delete()
    return JsonResponse({'success': True})


# ── Phone Extension CRUD ─────────────────────────────────────────────────────

@login_required
@admin_required
def phone_list(request):
    phones = Phone.objects.select_related('user', 'asterisk_server').all()
    return render(request, 'telephony/server_list.html', {
        'phones': phones, 'active_tab': 'phones',
    })


@login_required
@admin_required
@require_POST
def phone_create(request):
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    try:
        server = AsteriskServer.objects.get(id=data.get('asterisk_server_id'))
    except AsteriskServer.DoesNotExist:
        return JsonResponse({'error': 'Server not found'}, status=400)

    user_id = data.get('user_id')
    user = None
    if user_id:
        from django.contrib.auth import get_user_model
        try:
            user = get_user_model().objects.get(id=user_id)
        except Exception:
            pass

    try:
        phone = Phone(
            extension=data.get('extension', '').strip(),
            name=data.get('name', ''),
            phone_type=data.get('phone_type', 'webrtc'),
            asterisk_server=server,
            user=user,
            context=data.get('context', 'agents'),
            allow_codecs=data.get('allow_codecs', 'opus,ulaw,alaw'),
            is_active=data.get('is_active', True) in (True, 'true', '1', 'on'),
        )
        if data.get('secret'):
            phone.secret = data['secret']
        phone.save()
        return JsonResponse({'success': True, 'id': phone.pk, 'secret': phone.secret})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@admin_required
@require_POST
def phone_edit(request, pk):
    phone = get_object_or_404(Phone, pk=pk)
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    for field in ['extension', 'name', 'phone_type', 'context', 'allow_codecs']:
        if field in data:
            setattr(phone, field, data[field])
    if 'secret' in data and data['secret']:
        phone.secret = data['secret']
    if 'is_active' in data:
        phone.is_active = data['is_active'] in (True, 'true', '1', 'on')
    if 'user_id' in data:
        from django.contrib.auth import get_user_model
        if data['user_id']:
            try:
                phone.user = get_user_model().objects.get(id=data['user_id'])
            except Exception:
                pass
        else:
            phone.user = None
    if 'asterisk_server_id' in data:
        try:
            phone.asterisk_server = AsteriskServer.objects.get(id=data['asterisk_server_id'])
        except Exception:
            pass

    phone.save()
    return JsonResponse({'success': True})


@login_required
@admin_required
@require_POST
def phone_delete(request, pk):
    phone = get_object_or_404(Phone, pk=pk)
    phone.delete()
    return JsonResponse({'success': True})


@login_required
@admin_required
@require_POST
def phone_sync(request, pk):
    phone = get_object_or_404(Phone, pk=pk)
    try:
        phone.sync_to_asterisk()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


# ── Carrier CRUD ─────────────────────────────────────────────────────────────

@login_required
@admin_required
def carrier_list(request):
    carriers = Carrier.objects.select_related('asterisk_server').all()
    return render(request, 'telephony/server_list.html', {
        'carriers': carriers, 'active_tab': 'carriers',
    })


@login_required
@admin_required
@require_POST
def carrier_create(request):
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    try:
        server = AsteriskServer.objects.get(id=data.get('asterisk_server_id'))
    except AsteriskServer.DoesNotExist:
        return JsonResponse({'error': 'Server not found'}, status=400)

    try:
        carrier = Carrier.objects.create(
            name=data.get('name', '').strip(),
            description=data.get('description', ''),
            asterisk_server=server,
            protocol=data.get('protocol', 'pjsip'),
            host=data.get('host', ''),
            port=int(data.get('port', 5060)),
            username=data.get('username', ''),
            password=data.get('password', ''),
            caller_id=data.get('caller_id', ''),
            max_channels=int(data.get('max_channels', 30)),
            dial_prefix=data.get('dial_prefix', ''),
            is_active=data.get('is_active', True) in (True, 'true', '1', 'on'),
        )
        return JsonResponse({'success': True, 'id': carrier.pk})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@admin_required
@require_POST
def carrier_edit(request, pk):
    carrier = get_object_or_404(Carrier, pk=pk)
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    for field in ['name', 'description', 'protocol', 'host', 'username',
                  'password', 'caller_id', 'dial_prefix']:
        if field in data:
            setattr(carrier, field, data[field])
    if 'port' in data:
        carrier.port = int(data['port'])
    if 'max_channels' in data:
        carrier.max_channels = int(data['max_channels'])
    if 'is_active' in data:
        carrier.is_active = data['is_active'] in (True, 'true', '1', 'on')
    if 'asterisk_server_id' in data:
        try:
            carrier.asterisk_server = AsteriskServer.objects.get(id=data['asterisk_server_id'])
        except Exception:
            pass

    carrier.save()
    return JsonResponse({'success': True})


@login_required
@admin_required
@require_POST
def carrier_delete(request, pk):
    carrier = get_object_or_404(Carrier, pk=pk)
    if carrier.campaigns.exists():
        return JsonResponse({'error': 'Cannot delete carrier with active campaigns'}, status=400)
    carrier.delete()
    return JsonResponse({'success': True})


@login_required
@admin_required
@require_POST
def carrier_sync(request, pk):
    carrier = get_object_or_404(Carrier, pk=pk)
    try:
        carrier.sync_to_asterisk()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


# ── ARI Status API ───────────────────────────────────────────────────────────

def ari_status(request):
    servers = AsteriskServer.objects.filter(is_active=True).values(
        'id', 'name', 'connection_status', 'last_connected'
    )
    return JsonResponse({'servers': list(servers)})
