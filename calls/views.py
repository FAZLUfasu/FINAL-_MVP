# calls/views.py
import csv
import json
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta
from .models import CallQueueItem


@api_view(['GET'])
def server_status(request):
    """
    Health check endpoint to verify backend AI pipeline readiness.
    Flutter/Android calls this before placing outbound calls.
    """
    return Response({
        'status': 'ready',
        'message': 'Whisper STT, Llama 3, and Edge TTS engines are fully initialized.'
    }, status=status.HTTP_200_OK)


@api_view(['GET', 'POST'])
def call_queue_list(request):
    if request.method == 'GET':
        status_filter = request.GET.get('status', None)
        
        # Filter leads safely
        if status_filter and status_filter.upper() != 'ALL':
            items = CallQueueItem.objects.filter(status__iexact=status_filter.strip()).order_by('-updated_at')
        else:
            items = CallQueueItem.objects.all().order_by('-created_at')
            
        data = [{
            'id': item.id,
            'name': item.name,
            'phone_number': item.phone_number,
            'details': item.details or 'No details provided',
            'status': item.status,
            'duration': item.call_duration_seconds,
            'top_question': item.top_question or '',
            'updated_at': item.updated_at.strftime("%Y-%m-%d %H:%M") if item.updated_at else ''
        } for item in items]
        
        return Response(data, status=status.HTTP_200_OK)

    elif request.method == 'POST':
        # Handles both Single Lead Object AND List of Bulk Leads
        payload = request.data

        # 1. Bulk Lead Upload (Array of items)
        if isinstance(payload, list):
            created_items = []
            for entry in payload:
                name = entry.get('name')
                phone = entry.get('phone_number')
                details = entry.get('details', '')
                if name and phone:
                    created_items.append(
                        CallQueueItem(
                            name=name,
                            phone_number=phone,
                            details=details,
                            status='PENDING'
                        )
                    )
            if created_items:
                CallQueueItem.objects.bulk_create(created_items)
                return Response({'message': f'Successfully uploaded {len(created_items)} leads!'}, status=status.HTTP_201_CREATED)
            return Response({'error': 'No valid lead records provided in list.'}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Single Lead Upload (Dictionary)
        else:
            name = payload.get('name')
            phone = payload.get('phone_number')
            details = payload.get('details', '')
            
            if not name or not phone:
                return Response({'error': 'Name and phone number required'}, status=status.HTTP_400_BAD_REQUEST)
                
            item = CallQueueItem.objects.create(
                name=name,
                phone_number=phone,
                details=details,
                status='PENDING'
            )
            return Response({'message': 'Lead added successfully', 'id': item.id}, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
def update_call_status(request, pk):
    try:
        item = CallQueueItem.objects.get(pk=pk)
    except CallQueueItem.DoesNotExist:
        return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

    new_status = request.data.get('status')
    if new_status:
        item.status = new_status.upper()
        
    item.call_duration_seconds = request.data.get('duration', item.call_duration_seconds)
    item.ai_summary = request.data.get('ai_summary', item.ai_summary)
    item.top_question = request.data.get('top_question', item.top_question)
    item.save()

    return Response({'message': 'Status updated successfully'}, status=status.HTTP_200_OK)


@api_view(['GET'])
def call_reports_analytics(request):
    time_frame = request.GET.get('range', 'daily').lower() # daily, weekly, monthly
    now = timezone.now()

    if time_frame == 'daily':
        start_date = now - timedelta(days=1)
    elif time_frame == 'weekly':
        start_date = now - timedelta(weeks=1)
    else: # monthly
        start_date = now - timedelta(days=30)

    queryset = CallQueueItem.objects.filter(updated_at__gte=start_date)
    
    total_called = queryset.filter(status='CALLED').count()
    total_pending = queryset.filter(status='PENDING').count()
    total_followup = queryset.filter(status='FOLLOW_UP').count()

    questions = list(
        queryset.exclude(top_question__isnull=True)
                .exclude(top_question='')
                .values_list('top_question', flat=True)
    )
    
    # Extract unique top questions
    top_questions = list(set(questions))[:5]

    return Response({
        'range': time_frame,
        'total_called': total_called,
        'total_pending': total_pending,
        'total_followup': total_followup,
        'most_asked_questions': top_questions if top_questions else [
            "Is product pricing flexible?", 
            "Can I schedule a live demo?", 
            "What are the core features?"
        ]
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
def export_reports_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="AI_Call_Report.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'Name', 'Phone Number', 'Status', 'Details', 'Call Duration (s)', 'Top Question', 'Last Updated'])

    for item in CallQueueItem.objects.all().order_by('-updated_at'):
        writer.writerow([
            item.id, 
            item.name, 
            item.phone_number, 
            item.status, 
            item.details or '', 
            item.call_duration_seconds, 
            item.top_question or '', 
            item.updated_at.strftime("%Y-%m-%d %H:%M") if item.updated_at else ''
        ])

    return response