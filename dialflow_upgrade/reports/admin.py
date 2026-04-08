# reports/admin.py
from django.contrib import admin
from .models import DailySnapshot, AgentDailyLog


@admin.register(DailySnapshot)
class DailySnapshotAdmin(admin.ModelAdmin):
    list_display = ('date', 'campaign', 'calls_total', 'calls_answered',
                    'calls_dropped', 'avg_talk_time', 'abandon_rate')
    list_filter = ('campaign', 'date')
    date_hierarchy = 'date'
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AgentDailyLog)
class AgentDailyLogAdmin(admin.ModelAdmin):
    list_display = ('date', 'agent', 'campaign', 'calls_dialed', 'calls_answered',
                    'login_time_display', 'talk_time_display', 'dispositions_sale')
    list_filter = ('date', 'campaign')
    search_fields = ('agent__username',)
    date_hierarchy = 'date'
