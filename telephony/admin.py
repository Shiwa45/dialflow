# telephony/admin.py
from django.contrib import admin
from django.utils.html import format_html
from .models import AsteriskServer, Carrier, Phone


@admin.register(AsteriskServer)
class AsteriskServerAdmin(admin.ModelAdmin):
    list_display  = ('name', 'server_ip', 'connection_badge', 'last_connected', 'is_active')
    list_filter   = ('is_active', 'connection_status')
    search_fields = ('name', 'server_ip')
    readonly_fields = ('connection_status', 'last_connected', 'created_at', 'updated_at')

    fieldsets = (
        ('Identity',  {'fields': ('name', 'description', 'server_ip', 'is_active')}),
        ('ARI',       {'fields': ('ari_host', 'ari_port', 'ari_username', 'ari_password', 'ari_app_name')}),
        ('AMI',       {'fields': ('ami_host', 'ami_port', 'ami_username', 'ami_password')}),
        ('Status',    {'fields': ('connection_status', 'last_connected')}),
    )

    @admin.display(description='Connection')
    def connection_badge(self, obj):
        colors = {'connected': '#10B981', 'disconnected': '#EF4444',
                  'error': '#F59E0B', 'unknown': '#9CA3AF'}
        color = colors.get(obj.connection_status, '#9CA3AF')
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:5px">'
            '<span style="width:8px;height:8px;border-radius:50%;background:{}"></span>{}'
            '</span>',
            color, obj.get_connection_status_display()
        )


@admin.register(Carrier)
class CarrierAdmin(admin.ModelAdmin):
    list_display  = ('name', 'host', 'protocol', 'max_channels', 'is_active', 'asterisk_server')
    list_filter   = ('is_active', 'protocol', 'asterisk_server')
    search_fields = ('name', 'host')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    list_display  = ('extension', 'name', 'phone_type', 'user', 'asterisk_server',
                     'is_active', 'last_registered')
    list_filter   = ('phone_type', 'is_active', 'asterisk_server')
    search_fields = ('extension', 'name', 'user__username')
    readonly_fields = ('last_registered', 'last_ip', 'created_at', 'updated_at')

    fieldsets = (
        ('Extension', {'fields': ('extension', 'name', 'phone_type', 'user', 'asterisk_server')}),
        ('SIP Config', {'fields': ('secret', 'context', 'allow_codecs')}),
        ('Status',     {'fields': ('is_active', 'last_registered', 'last_ip')}),
    )

    actions = ['sync_to_asterisk', 'remove_from_asterisk']

    @admin.action(description='Sync selected phones to Asterisk realtime')
    def sync_to_asterisk(self, request, queryset):
        synced = 0
        for phone in queryset.filter(is_active=True):
            try:
                phone.sync_to_asterisk()
                synced += 1
            except Exception as e:
                self.message_user(request, f'Failed {phone.extension}: {e}', level='error')
        self.message_user(request, f'Synced {synced} phones.')

    @admin.action(description='Remove selected phones from Asterisk realtime')
    def remove_from_asterisk(self, request, queryset):
        for phone in queryset:
            phone.remove_from_asterisk()
        self.message_user(request, f'Removed {queryset.count()} phones.')
