# users/urls.py
from django.urls import path
from . import views

app_name = 'users'

urlpatterns = [
    path('login/',               views.login_view,          name='login'),
    path('logout/',              views.logout_view,         name='logout'),
    path('profile/',             views.profile_view,        name='profile'),
    path('password/change/',     views.change_password,     name='change_password'),

    # Admin user management UI
    path('manage/',              views.user_management,     name='user_management'),

    # User management APIs (admin only)
    path('api/create/',          views.user_create,         name='user_create'),
    path('api/<int:pk>/edit/',   views.user_edit,           name='user_edit'),
    path('api/<int:pk>/toggle/', views.user_toggle,         name='user_toggle'),
    path('api/<int:pk>/reset-password/', views.user_reset_password, name='user_reset_password'),
]
