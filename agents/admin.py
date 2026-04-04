# agents/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import AgentStatus, CallDisposition


@admin.register(AgentStatus)
class AgentStatusAdmin(admin.ModelAdmin):
    list_display  = ('user', 'status_badge', 'active_campaign',
                     'status_changed_at', 'last_heartbeat', 'calls_today')
    list_filter   = ('status',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    readonly_fields = ('status_changed_at', 'call_started_at', 'wrapup_started_at',
                       'last_heartbeat', 'calls_today', 'talk_time_today', 'break_time_today')

    @admin.display(description='Status')
    def status_badge(self, obj):
        colors = {
            'ready':    '#10B981',
            'on_call':  '#3B82F6',
            'wrapup':   '#F59E0B',
            'break':    '#8B5CF6',
            'offline':  '#6B7280',
            'training': '#EC4899',
        }
        color = colors.get(obj.status, '#6B7280')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:12px;font-size:11px">{}</span>',
            color, obj.get_status_display()
        )

    actions = ['force_offline']

    @admin.action(description='Force selected agents offline')
    def force_offline(self, request, queryset):
        for status in queryset:
            status.go_offline()


@admin.register(CallDisposition)
class CallDispositionAdmin(admin.ModelAdmin):
    list_display  = ('agent', 'disposition', 'campaign', 'lead', 'auto_applied', 'created_at')
    list_filter   = ('disposition', 'campaign', 'auto_applied')
    search_fields = ('agent__username', 'lead__primary_phone', 'notes')
    readonly_fields = ('created_at', 'updated_at')
