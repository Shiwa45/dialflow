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
