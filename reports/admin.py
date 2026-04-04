# reports/admin.py
from django.contrib import admin
from .models import DailySnapshot


@admin.register(DailySnapshot)
class DailySnapshotAdmin(admin.ModelAdmin):
    list_display  = ('date', 'campaign', 'calls_total', 'calls_answered',
                     'calls_dropped', 'abandon_rate', 'avg_talk_time')
    list_filter   = ('campaign', 'date')
    date_hierarchy = 'date'
    ordering      = ('-date',)
    readonly_fields = ('created_at', 'updated_at')
