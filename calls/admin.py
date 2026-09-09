from django.contrib import admin
from django.utils.html import format_html

from .models import (
    CallQueueItem,
    CallSession,
    CompanyScript,
    Contact,
    SalesInsight,
    SystemSettings,
)


# ============================================================
# CALL QUEUE ADMIN
# ============================================================

@admin.register(CallQueueItem)
class CallQueueItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "phone_number",
        "status",
        "formatted_duration",
        "top_question",
        "updated_at",
    )

    list_editable = (
        "status",
    )

    list_filter = (
        "status",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "name",
        "phone_number",
        "details",
        "top_question",
    )

    ordering = (
        "-updated_at",
    )

    actions = [
        "mark_as_pending",
        "mark_as_called",
        "mark_as_followup",
    ]

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
        queryset.update(
            status="PENDING"
        )

    @admin.action(description="Mark selected leads as CALLED")
    def mark_as_called(self, request, queryset):
        queryset.update(
            status="CALLED"
        )

    @admin.action(description="Mark selected leads as FOLLOW_UP")
    def mark_as_followup(self, request, queryset):
        queryset.update(
            status="FOLLOW_UP"
        )


# ============================================================
# COMPANY SCRIPT ADMIN
# ============================================================

@admin.register(CompanyScript)
class CompanyScriptAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "bot_name",
        "tagline",
        "active_status_badge",
        "updated_at",
    )

    list_filter = (
        "is_active",
    )

    search_fields = (
        "company_name",
        "company_description",
        "products_services",
        "target_audience",
        "frequently_asked_questions",
    )

    readonly_fields = (
        "updated_at",
    )

    fieldsets = (
        (
            "Company Identity",
            {
                "fields": (
                    "company_name",
                    "bot_name",
                    "tagline",
                    "company_description",
                    "is_active",
                ),
            },
        ),
        (
            "Products & Audience",
            {
                "fields": (
                    "target_audience",
                    "products_services",
                    "key_benefits",
                    "course_durations",
                    "pricing_information",
                ),
            },
        ),
        (
            "Contact & FAQ",
            {
                "fields": (
                    "working_hours",
                    "contact_information",
                    "frequently_asked_questions",
                ),
            },
        ),
        (
            "AI Sales Configuration",
            {
                "fields": (
                    "sales_objective",
                    "data_to_collect",
                    "ai_rules",
                ),
            },
        ),
        (
            "Call Opening & Closing",
            {
                "fields": (
                    "opening_greeting",
                    "closing_statement",
                ),
            },
        ),
        (
            "Legacy / Additional Information",
            {
                "classes": (
                    "collapse",
                ),
                "fields": (
                    "company_details",
                    "updated_at",
                ),
            },
        ),
    )

    @admin.display(description="Active Status")
    def active_status_badge(self, obj):
        if obj.is_active:
            return format_html(
                (
                    '<span style="background-color: #d1fae5; '
                    'color: #065f46; '
                    'padding: 4px 10px; '
                    'border-radius: 12px; '
                    'font-weight: 600; '
                    'font-size: 11px;">'
                    "Active"
                    "</span>"
                )
            )

        return format_html(
            (
                '<span style="background-color: #f3f4f6; '
                'color: #374151; '
                'padding: 4px 10px; '
                'border-radius: 12px; '
                'font-weight: 600; '
                'font-size: 11px;">'
                "Inactive"
                "</span>"
            )
        )


# ============================================================
# SALES INSIGHT INLINE
# ============================================================

class SalesInsightInline(admin.StackedInline):
    model = SalesInsight

    extra = 0

    readonly_fields = (
        "processed_at",
    )

    can_delete = False


# ============================================================
# CALL SESSION ADMIN
# ============================================================

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

    list_filter = (
        "status",
        "created_at",
    )

    search_fields = (
        "contact__phone_number",
        "contact__name",
    )

    readonly_fields = (
        "audio_players",
    )

    ordering = (
        "-created_at",
    )

    inlines = [
        SalesInsightInline,
    ]

    @admin.display(description="Duration")
    def formatted_duration(self, obj):
        try:
            duration = int(
                obj.duration_seconds
            )
        except (ValueError, TypeError):
            duration = 0

        minutes, seconds = divmod(
            duration,
            60,
        )

        return f"{minutes}m {seconds}s"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "queued": "#d97706",
            "active": "#3b82f6",
            "completed": "#10b981",
            "failed": "#ef4444",
        }

        status = str(
            obj.status
        ).lower()

        bg_color = colors.get(
            status,
            "#6b7280",
        )

        return format_html(
            (
                '<span style="background-color: {}; '
                'color: #ffffff; '
                'padding: 3px 8px; '
                'border-radius: 6px; '
                'font-size: 11px; '
                'font-weight: 600; '
                'text-transform: uppercase;">'
                "{}"
                "</span>"
            ),
            bg_color,
            obj.status,
        )

    @admin.display(description="Audio Recordings")
    def audio_players(self, obj):
        html = []

        # --------------------------------------------
        # CUSTOMER AUDIO
        # --------------------------------------------

        if obj.recording_file:
            try:
                customer_url = (
                    obj.recording_file.url
                )

                html.append(
                    (
                        '<div style="margin-bottom: 8px;">'
                        '<span style="font-size: 11px; '
                        'color: #4b5563; '
                        'font-weight: 600;">'
                        "Customer Audio:"
                        "</span><br>"
                        '<audio controls preload="none" '
                        'style="height: 30px; '
                        'max-width: 220px;">'
                        f'<source src="{customer_url}" '
                        'type="audio/wav">'
                        "</audio>"
                        "</div>"
                    )
                )

            except Exception:
                html.append(
                    (
                        '<div style="margin-bottom: 8px;">'
                        '<span style="color: #dc2626; '
                        'font-size: 11px;">'
                        "Customer recording unavailable"
                        "</span>"
                        "</div>"
                    )
                )

        # --------------------------------------------
        # AI AUDIO
        # --------------------------------------------

        if obj.ai_recording_file:
            try:
                ai_url = (
                    obj.ai_recording_file.url
                )

                html.append(
                    (
                        "<div>"
                        '<span style="font-size: 11px; '
                        'color: #4b5563; '
                        'font-weight: 600;">'
                        "AI Response:"
                        "</span><br>"
                        '<audio controls preload="none" '
                        'style="height: 30px; '
                        'max-width: 220px;">'
                        f'<source src="{ai_url}" '
                        'type="audio/wav">'
                        "</audio>"
                        "</div>"
                    )
                )

            except Exception:
                html.append(
                    (
                        "<div>"
                        '<span style="color: #dc2626; '
                        'font-size: 11px;">'
                        "AI recording unavailable"
                        "</span>"
                        "</div>"
                    )
                )

        if html:
            return format_html(
                "".join(html)
            )

        return format_html(
            (
                '<span style="color: #9ca3af; '
                'font-style: italic;">'
                "No Recordings"
                "</span>"
            )
        )


# ============================================================
# CONTACT ADMIN
# ============================================================

@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "phone_number",
        "uploaded_at",
    )

    search_fields = (
        "name",
        "phone_number",
    )

    ordering = (
        "-uploaded_at",
    )


# ============================================================
# SALES INSIGHT ADMIN
# ============================================================

@admin.register(SalesInsight)
class SalesInsightAdmin(admin.ModelAdmin):
    list_display = (
        "call_session",
        "followup_badge",
        "processed_at",
    )

    list_filter = (
        "needs_followup",
        "processed_at",
    )

    readonly_fields = (
        "processed_at",
    )

    @admin.display(description="Needs Followup")
    def followup_badge(self, obj):
        if obj.needs_followup:
            return format_html(
                (
                    '<span style="background-color: #fef2f2; '
                    'color: #991b1b; '
                    'border: 1px solid #fecaca; '
                    'padding: 3px 8px; '
                    'border-radius: 12px; '
                    'font-weight: 600; '
                    'font-size: 11px;">'
                    "Action Required"
                    "</span>"
                )
            )

        return format_html(
            (
                '<span style="background-color: #f0fdf4; '
                'color: #166534; '
                'border: 1px solid #bbf7d0; '
                'padding: 3px 8px; '
                'border-radius: 12px; '
                'font-weight: 600; '
                'font-size: 11px;">'
                "Resolved"
                "</span>"
            )
        )


# ============================================================
# TELICALL SYSTEM SETTINGS ADMIN
# ============================================================

@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "retention_period",
        "cleanup_status",
        "updated_at",
    )

    readonly_fields = (
        "updated_at",
    )

    fieldsets = (
        (
            "Call Recording Cleanup",
            {
                "fields": (
                    "automatic_recording_cleanup",
                    "recording_retention_days",
                ),
                "description": (
                    "Configure automatic deletion of old Telicall "
                    "customer recordings and AI response recordings. "
                    "CallSession data, duration, status, contact information "
                    "and sales insights will not be deleted."
                ),
            },
        ),
        (
            "System Information",
            {
                "classes": (
                    "collapse",
                ),
                "fields": (
                    "updated_at",
                ),
            },
        ),
    )

    @admin.display(description="Retention Period")
    def retention_period(self, obj):
        return (
            f"{obj.recording_retention_days} "
            f"{'Day' if obj.recording_retention_days == 1 else 'Days'}"
        )

    @admin.display(description="Automatic Cleanup")
    def cleanup_status(self, obj):
        if obj.automatic_recording_cleanup:
            return format_html(
                (
                    '<span style="background-color: #d1fae5; '
                    'color: #065f46; '
                    'padding: 4px 10px; '
                    'border-radius: 12px; '
                    'font-weight: 600; '
                    'font-size: 11px;">'
                    "Enabled"
                    "</span>"
                )
            )

        return format_html(
            (
                '<span style="background-color: #fef2f2; '
                'color: #991b1b; '
                'padding: 4px 10px; '
                'border-radius: 12px; '
                'font-weight: 600; '
                'font-size: 11px;">'
                "Disabled"
                "</span>"
            )
        )

    def has_add_permission(
        self,
        request,
    ):
        # Only one global SystemSettings record
        # is allowed.
        return not SystemSettings.objects.exists()

    def has_delete_permission(
        self,
        request,
        obj=None,
    ):
        # Prevent accidental removal of the
        # global Telicall settings.
        return False