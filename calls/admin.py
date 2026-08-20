from django.contrib import admin
from django.utils.html import format_html
from .models import CallQueueItem, CallSession, CompanyScript, Contact, SalesInsight


@admin.register(CallQueueItem)
class CallQueueItemAdmin(admin.ModelAdmin):
    list_display = (
        'id', 
        'name', 
        'phone_number', 
        'status', 
        'call_duration_seconds', 
        'top_question', 
        'updated_at'
    )
    list_filter = ('status', 'created_at', 'updated_at')
    search_fields = ('name', 'phone_number', 'details', 'top_question')
    list_editable = ('status',)
    ordering = ('-updated_at',)
    actions = ['mark_as_pending', 'mark_as_called', 'mark_as_followup']

    @admin.action(description="Mark selected leads as PENDING")
    def mark_as_pending(self, request, queryset):
        queryset.update(status='PENDING')

    @admin.action(description="Mark selected leads as CALLED")
    def mark_as_called(self, request, queryset):
        queryset.update(status='CALLED')

    @admin.action(description="Mark selected leads as FOLLOW_UP")
    def mark_as_followup(self, request, queryset):
        queryset.update(status='FOLLOW_UP')


@admin.register(CompanyScript)
class CompanyScriptAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'bot_name', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('company_name', 'company_details')


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "contact",
        "formatted_duration",
        "status",
        "audio_players",
        "created_at",
    )
    list_filter = ('status', 'created_at')
    search_fields = ('contact__phone_number', 'contact__name')
    readonly_fields = ("audio_players",)

    def formatted_duration(self, obj):
        try:
            duration = int(obj.duration_seconds)
        except (ValueError, TypeError):
            duration = 0
        minutes, seconds = divmod(duration, 60)
        return f'{minutes}m {seconds}s'

    formatted_duration.short_description = 'Duration'

    def audio_players(self, obj):
        html = []
        if obj.recording_file:
            html.append(
                f'<div><strong>Customer:</strong><br><audio controls preload="none"'
                f' style="max-width: 200px;"><source src="{obj.recording_file.url}"'
                ' type="audio/wav"></audio></div>'
            )
        if obj.ai_recording_file:
            html.append(
                '<div style="margin-top: 6px;"><strong>AI Response:</strong><br>'
                f'<audio controls preload="none" style="max-width: 200px;">'
                f'<source src="{obj.ai_recording_file.url}" type="audio/wav"></audio></div>'
            )
        return format_html("".join(html)) if html else "No Recordings"

    audio_players.short_description = "Audio Recordings"


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone_number', 'uploaded_at')
    search_fields = ('name', 'phone_number')


@admin.register(SalesInsight)
class SalesInsightAdmin(admin.ModelAdmin):
    list_display = ('call_session', 'needs_followup', 'processed_at')
    list_filter = ('needs_followup',)