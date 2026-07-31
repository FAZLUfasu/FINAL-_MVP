# calls/admin.py
from django.contrib import admin
from .models import CallQueueItem

@admin.register(CallQueueItem)
class CallQueueItemAdmin(admin.ModelAdmin):
    # Columns displayed in the Django Admin list view
    list_display = (
        'id', 
        'name', 
        'phone_number', 
        'status', 
        'call_duration_seconds', 
        'top_question', 
        'updated_at'
    )
    
    # Filter sidebar in Django Admin
    list_filter = ('status', 'created_at', 'updated_at')
    
    # Search box for fast lookups
    search_fields = ('name', 'phone_number', 'details', 'top_question')
    
    # Enable inline editing of lead status directly from the list table
    list_editable = ('status',)
    
    # Default ordering
    ordering = ('-updated_at',)

    # Custom Admin Bulk Actions
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