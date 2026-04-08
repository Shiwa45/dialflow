# agents/admin.py
from django.contrib import admin
from .models import AgentStatus, AgentLoginLog, PauseCode, CallDisposition


@admin.register(PauseCode)
class PauseCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'is_active', 'sort_order')
    list_editable = ('is_active', 'sort_order')
    search_fields = ('name', 'code')


@admin.register(AgentLoginLog)
class AgentLoginLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'login_at', 'logout_at', 'duration_display', 'ip_address')
    list_filter = ('user', 'login_at')
    search_fields = ('user__username',)
    readonly_fields = ('duration_display',)
    date_hierarchy = 'login_at'


@admin.register(AgentStatus)
class AgentStatusAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'active_campaign', 'pause_code',
                    'calls_today', 'talk_time_today', 'status_changed_at')
    list_filter = ('status', 'active_campaign')
    search_fields = ('user__username',)
    readonly_fields = ('status_changed_at', 'last_heartbeat', 'created_at', 'updated_at')


@admin.register(CallDisposition)
class CallDispositionAdmin(admin.ModelAdmin):
    list_display = ('call_log', 'agent', 'disposition', 'campaign', 'created_at')
    list_filter = ('disposition', 'campaign')
    search_fields = ('agent__username', 'call_log__phone_number')
    readonly_fields = ('created_at',)
