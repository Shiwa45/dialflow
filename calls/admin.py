# calls/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import CallLog


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display  = ('phone_number', 'status_badge', 'campaign', 'agent',
                     'duration_display', 'started_at', 'has_recording')
    list_filter   = ('status', 'direction', 'campaign')
    search_fields = ('phone_number', 'channel_id', 'lead__first_name', 'lead__last_name')
    readonly_fields = ('channel_id', 'bridge_id', 'started_at', 'answered_at',
                       'ended_at', 'duration', 'ring_duration', 'amd_result',
                       'recording_path', 'created_at', 'updated_at')
    date_hierarchy = 'started_at'

    @admin.display(description='Status')
    def status_badge(self, obj):
        colors = {
            'completed': '#10B981', 'answered': '#3B82F6',
            'no_answer': '#F59E0B', 'dropped':  '#EF4444',
            'failed':    '#6B7280', 'initiated': '#8B5CF6',
        }
        color = colors.get(obj.status, '#9CA3AF')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:12px;font-size:11px">{}</span>',
            color, obj.get_status_display()
        )

    @admin.display(description='Duration')
    def duration_display(self, obj):
        return obj.duration_display

    @admin.display(description='Recording', boolean=True)
    def has_recording(self, obj):
        return bool(obj.recording_path)
