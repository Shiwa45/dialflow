# users/views.py
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages
from .forms import LoginForm, UserProfileForm


def login_view(request):
    if request.user.is_authenticated:
        return redirect('core:home')

    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        next_url = request.GET.get('next', 'core:home')
        return redirect(next_url)

    return render(request, 'users/login.html', {'form': form})


def logout_view(request):
    # Update agent status to offline before logout
    if request.user.is_authenticated and request.user.is_agent:
        try:
            from agents.models import AgentStatus
            AgentStatus.objects.filter(user=request.user).update(
                status='offline',
                active_campaign=None,
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
    """Allow any authenticated user to change their own password."""
    from django.contrib.auth.forms import PasswordChangeForm
    from django.contrib.auth import update_session_auth_hash
    from django.contrib import messages

    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        update_session_auth_hash(request, form.user)
        messages.success(request, 'Password changed successfully.')
        return redirect('users:profile')
    return render(request, 'users/change_password.html', {'form': form})
from django.db import models


# ── Admin User Management ─────────────────────────────────────────────────────

@login_required
def user_management(request):
    """Admin UI to manage all users."""
    if not request.user.is_admin:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('Admin access required.')

    from django.contrib.auth import get_user_model
    from django.core.paginator import Paginator
    from agents.models import AgentStatus

    User = get_user_model()
    qs = User.objects.select_related('agent_status', 'agent_status__active_campaign', 'phone').order_by('role', 'username')

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
        qs = qs.filter(agent_status__status__in=['ready', 'on_call', 'wrapup', 'break'])

    page = Paginator(qs, 30).get_page(request.GET.get('page'))

    return render(request, 'users/user_management.html', {
        'users':           page,
        'page':            page,
        'total_users':     User.objects.count(),
        'active_users':    User.objects.filter(is_active=True).count(),
        'agent_count':     User.objects.filter(role='agent').count(),
        'supervisor_count':User.objects.filter(role='supervisor').count(),
        'online_count':    AgentStatus.objects.filter(status__in=['ready','on_call','wrapup']).count(),
    })


@login_required
def user_create(request):
    """API: Create a new user."""
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
    role     = data.get('role', 'agent')

    if not username or not password:
        return JsonResponse({'error': 'Username and password required'})
    if User.objects.filter(username=username).exists():
        return JsonResponse({'error': f'Username "{username}" already exists'})
    if len(password) < 8:
        return JsonResponse({'error': 'Password must be at least 8 characters'})

    user = User.objects.create_user(
        username   = username,
        password   = password,
        email      = data.get('email', ''),
        first_name = data.get('first_name', ''),
        last_name  = data.get('last_name', ''),
        role       = role,
    )

    # Create AgentStatus for agents
    if role in ('agent', 'supervisor'):
        from agents.models import AgentStatus
        AgentStatus.objects.get_or_create(user=user)

    return JsonResponse({'success': True, 'user_id': user.pk})


@login_required
def user_edit(request, pk):
    """API: Edit user fields."""
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
    user.last_name  = data.get('last_name',  user.last_name)
    user.email      = data.get('email',      user.email)
    if data.get('role') and data['role'] in ('agent', 'supervisor', 'admin'):
        user.role   = data['role']
    user.save()
    return JsonResponse({'success': True})


@login_required
def user_toggle(request, pk):
    """API: Activate or deactivate a user."""
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

    if user == request.user:
        return JsonResponse({'error': 'Cannot deactivate yourself'})

    data     = json.loads(request.body)
    activate = data.get('activate', True)
    user.is_active = activate
    user.save(update_fields=['is_active'])
    return JsonResponse({'success': True})


@login_required
def user_reset_password(request, pk):
    """API: Reset user password."""
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

    data = json.loads(request.body)
    pw   = data.get('password', '')
    if len(pw) < 8:
        return JsonResponse({'error': 'Password too short'})

    user.set_password(pw)
    user.save()
    return JsonResponse({'success': True})
