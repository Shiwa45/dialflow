# leads/admin.py
from django.contrib import admin
from .models import Lead, LeadAttempt, LeadNote


class LeadAttemptInline(admin.TabularInline):
    model       = LeadAttempt
    extra       = 0
    readonly_fields = ('campaign', 'attempt_number', 'phone_number', 'result', 'attempted_at')
    can_delete  = False


class LeadNoteInline(admin.TabularInline):
    model       = LeadNote
    extra       = 0
    readonly_fields = ('agent', 'campaign', 'created_at')


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display  = ('full_name', 'primary_phone', 'email', 'city', 'is_active', 'do_not_call', 'priority', 'created_at')
    list_filter   = ('is_active', 'do_not_call', 'country')
    search_fields = ('first_name', 'last_name', 'primary_phone', 'email', 'external_id')
    readonly_fields = ('created_at', 'updated_at')
    inlines       = [LeadAttemptInline, LeadNoteInline]
    filter_horizontal = ('campaigns',)

    @admin.display(description='Name')
    def full_name(self, obj):
        return obj.full_name

    actions = ['mark_dnc', 'mark_active']

    @admin.action(description='Mark selected leads as DNC')
    def mark_dnc(self, request, queryset):
        queryset.update(do_not_call=True)

    @admin.action(description='Mark selected leads as active')
    def mark_active(self, request, queryset):
        queryset.update(is_active=True, do_not_call=False)
