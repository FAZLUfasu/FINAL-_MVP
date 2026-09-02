# config/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from calls import views
from calls.views import home
urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/call-queue/', views.call_queue_list, name='call_queue_list'),
    path("", home, name="home"),
    

    path(
        'api/call-queue/<int:pk>/update/',
        views.update_call_status,
        name='update_call_status',
    ),
    path(
        'api/reports/',
        views.call_reports_analytics,
        name='call_reports_analytics',
    ),
    path(
        'api/reports/export/',
        views.export_reports_csv,
        name='export_reports_csv',
    ),
]

# 🟢 Serve uploaded media files (call recordings) locally during development
if settings.DEBUG:
  urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)