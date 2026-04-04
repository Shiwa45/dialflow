# users/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display  = ('username', 'full_name', 'role', 'is_active', 'date_joined')
    list_filter   = ('role', 'is_active', 'is_staff')
    search_fields = ('username', 'first_name', 'last_name', 'email')
    ordering      = ('username',)

    fieldsets = BaseUserAdmin.fieldsets + (
        ('DialFlow', {'fields': ('role', 'phone_number', 'timezone', 'avatar')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('DialFlow', {'fields': ('role', 'phone_number', 'timezone')}),
    )

    @admin.display(description='Full name')
    def full_name(self, obj):
        return obj.get_full_name() or '—'
