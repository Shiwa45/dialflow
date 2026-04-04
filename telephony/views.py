# telephony/views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from .models import AsteriskServer, Phone, Carrier


@login_required
def server_list(request):
    servers  = AsteriskServer.objects.all()
    phones   = Phone.objects.select_related('user', 'asterisk_server').all()
    carriers = Carrier.objects.select_related('asterisk_server').all()
    return render(request, 'telephony/server_list.html', {
        'servers': servers, 'phones': phones, 'carriers': carriers,
    })


@login_required
def server_detail(request, pk):
    server = get_object_or_404(AsteriskServer, pk=pk)
    return render(request, 'telephony/server_list.html', {'server': server})


@login_required
def phone_list(request):
    phones = Phone.objects.select_related('user', 'asterisk_server').all()
    return render(request, 'telephony/server_list.html', {'phones': phones})


@login_required
def carrier_list(request):
    carriers = Carrier.objects.select_related('asterisk_server').all()
    return render(request, 'telephony/server_list.html', {'carriers': carriers})


def ari_status(request):
    servers = AsteriskServer.objects.filter(is_active=True).values(
        'id', 'name', 'connection_status', 'last_connected'
    )
    return JsonResponse({'servers': list(servers)})
