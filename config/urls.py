# config/urls.py
from django.contrib import admin
from django.urls import path
from calls import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/call-queue/', views.call_queue_list, name='call_queue_list'),
    path('api/call-queue/<int:pk>/update/', views.update_call_status, name='update_call_status'),
    path('api/reports/', views.call_reports_analytics, name='call_reports_analytics'),
    path('api/reports/export/', views.export_reports_csv, name='export_reports_csv'),
]