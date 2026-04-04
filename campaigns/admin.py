# campaigns/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import Campaign, CampaignAgent, Disposition, HopperEntry, DNCEntry


@admin.register(Disposition)
class DispositionAdmin(admin.ModelAdmin):
    list_display  = ('name', 'category', 'outcome', 'color_preview', 'hotkey', 'is_active', 'sort_order')
    list_filter   = ('category', 'outcome', 'is_active')
    search_fields = ('name',)
    ordering      = ('sort_order', 'name')

    @admin.display(description='Color')
    def color_preview(self, obj):
        return format_html(
            '<span style="display:inline-block;width:20px;height:20px;'
            'border-radius:4px;background:{};border:1px solid #ccc;"></span>',
            obj.color
        )


class CampaignAgentInline(admin.TabularInline):
    model      = CampaignAgent
    extra      = 0
    fields     = ('agent', 'is_active', 'joined_at')
    readonly_fields = ('joined_at',)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display   = ('name', 'status', 'dial_mode', 'stat_agents_active',
                      'stat_calls_today', 'stat_answered_today', 'stat_abandon_rate')
    list_filter    = ('status', 'dial_mode', 'amd_enabled', 'enable_recording')
    search_fields  = ('name',)
    readonly_fields = ('stat_calls_today', 'stat_answered_today',
                       'stat_abandon_rate', 'stat_agents_active', 'created_at', 'updated_at')
    inlines        = [CampaignAgentInline]
    filter_horizontal = ('dispositions',)

    fieldsets = (
        ('Identity',    {'fields': ('name', 'description', 'status', 'created_by')}),
        ('Telephony',   {'fields': ('asterisk_server', 'carrier', 'caller_id', 'dial_prefix')}),
        ('Dial Settings', {'fields': ('dial_mode', 'dial_ratio', 'min_dial_ratio', 'max_dial_ratio',
                                      'dial_timeout', 'abandon_rate')}),
        ('Hopper',      {'fields': ('hopper_level', 'hopper_size', 'lead_order')}),
        ('AMD',         {'fields': ('amd_enabled', 'amd_action')}),
        ('Recording',   {'fields': ('enable_recording', 'recording_mode')}),
        ('Call Hours',  {'fields': ('call_hour_start', 'call_hour_end', 'respect_timezone')}),
        ('Wrapup',      {'fields': ('wrapup_timeout', 'auto_wrapup_enabled',
                                    'auto_wrapup_timeout', 'auto_wrapup_disposition')}),
        ('Leads',       {'fields': ('max_attempts', 'retry_delay_minutes',
                                    'use_system_dnc', 'use_campaign_dnc')}),
        ('Dispositions', {'fields': ('dispositions',)}),
        ('Live Stats',  {'fields': ('stat_calls_today', 'stat_answered_today',
                                    'stat_abandon_rate', 'stat_agents_active')}),
    )

    actions = ['start_campaigns', 'pause_campaigns', 'stop_campaigns']

    @admin.action(description='Start selected campaigns')
    def start_campaigns(self, request, qs):
        for c in qs:
            c.start()

    @admin.action(description='Pause selected campaigns')
    def pause_campaigns(self, request, qs):
        for c in qs:
            c.pause()

    @admin.action(description='Stop selected campaigns')
    def stop_campaigns(self, request, qs):
        for c in qs:
            c.stop()


@admin.register(HopperEntry)
class HopperEntryAdmin(admin.ModelAdmin):
    list_display  = ('campaign', 'phone_number', 'status', 'attempt_number', 'queued_at', 'dialed_at')
    list_filter   = ('status', 'campaign')
    search_fields = ('phone_number',)
    readonly_fields = ('queued_at', 'dialed_at', 'completed_at')


@admin.register(DNCEntry)
class DNCEntryAdmin(admin.ModelAdmin):
    list_display  = ('phone_number', 'campaign', 'added_by', 'reason', 'created_at')
    list_filter   = ('campaign',)
    search_fields = ('phone_number', 'reason')
