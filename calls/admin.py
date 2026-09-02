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
        'formatted_duration', 
        'top_question', 
        'updated_at'
    )
    list_editable = ('status',)  # Enables direct status editing from the table view
    list_filter = ('status', 'created_at', 'updated_at')
    search_fields = ('name', 'phone_number', 'details', 'top_question')
    ordering = ('-updated_at',)
    actions = ['mark_as_pending', 'mark_as_called', 'mark_as_followup']

    @admin.display(description="Call Duration")
    def formatted_duration(self, obj):
        try:
            duration = int(obj.call_duration_seconds)
        except (ValueError, TypeError):
            duration = 0
        minutes, seconds = divmod(duration, 60)
        return f"{minutes}m {seconds}s"

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
    list_display = ('company_name', 'bot_name', 'active_status_badge', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('company_name', 'company_details')

    @admin.display(description="Active Status")
    def active_status_badge(self, obj):
        if obj.is_active:
            return format_html(
                '<span style="background-color: #d1fae5; color: #065f46; padding: 4px 10px; '
                'border-radius: 12px; font-weight: 600; font-size: 11px;">Active</span>'
            )
        return format_html(
            '<span style="background-color: #f3f4f6; color: #374151; padding: 4px 10px; '
            'border-radius: 12px; font-weight: 600; font-size: 11px;">Inactive</span>'
        )


class SalesInsightInline(admin.StackedInline):
    model = SalesInsight
    extra = 0
    readonly_fields = ('processed_at',)
    can_delete = False


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "contact",
        "formatted_duration",
        "status_badge",
        "audio_players",
        "created_at",
    )
    list_filter = ('status', 'created_at')
    search_fields = ('contact__phone_number', 'contact__name')
    readonly_fields = ("audio_players",)
    inlines = [SalesInsightInline]

    @admin.display(description="Duration")
    def formatted_duration(self, obj):
        try:
            duration = int(obj.duration_seconds)
        except (ValueError, TypeError):
            duration = 0
        minutes, seconds = divmod(duration, 60)
        return f'{minutes}m {seconds}s'

    @admin.display(description="Status")
    def status_badge(self, obj):
        # Matching lowercase status choices from CallSession model
        colors = {
            'queued': '#d97706',      # Amber
            'active': '#3b82f6',      # Blue
            'completed': '#10b981',   # Emerald Green
            'failed': '#ef4444',       # Red
        }
        bg_color = colors.get(str(obj.status).lower(), '#6b7280')
        return format_html(
            '<span style="background-color: {}; color: #ffffff; padding: 3px 8px; '
            'border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase;">{}</span>',
            bg_color,
            obj.status
        )

    @admin.display(description="Audio Recordings")
    def audio_players(self, obj):
        html = []
        if obj.recording_file:
            html.append(
                '<div style="margin-bottom: 4px;">'
                '<span style="font-size: 11px; color: #4b5563; font-weight: 600;">Customer Audio:</span><br>'
                f'<audio controls preload="none" style="height: 30px; max-width: 220px;">'
                f'<source src="{obj.recording_file.url}" type="audio/wav"></audio></div>'
            )
        if obj.ai_recording_file:
            html.append(
                '<div>'
                '<span style="font-size: 11px; color: #4b5563; font-weight: 600;">AI Response:</span><br>'
                f'<audio controls preload="none" style="height: 30px; max-width: 220px;">'
                f'<source src="{obj.ai_recording_file.url}" type="audio/wav"></audio></div>'
            )
        return format_html("".join(html)) if html else format_html('<span style="color: #9ca3af; font-style: italic;">No Recordings</span>')


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone_number', 'uploaded_at')
    search_fields = ('name', 'phone_number')


@admin.register(SalesInsight)
class SalesInsightAdmin(admin.ModelAdmin):
    list_display = ('call_session', 'followup_badge', 'processed_at')
    list_filter = ('needs_followup',)

    @admin.display(description="Needs Followup")
    def followup_badge(self, obj):
        if obj.needs_followup:
            return format_html(
                '<span style="background-color: #fef2f2; color: #991b1b; border: 1px solid #fecaca; '
                'padding: 3px 8px; border-radius: 12px; font-weight: 600; font-size: 11px;">Action Required</span>'
            )
        return format_html(
            '<span style="background-color: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; '
            'padding: 3px 8px; border-radius: 12px; font-weight: 600; font-size: 11px;">Resolved</span>'
        )