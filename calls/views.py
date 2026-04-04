# calls/views.py
import os
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, FileResponse, Http404
from django.core.paginator import Paginator
from django.conf import settings
from .models import CallLog


@login_required
def call_list(request):
    qs = CallLog.objects.select_related('lead', 'campaign', 'agent', 'disposition')

    # Supervisors see all; agents see only their own
    if not request.user.is_supervisor:
        qs = qs.filter(agent=request.user)

    # Filters
    campaign_id = request.GET.get('campaign')
    status      = request.GET.get('status')
    date_from   = request.GET.get('date_from')
    date_to     = request.GET.get('date_to')
    search      = request.GET.get('q', '').strip()

    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)
    if status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(started_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(started_at__date__lte=date_to)
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(phone_number__icontains=search) |
            Q(lead__first_name__icontains=search) |
            Q(lead__last_name__icontains=search)
        )

    paginator = Paginator(qs.order_by('-started_at'), 50)
    page      = paginator.get_page(request.GET.get('page'))

    return render(request, 'calls/list.html', {
        'page':    page,
        'total':   qs.count(),
        'filters': {'campaign': campaign_id, 'status': status,
                    'date_from': date_from, 'date_to': date_to, 'q': search},
    })


@login_required
def call_detail(request, pk):
    call = get_object_or_404(CallLog, pk=pk)
    if not request.user.is_supervisor and call.agent != request.user:
        raise Http404
    return render(request, 'calls/detail.html', {'call': call})


@login_required
def serve_recording(request, pk):
    """Serve call recording file. Checks permissions before serving."""
    call = get_object_or_404(CallLog, pk=pk)

    if not request.user.is_supervisor and call.agent != request.user:
        raise Http404

    if not call.recording_path:
        raise Http404

    path = call.recording_path
    if not os.path.exists(path):
        raise Http404

    return FileResponse(open(path, 'rb'), content_type='audio/wav')


@login_required
def call_stats_api(request):
    """JSON stats for reporting page."""
    from django.db.models import Count, Sum, Avg, Q
    from django.utils import timezone

    today = timezone.now().date()
    qs    = CallLog.objects.filter(started_at__date=today)

    if not request.user.is_supervisor:
        qs = qs.filter(agent=request.user)

    agg = qs.aggregate(
        total     = Count('id'),
        answered  = Count('id', filter=Q(status='completed')),
        dropped   = Count('id', filter=Q(status='dropped')),
        no_answer = Count('id', filter=Q(status='no_answer')),
        avg_dur   = Avg('duration', filter=Q(status='completed')),
        total_dur = Sum('duration', filter=Q(status='completed')),
    )
    return JsonResponse(agg)
