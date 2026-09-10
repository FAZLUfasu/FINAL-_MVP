# calls/views.py
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta
from asgiref.sync import async_to_sync
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import CallQueueItem, CompanyScript, SystemSettings
from .conversation_engine import generate_conversation_turn
from .report import (
    generate_daily_report,
    generate_monthly_report,
    get_follow_up_report,
    get_most_asked_questions,
    get_previous_days_report,
    get_report_payload,
)


@api_view(['GET'])
def server_status(request):
    """
    Health check endpoint to verify backend AI pipeline readiness.
    Flutter/Android calls this before placing outbound calls.
    """
    return Response({
        'status': 'ready',
        'message': (
            'Whisper STT, Llama 3, and TTS engines are initialized.'
        )
    }, status=status.HTTP_200_OK)


@api_view(['GET', 'POST'])
def call_queue_list(request):
    if request.method == 'GET':
        status_filter = request.GET.get('status', None)

        if status_filter and status_filter.upper() != 'ALL':
            items = (
                CallQueueItem.objects
                .filter(status__iexact=status_filter.strip())
                .order_by('-updated_at')
            )
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
            'updated_at': (
                item.updated_at.strftime("%Y-%m-%d %H:%M")
                if item.updated_at else ''
            )
        } for item in items]

        return Response(data, status=status.HTTP_200_OK)

    payload = request.data

    # Bulk lead upload
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
            return Response({
                'message': (
                    f'Successfully uploaded {len(created_items)} leads!'
                )
            }, status=status.HTTP_201_CREATED)

        return Response({
            'error': 'No valid lead records provided in list.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Single lead upload
    name = payload.get('name')
    phone = payload.get('phone_number')
    details = payload.get('details', '')

    if not name or not phone:
        return Response({
            'error': 'Name and phone number required'
        }, status=status.HTTP_400_BAD_REQUEST)

    item = CallQueueItem.objects.create(
        name=name,
        phone_number=phone,
        details=details,
        status='PENDING'
    )

    return Response({
        'message': 'Lead added successfully',
        'id': item.id
    }, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
def update_call_status(request, pk):
    try:
        item = CallQueueItem.objects.get(pk=pk)
    except CallQueueItem.DoesNotExist:
        return Response(
            {'error': 'Item not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    new_status = request.data.get('status')

    if new_status:
        item.status = new_status.upper()

    item.call_duration_seconds = request.data.get(
        'duration',
        item.call_duration_seconds
    )

    item.ai_summary = request.data.get(
        'ai_summary',
        item.ai_summary
    )

    item.top_question = request.data.get(
        'top_question',
        item.top_question
    )

    item.save()

    return Response(
        {'message': 'Status updated successfully'},
        status=status.HTTP_200_OK
    )



@api_view(['GET'])
def export_reports_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        'attachment; filename="AI_Call_Report.csv"'
    )

    writer = csv.writer(response)

    writer.writerow([
        'ID',
        'Name',
        'Phone Number',
        'Status',
        'Details',
        'Call Duration (s)',
        'Top Question',
        'Last Updated'
    ])

    for item in CallQueueItem.objects.all().order_by('-updated_at'):
        writer.writerow([
            item.id,
            item.name,
            item.phone_number,
            item.status,
            item.details or '',
            item.call_duration_seconds,
            item.top_question or '',
            (
                item.updated_at.strftime("%Y-%m-%d %H:%M")
                if item.updated_at else ''
            )
        ])

    return response


def home(request):
    return render(request, "index.html")

# ---------------------------------------------------------------------------
# INTERNAL AI TEST CONSOLE
# ---------------------------------------------------------------------------

AI_TEST_MODEL = os.getenv(
    "TELICALL_TEST_LLM_MODEL",
    "llama3.2",
).strip()


def _compact_text(value, limit=500):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _get_active_company_config():
    script = CompanyScript.objects.filter(is_active=True).first()

    if not script:
        return None

    if hasattr(script, "build_ai_knowledge"):
        details = script.build_ai_knowledge()
    else:
        details = script.company_details

    return {
        "id": script.id,
        "company": script.company_name or "Brainex AI Institute",
        "bot_name": script.bot_name or "AI Assistant",
        "details": details or "",
        "greeting": script.opening_greeting or "",
        "closing": script.closing_statement or "",
        "followup_message": getattr(
            script,
            "human_followup_message",
            "",
        ) or "",
        "default_language": getattr(
            script,
            "default_language",
            "English",
        ) or "English",
    }


def _default_test_profile():
    return {
        "customer_type": None,
        "education": None,
        "occupation": None,
        "interest_area": None,
        "course_interest": None,
        "interest_status": "UNDECIDED",
        "needs_more_information": False,
        "ready_to_join": False,
        "callback_requested": False,
    }


@ensure_csrf_cookie
def ai_test_console(request):
    config = _get_active_company_config()

    return render(
        request,
        "ai_test_console.html",
        {
            "active_company": (
                config["company"]
                if config
                else "No active CompanyScript"
            ),
            "active_bot": (
                config["bot_name"]
                if config
                else "Not configured"
            ),
            "opening_greeting": (
                config["greeting"]
                if config and config.get("greeting")
                else (
                    "Hello, I'm the AI calling assistant from "
                    "Brainex AI Institute. We provide practical AI "
                    "training programs. May I briefly tell you about them?"
                )
            ),
        },
    )


@api_view(["POST"])
def ai_test_respond(request):
    """
    Typed simulation using the SAME conversation_engine.py as the live call.

    The browser must send back these values on every turn:
      - stage
      - customer_profile
      - history
    """

    config = _get_active_company_config()

    if not config:
        return Response(
            {
                "error": (
                    "No active CompanyScript found. "
                    "Create or activate one in Django Admin."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    message = _compact_text(
        request.data.get("message", ""),
        1200,
    )

    if not message:
        return Response(
            {"error": "Message is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw_lead = request.data.get("lead", {})
    if not isinstance(raw_lead, dict):
        raw_lead = {}

    lead_name = _compact_text(
        raw_lead.get("name", ""),
        150,
    )
    lead_phone = _compact_text(
        raw_lead.get("phone_number", ""),
        50,
    )
    lead_details = _compact_text(
        raw_lead.get("details", ""),
        1200,
    )

    if not lead_name:
        return Response(
            {"error": "Lead name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not lead_phone:
        return Response(
            {"error": "Lead mobile number is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    allowed_stages = {
        "WAIT_PERMISSION",
        "PROFILE_COLLECTION",
        "NEED_DISCOVERY",
        "PRODUCT_EXPLANATION",
        "DETAIL_COLLECTION",
        "INTEREST_CHECK",
        "FOLLOW_UP",
        "CLOSING",
        "FINISHED",
    }

    stage_value = _compact_text(
        request.data.get(
            "stage",
            "WAIT_PERMISSION",
        ),
        80,
    ).upper()

    if stage_value not in allowed_stages:
        stage_value = "WAIT_PERMISSION"

    raw_profile = request.data.get(
        "customer_profile",
        {},
    )

    customer_profile = _default_test_profile()

    if isinstance(raw_profile, dict):
        for key in customer_profile:
            if key in raw_profile:
                customer_profile[key] = raw_profile[key]

    raw_history = request.data.get(
        "history",
        [],
    )

    safe_history = []

    if isinstance(raw_history, list):
        for item in raw_history[-20:]:
            if not isinstance(item, dict):
                continue

            role = str(
                item.get("role", "")
            ).strip().lower()

            text = _compact_text(
                item.get("text", ""),
                700,
            )

            if role in {"user", "assistant"} and text:
                safe_history.append(
                    {
                        "role": role,
                        "text": text,
                    }
                )

    start_time = time.perf_counter()

    try:
        result = async_to_sync(
            generate_conversation_turn
        )(
            company_config=config,
            lead={
                "name": lead_name,
                "phone_number": lead_phone,
                "details": lead_details,
            },
            history=safe_history,
            customer_profile=customer_profile,
            stage=stage_value,
            user_text=message,
            model=AI_TEST_MODEL,
        )

        elapsed = time.perf_counter() - start_time

        if not isinstance(result, dict):
            raise ValueError(
                "Conversation engine returned an invalid result."
            )

        reply = _compact_text(
            result.get("reply", ""),
            700,
        ) or "Could you please repeat that?"

        next_stage = _compact_text(
            result.get(
                "next_stage",
                stage_value,
            ),
            80,
        ).upper()

        if next_stage not in allowed_stages:
            next_stage = stage_value

        returned_profile = result.get(
            "customer_profile",
            customer_profile,
        )

        if not isinstance(
            returned_profile,
            dict,
        ):
            returned_profile = customer_profile

        return Response(
            {
                "company": config["company"],
                "bot_name": config["bot_name"],
                "lead": {
                    "name": lead_name,
                    "phone_number": lead_phone,
                    "details": lead_details,
                },
                "reply": reply,

                # CRITICAL state returned to the browser.
                "stage": next_stage,
                "customer_profile": returned_profile,
                "interest_status": result.get(
                    "interest_status",
                    returned_profile.get(
                        "interest_status",
                        "UNDECIDED",
                    ),
                ),
                "needs_more_information": bool(
                    result.get(
                        "needs_more_information",
                        returned_profile.get(
                            "needs_more_information",
                            False,
                        ),
                    )
                ),
                "ready_to_join": bool(
                    result.get(
                        "ready_to_join",
                        returned_profile.get(
                            "ready_to_join",
                            False,
                        ),
                    )
                ),
                "callback_requested": bool(
                    result.get(
                        "callback_requested",
                        returned_profile.get(
                            "callback_requested",
                            False,
                        ),
                    )
                ),
                "response_time_seconds": round(
                    elapsed,
                    2,
                ),
                "model": AI_TEST_MODEL,
            },
            status=status.HTTP_200_OK,
        )

    except Exception as exc:
        return Response(
            {
                "error": (
                    "Conversation test failed: "
                    f"{type(exc).__name__}: {str(exc)}"
                )
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@require_http_methods(["GET", "PUT", "POST"])
def system_settings_view(request):
    settings_obj = SystemSettings.get_settings()

    if request.method == "GET":
        return JsonResponse({
            "success": True,
            "recording_retention_days":
                settings_obj.recording_retention_days,
            "automatic_recording_cleanup":
                settings_obj.automatic_recording_cleanup,
        })

    try:
        data = json.loads(
            request.body.decode("utf-8")
        )

        if "recording_retention_days" in data:
            days = int(
                data["recording_retention_days"]
            )

            if days < 1 or days > 365:
                return JsonResponse(
                    {
                        "success": False,
                        "message":
                            "Retention must be between 1 and 365 days.",
                    },
                    status=400,
                )

            settings_obj.recording_retention_days = days

        if "automatic_recording_cleanup" in data:
            settings_obj.automatic_recording_cleanup = bool(
                data["automatic_recording_cleanup"]
            )

        settings_obj.save()

        return JsonResponse({
            "success": True,
            "message": "Settings saved successfully.",
            "recording_retention_days":
                settings_obj.recording_retention_days,
            "automatic_recording_cleanup":
                settings_obj.automatic_recording_cleanup,
        })

    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
            },
            status=400,
        )


@api_view(["GET"])
def call_reports_analytics(request):
    """
    Telicall reporting API.

    Examples:
        /api/reports/?range=daily
        /api/reports/?range=daily&date=2026-09-10
        /api/reports/?range=weekly
        /api/reports/?range=monthly
        /api/reports/?range=monthly&year=2026&month=9
        /api/reports/?range=previous&days=30
        /api/reports/?range=followup
        /api/reports/?range=questions&days=30
    """
    report_range = str(request.GET.get("range", "daily") or "daily").strip().lower()

    report_date = None
    raw_date = str(request.GET.get("date", "") or "").strip()
    if raw_date:
        try:
            report_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"error": "Invalid date. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    year = request.GET.get("year")
    month = request.GET.get("month")
    days = request.GET.get("days", 30)

    try:
        year = int(year) if year else None
        month = int(month) if month else None
        days = max(1, min(int(days), 365))

        payload = get_report_payload(
            report_range,
            report_date=report_date,
            year=year,
            month=month,
            days=days,
        )

        return Response(payload, status=status.HTTP_200_OK)

    except ValueError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    except Exception as exc:
        return Response(
            {
                "error": "Unable to generate Telicall report.",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@staff_member_required
def telicall_reports_dashboard(request):
    """Staff-only visual Telicall reporting dashboard."""
    today = timezone.localdate()

    try:
        selected_year = int(request.GET.get("year", today.year))
        selected_month = int(request.GET.get("month", today.month))
    except (TypeError, ValueError):
        selected_year = today.year
        selected_month = today.month

    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    daily = generate_daily_report(today)
    monthly = generate_monthly_report(selected_year, selected_month)
    previous_days = get_previous_days_report(14)
    followups = get_follow_up_report(100)
    questions = get_most_asked_questions(days=30, limit=15)

    # Get the full Django / Unfold admin context
    admin_context = admin.site.each_context(request)

    context = {
        **admin_context,

        "title": "Telicall Reports",
        "today": today,
        "daily": daily,
        "monthly": monthly,
        "previous_days": previous_days,
        "followups": followups,
        "questions": questions,
        "selected_year": selected_year,
        "selected_month": selected_month,
        "has_permission": True,

        # Helps Unfold associate this page with the Calls app/model
        "opts": CallQueueItem._meta,
    }

    return render(
        request,
        "admin/calls/telicall_reports_dashboard.html",
        context,
    )