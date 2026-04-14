# users/views.py
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.db import models
from .forms import LoginForm, UserProfileForm


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')


def login_view(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.get_user()
        login(request, user)

        # Create login log for agents/supervisors
        if user.role in ('agent', 'supervisor'):
            from agents.models import AgentLoginLog, AgentStatus
            log = AgentLoginLog.objects.create(
                user=user,
                ip_address=_get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
            # Link to agent status
            if user.role == 'agent':
                status, _ = AgentStatus.objects.get_or_create(user=user)
                status.login_log = log
                status.save(update_fields=['login_log'])

        next_url = request.GET.get('next')
        if next_url:
            return redirect(next_url)
        return _role_redirect(user)

    return render(request, 'users/login.html', {'form': form})


def _role_redirect(user):
    """Redirect user based on their role."""
    if user.role == 'agent':
        return redirect('agents:dashboard')
    else:
        # Admin and supervisor go to CRM (campaigns list)
        return redirect('campaigns:list')


def logout_view(request):
    if request.user.is_authenticated:
        # Close login session
        if request.user.role in ('agent', 'supervisor'):
            from agents.models import AgentLoginLog, AgentStatus
            from django.utils import timezone
            AgentLoginLog.objects.filter(
                user=request.user, logout_at__isnull=True
            ).update(logout_at=timezone.now())

        # Set agent offline
        if request.user.is_agent:
            try:
                from agents.models import AgentStatus
                AgentStatus.objects.filter(user=request.user).update(
                    status='offline',
                    active_campaign=None,
                    login_log=None,
                )
            except Exception:
                pass

    logout(request)
    return redirect('users:login')


@login_required
def profile_view(request):
    form = UserProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Profile updated.')
        return redirect('users:profile')
    return render(request, 'users/profile.html', {'form': form})


@login_required
def change_password(request):
    from django.contrib.auth.forms import PasswordChangeForm
    from django.contrib.auth import update_session_auth_hash

    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        update_session_auth_hash(request, form.user)
        messages.success(request, 'Password changed successfully.')
        return redirect('users:profile')
    return render(request, 'users/change_password.html', {'form': form})


# ── Admin User Management ─────────────────────────────────────────────────────

@login_required
def user_management(request):
    if not request.user.is_admin:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('Admin access required.')

    from django.contrib.auth import get_user_model
    from django.core.paginator import Paginator
    from agents.models import AgentStatus

    User = get_user_model()
    qs = User.objects.select_related(
        'agent_status', 'agent_status__active_campaign', 'phone'
    ).order_by('role', 'username')

    q = request.GET.get('q', '')
    if q:
        qs = qs.filter(
            models.Q(username__icontains=q) |
            models.Q(first_name__icontains=q) |
            models.Q(last_name__icontains=q) |
            models.Q(email__icontains=q)
        )

    role = request.GET.get('role', '')
    if role:
        qs = qs.filter(role=role)

    status = request.GET.get('status', '')
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'online':
        qs = qs.filter(agent_status__status__in=['ready', 'ringing', 'on_call', 'wrapup', 'break'])

    page = Paginator(qs, 30).get_page(request.GET.get('page'))

    return render(request, 'users/user_management.html', {
        'users':            page,
        'page':             page,
        'total_users':      User.objects.count(),
        'active_users':     User.objects.filter(is_active=True).count(),
        'agent_count':      User.objects.filter(role='agent').count(),
        'supervisor_count': User.objects.filter(role='supervisor').count(),
        'online_count':     AgentStatus.objects.filter(status__in=['ready', 'ringing', 'on_call', 'wrapup']).count(),
    })


@login_required
def user_create(request):
    if not request.user.is_admin:
        return JsonResponse({'error': 'Admin only'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from django.contrib.auth import get_user_model
    import json
    User = get_user_model()

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'agent')

    if not username or not password:
        return JsonResponse({'error': 'Username and password required'})
    if User.objects.filter(username=username).exists():
        return JsonResponse({'error': f'Username "{username}" already exists'})
    if len(password) < 8:
        return JsonResponse({'error': 'Password must be at least 8 characters'})

    user = User.objects.create_user(
        username=username, password=password,
        email=data.get('email', ''),
        first_name=data.get('first_name', ''),
        last_name=data.get('last_name', ''),
        role=role,
    )

    if role in ('agent', 'supervisor'):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=user)

    return JsonResponse({'success': True, 'user_id': user.pk})


@login_required
def user_edit(request, pk):
    if not request.user.is_admin:
        return JsonResponse({'error': 'Admin only'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from django.contrib.auth import get_user_model
    import json
    User = get_user_model()

    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    user.first_name = data.get('first_name', user.first_name)
    user.last_name = data.get('last_name', user.last_name)
    user.email = data.get('email', user.email)
    if data.get('role') and data['role'] in ('agent', 'supervisor', 'admin'):
        user.role = data['role']
    user.save()
    return JsonResponse({'success': True})


@login_required
def user_toggle(request, pk):
    if not request.user.is_admin:
        return JsonResponse({'error': 'Admin only'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    user.is_active = not user.is_active
    if not user.is_active:
        from django.utils import timezone
        user.deactivated_at = timezone.now()
    user.save()
    return JsonResponse({'success': True, 'is_active': user.is_active})


@login_required
def user_reset_password(request, pk):
    if not request.user.is_admin:
        return JsonResponse({'error': 'Admin only'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from django.contrib.auth import get_user_model
    import json
    User = get_user_model()

    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    new_password = data.get('password', '')
    if len(new_password) < 8:
        return JsonResponse({'error': 'Password must be at least 8 characters'})

    user.set_password(new_password)
    user.save()
    return JsonResponse({'success': True})
