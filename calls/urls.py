from django.urls import path

from .views import (
    ai_test_console,
    ai_test_respond,
    call_queue_list,
    call_reports_analytics,
    export_reports_csv,
    home,
    server_status,
    update_call_status,
)

urlpatterns = [
    path('', home, name='home'),

    path(
        'api/status/',
        server_status,
        name='server_status',
    ),

    path(
        'api/call-queue/',
        call_queue_list,
        name='call_queue_list',
    ),

    path(
        'api/call-queue/<int:pk>/update/',
        update_call_status,
        name='update_call_status',
    ),

    path(
        'api/reports/',
        call_reports_analytics,
        name='call_reports_analytics',
    ),

    path(
        'api/reports/export/',
        export_reports_csv,
        name='export_reports_csv',
    ),

    path(
        'ai-test/',
        ai_test_console,
        name='ai_test_console',
    ),

    path(
        'api/ai-test/respond/',
        ai_test_respond,
        name='ai_test_respond',
    ),
]