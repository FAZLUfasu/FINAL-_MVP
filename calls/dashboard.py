from django.urls import reverse
from django.utils import timezone

from .report import (
    get_daily_metrics,
    get_follow_up_report,
    get_most_asked_questions,
)


def dashboard_callback(request, context):
    """
    Add Telicall reporting summary data to the Unfold admin home page.
    This is read-only and does not change the live calling/audio pipeline.
    """
    today = timezone.localdate()

    try:
        daily = get_daily_metrics(today)
    except Exception:
        daily = {}

    try:
        followups = get_follow_up_report(5)
    except Exception:
        followups = []

    try:
        questions = get_most_asked_questions(days=30, limit=5)
    except Exception:
        questions = []

    context.update(
        {
            "telicall_today": today,
            "telicall_summary": {
                "new_leads": daily.get("new_leads", 0),
                "calls_attempted": daily.get("calls_attempted", 0),
                "calls_completed": daily.get("calls_completed", 0),
                "calls_failed": daily.get("calls_failed", 0),
                "interested": daily.get("interested", 0),
                "not_interested": daily.get("not_interested", 0),
                "follow_up_required": daily.get("follow_up_required", 0),
                "completion_rate": daily.get("completion_rate", 0),
                "interest_rate": daily.get("interest_rate", 0),
                "average_call_duration_display": daily.get(
                    "average_call_duration_display", "0s"
                ),
            },
            "telicall_followups": followups,
            "telicall_questions": questions,
            "telicall_reports_url": reverse(
                "admin:calls_callqueueitem_reports"
            ),
        }
    )

    return context
