"""
Telicall reporting service.

Place at: calls/report.py

Generates:
- Daily report
- Previous-day comparison
- Previous 7-day lead trend / averages
- Monthly report
- Previous-month comparison
- Follow-up list
- Called / pending / all-lead lists
- Most asked questions
- Interested courses
- Customer type / education / occupation summaries
- Per-call SalesInsight detail
- Optional JSON snapshots under MEDIA_ROOT/reports/

Uses the current existing models:
    CallQueueItem, CallSession, SalesInsight

It does not modify the live call/audio pipeline.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db.models import Avg, Sum
from django.utils import timezone

from .models import CallQueueItem, CallSession, SalesInsight


INTEREST_STATUSES = {
    "UNDECIDED",
    "NOT_INTERESTED",
    "INTERESTED",
    "NEED_MORE_INFORMATION",
    "READY_TO_JOIN",
    "CALLBACK_REQUESTED",
}

FOLLOW_UP_STATUSES = {
    "INTERESTED",
    "NEED_MORE_INFORMATION",
    "READY_TO_JOIN",
    "CALLBACK_REQUESTED",
}

PRIORITIES = ("HOT", "HIGH", "MEDIUM", "LOW")

QUESTION_ALIASES = {
    "COURSE_FEE": (
        "fee", "fees", "price", "pricing", "cost", "how much", "payment",
    ),
    "COURSE_DURATION": (
        "duration", "how long", "days", "month", "months", "course length", "program length",
    ),
    "CERTIFICATE": ("certificate", "certification"),
    "CLASS_TIMING": ("timing", "class time", "schedule", "batch time", "when is class"),
    "ONLINE_TRAINING": ("online", "remote", "virtual"),
    "LOCATION": ("location", "address", "where are you", "where is", "office"),
    "COURSE_CONTENT": ("syllabus", "course content", "what do you teach", "topics", "modules"),
    "PLACEMENT": ("placement", "job", "career support"),
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, limit: Optional[int] = None) -> str:
    text_value = re.sub(r"\s+", " ", str(value or "")).strip()
    return text_value[:limit] if limit is not None else text_value


def _safe_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    return []


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "y"}:
            return True
        if normalized in {"false", "no", "0", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_phone(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def percentage(part: Any, total: Any) -> float:
    part_value = _safe_float(part)
    total_value = _safe_float(total)
    if total_value <= 0:
        return 0.0
    return round((part_value / total_value) * 100.0, 2)


def percentage_change(current: Any, previous: Any) -> Dict[str, Any]:
    current_value = _safe_float(current)
    previous_value = _safe_float(previous)

    if previous_value == 0:
        if current_value == 0:
            change = 0.0
            direction = "same"
            label = "0%"
        else:
            change = None
            direction = "up"
            label = "NEW"
    else:
        change = round(((current_value - previous_value) / previous_value) * 100.0, 2)
        direction = "up" if change > 0 else "down" if change < 0 else "same"
        label = f"{change:+.2f}%"

    return {
        "current": current,
        "previous": previous,
        "change_percent": change,
        "direction": direction,
        "label": label,
    }


def format_duration(seconds: Any) -> str:
    seconds = max(0, _safe_int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _local_day_bounds(report_date: date) -> Tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start_naive = datetime.combine(report_date, time.min)
    end_naive = start_naive + timedelta(days=1)
    return timezone.make_aware(start_naive, tz), timezone.make_aware(end_naive, tz)


def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    first = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return first, next_month


def _previous_month(year: int, month: int) -> Tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _counter_rows(counter: Counter, key_name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return [{key_name: key, "count": count} for key, count in counter.most_common(limit)]


def get_insight_data(insight: SalesInsight) -> Dict[str, Any]:
    return _safe_dict(getattr(insight, "extracted_data", None))


def _insight_status(data: Dict[str, Any]) -> str:
    status = _clean_text(data.get("interest_status") or data.get("outcome") or "UNDECIDED").upper()
    return status if status in INTEREST_STATUSES else "UNDECIDED"


def _insight_priority(data: Dict[str, Any]) -> str:
    priority = _clean_text(data.get("lead_priority") or data.get("priority") or "LOW").upper()
    return priority if priority in PRIORITIES else "LOW"


def _insight_followup(insight: SalesInsight, data: Dict[str, Any]) -> bool:
    if "follow_up_required" in data:
        return _bool_value(data.get("follow_up_required"))
    if _bool_value(getattr(insight, "needs_followup", False)):
        return True
    return _insight_status(data) in FOLLOW_UP_STATUSES


def _extract_questions(data: Dict[str, Any]) -> List[str]:
    questions: List[str] = []
    for key in ("questions_asked", "customer_questions", "questions"):
        for item in _safe_list(data.get(key)):
            cleaned = _clean_text(item, 500)
            if cleaned and cleaned not in questions:
                questions.append(cleaned)

    single = _clean_text(data.get("most_asked_question") or data.get("top_question"))
    if single and single not in questions:
        questions.append(single)
    return questions


def normalize_question_category(question: str) -> str:
    cleaned = _clean_text(question).lower()
    for category, aliases in QUESTION_ALIASES.items():
        if any(alias in cleaned for alias in aliases):
            return category
    fallback = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_").upper()
    return (fallback or "OTHER")[:80]


def _extract_question_categories(data: Dict[str, Any]) -> List[str]:
    categories: List[str] = []
    for item in _safe_list(data.get("question_categories")):
        if isinstance(item, dict):
            item = item.get("category") or item.get("name") or item.get("question")
        cleaned = _clean_text(item).upper().replace(" ", "_")
        if cleaned and cleaned not in categories:
            categories.append(cleaned)

    if categories:
        return categories

    for question in _extract_questions(data):
        category = normalize_question_category(question)
        if category not in categories:
            categories.append(category)
    return categories


def _display_question_category(category: str) -> str:
    value = _clean_text(category).replace("_", " ").title()
    return {
        "Course Fee": "Course Fee",
        "Course Duration": "Course Duration",
        "Certificate": "Certificate",
        "Class Timing": "Class Timing",
        "Online Training": "Online Training",
        "Location": "Location",
        "Course Content": "Course Content",
        "Placement": "Placement / Career",
    }.get(value, value)


def _extract_course(data: Dict[str, Any]) -> str:
    return _clean_text(data.get("interested_course") or data.get("course_interest") or data.get("course"))


def _extract_customer_type(data: Dict[str, Any]) -> str:
    return _clean_text(data.get("customer_type") or data.get("lead_type")) or "Not Provided"


def _extract_education(data: Dict[str, Any]) -> str:
    return _clean_text(data.get("education")) or "Not Provided"


def _extract_occupation(data: Dict[str, Any]) -> str:
    return _clean_text(data.get("occupation")) or "Not Provided"


def _extract_name(insight: SalesInsight, data: Dict[str, Any]) -> str:
    value = _clean_text(data.get("customer_name") or data.get("name"))
    if value:
        return value
    try:
        return _clean_text(insight.call_session.contact.name) or "Unknown"
    except Exception:
        return "Unknown"


def _extract_phone(insight: SalesInsight) -> str:
    try:
        return _clean_text(insight.call_session.contact.phone_number)
    except Exception:
        return ""


def _insight_to_call_detail(insight: SalesInsight) -> Dict[str, Any]:
    data = get_insight_data(insight)
    session = insight.call_session
    status = _insight_status(data)
    priority = _insight_priority(data)
    follow_up = _insight_followup(insight, data)

    return {
        "call_session_id": session.id,
        "call_date": timezone.localtime(session.created_at).isoformat() if session.created_at else None,
        "name": _extract_name(insight, data),
        "phone_number": _extract_phone(insight),
        "education": _extract_education(data),
        "occupation": _extract_occupation(data),
        "customer_type": _extract_customer_type(data),
        "primary_intent": _clean_text(data.get("primary_intent")),
        "interest_area": _clean_text(data.get("interest_area")),
        "interested_course": _extract_course(data) or "Not Provided",
        "interest_status": status,
        "lead_priority": priority,
        "ready_to_join": _bool_value(data.get("ready_to_join")),
        "needs_more_information": _bool_value(data.get("needs_more_information")),
        "callback_requested": _bool_value(data.get("callback_requested")),
        "callback_preference": _clean_text(data.get("callback_preference") or data.get("callback_time")),
        "follow_up_required": follow_up,
        "questions_asked": _extract_questions(data),
        "question_categories": [_display_question_category(v) for v in _extract_question_categories(data)],
        "objections": [_clean_text(v) for v in _safe_list(data.get("objections")) if _clean_text(v)],
        "positive_signals": [_clean_text(v) for v in _safe_list(data.get("positive_signals")) if _clean_text(v)],
        "customer_summary": _clean_text(data.get("customer_summary") or data.get("summary")),
        "recommended_action": _clean_text(
            data.get("recommended_action") or data.get("recommended_followup") or data.get("next_action")
        ),
        "conversation_quality": _clean_text(data.get("conversation_quality")),
        "duration_seconds": _safe_int(session.duration_seconds),
        "duration_display": format_duration(session.duration_seconds),
        "customer_recording": session.recording_file.url if getattr(session, "recording_file", None) else None,
        "ai_recording": session.ai_recording_file.url if getattr(session, "ai_recording_file", None) else None,
    }


def _queue_items_for_date(report_date: date):
    start, end = _local_day_bounds(report_date)
    return CallQueueItem.objects.filter(created_at__gte=start, created_at__lt=end)


def _sessions_for_date(report_date: date):
    start, end = _local_day_bounds(report_date)
    return CallSession.objects.filter(created_at__gte=start, created_at__lt=end)


def _insights_for_date(report_date: date):
    start, end = _local_day_bounds(report_date)
    return SalesInsight.objects.filter(
        call_session__created_at__gte=start,
        call_session__created_at__lt=end,
    ).select_related("call_session", "call_session__contact")


def _queue_item_by_phone(phone_number: str) -> Optional[CallQueueItem]:
    target = _normalize_phone(phone_number)
    if not target:
        return None

    exact = CallQueueItem.objects.filter(phone_number=phone_number).order_by("-created_at").first()
    if exact is not None:
        return exact

    candidates = CallQueueItem.objects.only(
        "id", "name", "phone_number", "details", "status", "call_duration_seconds",
        "ai_summary", "top_question", "created_at", "updated_at"
    ).order_by("-created_at")[:5000]

    for item in candidates:
        if _normalize_phone(item.phone_number) == target:
            return item
    return None


def build_follow_up_lead(insight: SalesInsight) -> Dict[str, Any]:
    detail = _insight_to_call_detail(insight)
    queue_item = _queue_item_by_phone(detail["phone_number"])
    return {
        "queue_item_id": queue_item.id if queue_item else None,
        "call_session_id": detail["call_session_id"],
        "name": detail["name"] or (queue_item.name if queue_item else "Unknown"),
        "phone_number": detail["phone_number"],
        "education": detail["education"],
        "occupation": detail["occupation"],
        "customer_type": detail["customer_type"],
        "interest_area": detail["interest_area"],
        "interested_course": detail["interested_course"],
        "interest_status": detail["interest_status"],
        "lead_priority": detail["lead_priority"],
        "callback_preference": detail["callback_preference"],
        "questions_asked": detail["questions_asked"],
        "customer_summary": detail["customer_summary"],
        "recommended_action": detail["recommended_action"],
        "duration_seconds": detail["duration_seconds"],
        "duration_display": detail["duration_display"],
    }


def _aggregate_insights(insights: Iterable[SalesInsight]) -> Dict[str, Any]:
    status_counter = Counter()
    priority_counter = Counter()
    question_counter = Counter()
    original_question_counter = Counter()
    course_counter = Counter()
    customer_type_counter = Counter()
    education_counter = Counter()
    occupation_counter = Counter()
    follow_up_leads: List[Dict[str, Any]] = []
    call_details: List[Dict[str, Any]] = []
    insight_count = 0

    for insight in insights:
        insight_count += 1
        data = get_insight_data(insight)
        status = _insight_status(data)
        priority = _insight_priority(data)
        follow_up = _insight_followup(insight, data)

        status_counter[status] += 1
        priority_counter[priority] += 1

        for question in _extract_questions(data):
            original_question_counter[question] += 1

        for category in _extract_question_categories(data):
            question_counter[_display_question_category(category)] += 1

        course = _extract_course(data)
        if course:
            course_counter[course] += 1

        customer_type_counter[_extract_customer_type(data)] += 1
        education_counter[_extract_education(data)] += 1
        occupation_counter[_extract_occupation(data)] += 1

        call_details.append(_insight_to_call_detail(insight))
        if follow_up:
            follow_up_leads.append(build_follow_up_lead(insight))

    priority_rank = {"HOT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    follow_up_leads.sort(
        key=lambda row: (
            priority_rank.get(row.get("lead_priority", "LOW"), 99),
            str(row.get("name", "")).lower(),
        )
    )

    return {
        "insight_count": insight_count,
        "status_counts": {status: status_counter.get(status, 0) for status in sorted(INTEREST_STATUSES)},
        "priority_summary": {priority: priority_counter.get(priority, 0) for priority in PRIORITIES},
        "most_asked_questions": _counter_rows(question_counter, "question", 15),
        "original_questions": _counter_rows(original_question_counter, "question", 25),
        "interested_courses": _counter_rows(course_counter, "course", 15),
        "customer_types": _counter_rows(customer_type_counter, "customer_type"),
        "education_summary": _counter_rows(education_counter, "education"),
        "occupation_summary": _counter_rows(occupation_counter, "occupation"),
        "follow_up_leads": follow_up_leads,
        "call_details": call_details,
    }


def get_daily_metrics(report_date: date) -> Dict[str, Any]:
    queue_items = _queue_items_for_date(report_date)
    sessions = _sessions_for_date(report_date)
    insights = _insights_for_date(report_date)

    new_leads = queue_items.count()
    calls_attempted = sessions.count()
    calls_completed = sessions.filter(status="completed").count()
    calls_failed = sessions.filter(status="failed").count()
    calls_active = sessions.filter(status="active").count()
    calls_queued = sessions.filter(status="queued").count()

    duration = sessions.filter(status="completed").aggregate(
        total=Sum("duration_seconds"),
        average=Avg("duration_seconds"),
    )
    total_duration = _safe_int(duration.get("total"))
    average_duration = _safe_int(duration.get("average"))

    sales = _aggregate_insights(insights)
    statuses = sales["status_counts"]
    interested = statuses.get("INTERESTED", 0)
    not_interested = statuses.get("NOT_INTERESTED", 0)
    need_more_information = statuses.get("NEED_MORE_INFORMATION", 0)
    ready_to_join = statuses.get("READY_TO_JOIN", 0)
    callback_requested = statuses.get("CALLBACK_REQUESTED", 0)
    undecided = statuses.get("UNDECIDED", 0)
    follow_up_count = len(sales["follow_up_leads"])

    return {
        "date": report_date.isoformat(),
        "new_leads": new_leads,
        "new_lead_status": {
            "pending": queue_items.filter(status="PENDING").count(),
            "called": queue_items.filter(status="CALLED").count(),
            "follow_up": queue_items.filter(status="FOLLOW_UP").count(),
        },
        "queue_snapshot": {
            "total": CallQueueItem.objects.count(),
            "pending": CallQueueItem.objects.filter(status="PENDING").count(),
            "called": CallQueueItem.objects.filter(status="CALLED").count(),
            "follow_up": CallQueueItem.objects.filter(status="FOLLOW_UP").count(),
        },
        "calls_attempted": calls_attempted,
        "calls_completed": calls_completed,
        "calls_failed": calls_failed,
        "calls_active": calls_active,
        "calls_queued": calls_queued,
        "sales_insights_generated": sales["insight_count"],
        "interested": interested,
        "not_interested": not_interested,
        "need_more_information": need_more_information,
        "ready_to_join": ready_to_join,
        "callback_requested": callback_requested,
        "undecided": undecided,
        "follow_up_required": follow_up_count,
        "completion_rate": percentage(calls_completed, calls_attempted),
        "interest_rate": percentage(interested, calls_completed),
        "follow_up_rate": percentage(follow_up_count, calls_completed),
        "ready_to_join_rate": percentage(ready_to_join, calls_completed),
        "callback_rate": percentage(callback_requested, calls_completed),
        "not_interested_rate": percentage(not_interested, calls_completed),
        "total_call_duration_seconds": total_duration,
        "total_call_duration_display": format_duration(total_duration),
        "average_call_duration_seconds": average_duration,
        "average_call_duration_display": format_duration(average_duration),
        "priority_summary": sales["priority_summary"],
        "most_asked_questions": sales["most_asked_questions"],
        "original_questions": sales["original_questions"],
        "interested_courses": sales["interested_courses"],
        "customer_types": sales["customer_types"],
        "education_summary": sales["education_summary"],
        "occupation_summary": sales["occupation_summary"],
        "follow_up_leads": sales["follow_up_leads"],
        "call_details": sales["call_details"],
    }


def get_previous_days_lead_comparison(report_date: date, days: int = 7) -> List[Dict[str, Any]]:
    days = max(1, min(int(days), 60))
    rows: List[Dict[str, Any]] = []

    for offset in range(days, -1, -1):
        target_date = report_date - timedelta(days=offset)
        rows.append({
            "date": target_date.isoformat(),
            "lead_count": _queue_items_for_date(target_date).count(),
        })

    for index, row in enumerate(rows):
        row["previous_day_change"] = None if index == 0 else percentage_change(
            row["lead_count"], rows[index - 1]["lead_count"]
        )
    return rows


def get_seven_day_average(report_date: date) -> Dict[str, Any]:
    rows = [get_daily_metrics(report_date - timedelta(days=offset)) for offset in range(1, 8)]
    count = len(rows) or 1
    return {
        "average_new_leads": round(sum(r["new_leads"] for r in rows) / count, 2),
        "average_calls_attempted": round(sum(r["calls_attempted"] for r in rows) / count, 2),
        "average_calls_completed": round(sum(r["calls_completed"] for r in rows) / count, 2),
        "average_interested": round(sum(r["interested"] for r in rows) / count, 2),
        "average_follow_up_required": round(sum(r["follow_up_required"] for r in rows) / count, 2),
        "average_ready_to_join": round(sum(r["ready_to_join"] for r in rows) / count, 2),
    }


def generate_daily_report(report_date: Optional[date] = None, *, save_json: bool = True) -> Dict[str, Any]:
    report_date = report_date or timezone.localdate()
    current = get_daily_metrics(report_date)
    yesterday_date = report_date - timedelta(days=1)
    yesterday = get_daily_metrics(yesterday_date)
    seven_day = get_seven_day_average(report_date)

    report = {
        "report_type": "daily",
        "report_date": report_date.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "summary": current,
        "yesterday": {"date": yesterday_date.isoformat(), "summary": yesterday},
        "comparison_vs_yesterday": {
            "new_leads": percentage_change(current["new_leads"], yesterday["new_leads"]),
            "calls_attempted": percentage_change(current["calls_attempted"], yesterday["calls_attempted"]),
            "calls_completed": percentage_change(current["calls_completed"], yesterday["calls_completed"]),
            "interested": percentage_change(current["interested"], yesterday["interested"]),
            "follow_up_required": percentage_change(current["follow_up_required"], yesterday["follow_up_required"]),
            "ready_to_join": percentage_change(current["ready_to_join"], yesterday["ready_to_join"]),
        },
        "previous_days_lead_comparison": get_previous_days_lead_comparison(report_date, 7),
        "seven_day_average": seven_day,
        "comparison_vs_7_day_average": {
            "new_leads": percentage_change(current["new_leads"], seven_day["average_new_leads"]),
            "calls_attempted": percentage_change(current["calls_attempted"], seven_day["average_calls_attempted"]),
            "calls_completed": percentage_change(current["calls_completed"], seven_day["average_calls_completed"]),
            "interested": percentage_change(current["interested"], seven_day["average_interested"]),
        },
    }

    if save_json:
        report["json_file"] = save_report_json(
            report,
            report_type="daily",
            filename=f"{report_date.isoformat()}.json",
        )
    return report


def get_monthly_metrics(year: int, month: int) -> Dict[str, Any]:
    first_date, next_month = _month_bounds(year, month)
    start, _ = _local_day_bounds(first_date)
    end, _ = _local_day_bounds(next_month)

    queue_items = CallQueueItem.objects.filter(created_at__gte=start, created_at__lt=end)
    sessions = CallSession.objects.filter(created_at__gte=start, created_at__lt=end)
    insights = SalesInsight.objects.filter(
        call_session__created_at__gte=start,
        call_session__created_at__lt=end,
    ).select_related("call_session", "call_session__contact")

    total_leads = queue_items.count()
    calls_attempted = sessions.count()
    calls_completed = sessions.filter(status="completed").count()
    calls_failed = sessions.filter(status="failed").count()

    duration = sessions.filter(status="completed").aggregate(
        total=Sum("duration_seconds"), average=Avg("duration_seconds")
    )
    total_duration = _safe_int(duration.get("total"))
    average_duration = _safe_int(duration.get("average"))

    sales = _aggregate_insights(insights)
    statuses = sales["status_counts"]
    interested = statuses.get("INTERESTED", 0)
    not_interested = statuses.get("NOT_INTERESTED", 0)
    need_more_information = statuses.get("NEED_MORE_INFORMATION", 0)
    ready_to_join = statuses.get("READY_TO_JOIN", 0)
    callback_requested = statuses.get("CALLBACK_REQUESTED", 0)
    undecided = statuses.get("UNDECIDED", 0)
    follow_up_count = len(sales["follow_up_leads"])

    _, days_in_month = calendar.monthrange(year, month)
    daily_performance: List[Dict[str, Any]] = []
    for day_number in range(1, days_in_month + 1):
        target_date = date(year, month, day_number)
        if target_date > timezone.localdate():
            break
        metric = get_daily_metrics(target_date)
        daily_performance.append({
            "date": target_date.isoformat(),
            "new_leads": metric["new_leads"],
            "calls_attempted": metric["calls_attempted"],
            "calls_completed": metric["calls_completed"],
            "interested": metric["interested"],
            "follow_up_required": metric["follow_up_required"],
            "ready_to_join": metric["ready_to_join"],
            "completion_rate": metric["completion_rate"],
            "interest_rate": metric["interest_rate"],
        })

    return {
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "total_leads": total_leads,
        "calls_attempted": calls_attempted,
        "calls_completed": calls_completed,
        "calls_failed": calls_failed,
        "pending_new_leads": queue_items.filter(status="PENDING").count(),
        "called_new_leads": queue_items.filter(status="CALLED").count(),
        "follow_up_new_leads": queue_items.filter(status="FOLLOW_UP").count(),
        "interested": interested,
        "not_interested": not_interested,
        "need_more_information": need_more_information,
        "ready_to_join": ready_to_join,
        "callback_requested": callback_requested,
        "undecided": undecided,
        "follow_up_required": follow_up_count,
        "completion_rate": percentage(calls_completed, calls_attempted),
        "interest_rate": percentage(interested, calls_completed),
        "follow_up_rate": percentage(follow_up_count, calls_completed),
        "ready_to_join_rate": percentage(ready_to_join, calls_completed),
        "callback_rate": percentage(callback_requested, calls_completed),
        "total_call_duration_seconds": total_duration,
        "total_call_duration_display": format_duration(total_duration),
        "average_call_duration_seconds": average_duration,
        "average_call_duration_display": format_duration(average_duration),
        "priority_summary": sales["priority_summary"],
        "most_asked_questions": sales["most_asked_questions"],
        "original_questions": sales["original_questions"],
        "interested_courses": sales["interested_courses"],
        "customer_types": sales["customer_types"],
        "education_summary": sales["education_summary"],
        "occupation_summary": sales["occupation_summary"],
        "follow_up_leads": sales["follow_up_leads"],
        "daily_performance": daily_performance,
    }


def _best_day(rows: List[Dict[str, Any]], field: str) -> Optional[Dict[str, Any]]:
    return max(rows, key=lambda row: _safe_float(row.get(field))) if rows else None


def _lowest_day(rows: List[Dict[str, Any]], field: str) -> Optional[Dict[str, Any]]:
    valid = [row for row in rows if _safe_float(row.get("calls_attempted")) > 0]
    return min(valid, key=lambda row: _safe_float(row.get(field))) if valid else None


def generate_monthly_report(
    year: Optional[int] = None,
    month: Optional[int] = None,
    *,
    save_json: bool = True,
) -> Dict[str, Any]:
    today = timezone.localdate()
    year = _safe_int(year, today.year)
    month = _safe_int(month, today.month)

    if month < 1 or month > 12:
        raise ValueError("month must be between 1 and 12")

    current = get_monthly_metrics(year, month)
    previous_year, previous_month = _previous_month(year, month)
    previous = get_monthly_metrics(previous_year, previous_month)

    comparison = {
        "total_leads": percentage_change(current["total_leads"], previous["total_leads"]),
        "calls_attempted": percentage_change(current["calls_attempted"], previous["calls_attempted"]),
        "calls_completed": percentage_change(current["calls_completed"], previous["calls_completed"]),
        "interested": percentage_change(current["interested"], previous["interested"]),
        "follow_up_required": percentage_change(current["follow_up_required"], previous["follow_up_required"]),
        "ready_to_join": percentage_change(current["ready_to_join"], previous["ready_to_join"]),
        "completion_rate": percentage_change(current["completion_rate"], previous["completion_rate"]),
        "interest_rate": percentage_change(current["interest_rate"], previous["interest_rate"]),
    }

    rows = current["daily_performance"]
    report = {
        "report_type": "monthly",
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "generated_at": timezone.now().isoformat(),
        "summary": current,
        "previous_month": {
            "year": previous_year,
            "month": previous_month,
            "month_name": calendar.month_name[previous_month],
            "summary": previous,
        },
        "comparison_vs_previous_month": comparison,
        "performance_highlights": {
            "best_lead_day": _best_day(rows, "new_leads"),
            "best_completed_call_day": _best_day(rows, "calls_completed"),
            "best_interested_day": _best_day(rows, "interested"),
            "best_ready_to_join_day": _best_day(rows, "ready_to_join"),
            "lowest_completion_rate_day": _lowest_day(rows, "completion_rate"),
        },
        "telicall_performance": {
            "lead_to_call_rate": percentage(current["calls_attempted"], current["total_leads"]),
            "call_completion_rate": current["completion_rate"],
            "interest_rate": current["interest_rate"],
            "follow_up_rate": current["follow_up_rate"],
            "ready_to_join_rate": current["ready_to_join_rate"],
            "average_call_duration_seconds": current["average_call_duration_seconds"],
            "average_call_duration_display": current["average_call_duration_display"],
            "total_talk_time_seconds": current["total_call_duration_seconds"],
            "total_talk_time_display": current["total_call_duration_display"],
        },
    }

    if save_json:
        report["json_file"] = save_report_json(
            report,
            report_type="monthly",
            filename=f"{year:04d}-{month:02d}.json",
        )
    return report


def _reports_root() -> Path:
    media_root = Path(getattr(settings, "MEDIA_ROOT", Path(settings.BASE_DIR) / "media"))
    root = media_root / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_report_json(report: Dict[str, Any], *, report_type: str, filename: str) -> str:
    report_type = re.sub(r"[^a-zA-Z0-9_-]", "", report_type) or "general"
    folder = _reports_root() / report_type
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    payload = dict(report)
    payload.pop("json_file", None)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(path)


def get_today_report(*, save_json: bool = False) -> Dict[str, Any]:
    return generate_daily_report(timezone.localdate(), save_json=save_json)


def get_month_report(*, save_json: bool = False) -> Dict[str, Any]:
    today = timezone.localdate()
    return generate_monthly_report(today.year, today.month, save_json=save_json)


def get_report_dashboard() -> Dict[str, Any]:
    today = timezone.localdate()
    return {
        "generated_at": timezone.now().isoformat(),
        "daily": generate_daily_report(today, save_json=False),
        "monthly": generate_monthly_report(today.year, today.month, save_json=False),
    }


def get_follow_up_list(*, report_date: Optional[date] = None) -> List[Dict[str, Any]]:
    insights = _insights_for_date(report_date) if report_date is not None else (
        SalesInsight.objects.select_related("call_session", "call_session__contact")
        .order_by("-call_session__created_at")
    )

    result = []
    for insight in insights:
        data = get_insight_data(insight)
        if _insight_followup(insight, data):
            result.append(build_follow_up_lead(insight))

    priority_rank = {"HOT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    result.sort(
        key=lambda row: (
            priority_rank.get(row.get("lead_priority", "LOW"), 99),
            str(row.get("name", "")).lower(),
        )
    )
    return result


def get_called_list() -> List[Dict[str, Any]]:
    return list(
        CallQueueItem.objects.filter(status="CALLED").order_by("-updated_at").values(
            "id", "name", "phone_number", "details", "status", "call_duration_seconds",
            "ai_summary", "top_question", "created_at", "updated_at",
        )
    )


def get_pending_list() -> List[Dict[str, Any]]:
    return list(
        CallQueueItem.objects.filter(status="PENDING").order_by("created_at").values(
            "id", "name", "phone_number", "details", "status", "created_at", "updated_at",
        )
    )


def get_lead_list() -> List[Dict[str, Any]]:
    return list(
        CallQueueItem.objects.order_by("-created_at").values(
            "id", "name", "phone_number", "details", "status", "call_duration_seconds",
            "ai_summary", "top_question", "created_at", "updated_at",
        )
    )


def get_most_asked_questions(
    *,
    report_date: Optional[date] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    days: Optional[int] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Return most frequently asked question categories.

    Filters supported:
      - report_date=<date>
      - year=<year>, month=<month>
      - days=<number of previous days>
    """
    if report_date is not None:
        insights = _insights_for_date(report_date)
    elif year is not None and month is not None:
        first, next_month = _month_bounds(int(year), int(month))
        start, _ = _local_day_bounds(first)
        end, _ = _local_day_bounds(next_month)
        insights = SalesInsight.objects.filter(
            call_session__created_at__gte=start,
            call_session__created_at__lt=end,
        )
    elif days is not None:
        days = max(1, min(_safe_int(days, 30), 365))
        end = timezone.now()
        start = end - timedelta(days=days)
        insights = SalesInsight.objects.filter(
            call_session__created_at__gte=start,
            call_session__created_at__lte=end,
        )
    else:
        insights = SalesInsight.objects.all()

    counter = Counter()
    for insight in insights:
        data = get_insight_data(insight)
        for category in _extract_question_categories(data):
            counter[_display_question_category(category)] += 1

    return _counter_rows(counter, "question", max(1, int(limit)))


def get_follow_up_report(limit: int = 100) -> List[Dict[str, Any]]:
    """Compatibility wrapper used by views.py and the report dashboard."""
    limit = max(1, min(_safe_int(limit, 100), 1000))
    return get_follow_up_list()[:limit]


def get_previous_days_report(days: int = 30) -> List[Dict[str, Any]]:
    """Return day-by-day Telicall metrics for the previous N days, including today."""
    days = max(1, min(_safe_int(days, 30), 365))
    today = timezone.localdate()
    rows: List[Dict[str, Any]] = []

    for offset in range(days - 1, -1, -1):
        target_date = today - timedelta(days=offset)
        metrics = get_daily_metrics(target_date)
        rows.append({
            "date": target_date.isoformat(),
            "new_leads": metrics.get("new_leads", 0),
            "calls_attempted": metrics.get("calls_attempted", 0),
            "calls_completed": metrics.get("calls_completed", 0),
            "calls_failed": metrics.get("calls_failed", 0),
            "interested": metrics.get("interested", 0),
            "not_interested": metrics.get("not_interested", 0),
            "need_more_information": metrics.get("need_more_information", 0),
            "ready_to_join": metrics.get("ready_to_join", 0),
            "callback_requested": metrics.get("callback_requested", 0),
            "follow_up_required": metrics.get("follow_up_required", 0),
            "completion_rate": metrics.get("completion_rate", 0),
            "interest_rate": metrics.get("interest_rate", 0),
            "average_call_duration_seconds": metrics.get("average_call_duration_seconds", 0),
            "average_call_duration_display": metrics.get("average_call_duration_display", "0s"),
        })

    return rows


def get_report_payload(
    report_range: str = "daily",
    *,
    report_date: Optional[date] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """Route reporting API requests to the matching report generator."""
    report_range = _clean_text(report_range or "daily").lower()
    today = timezone.localdate()

    if report_range == "daily":
        return generate_daily_report(report_date or today, save_json=False)

    if report_range == "weekly":
        rows = get_previous_days_report(7)
        return {
            "report_type": "weekly",
            "generated_at": timezone.now().isoformat(),
            "days": 7,
            "daily_performance": rows,
        }

    if report_range == "monthly":
        selected_year = year or today.year
        selected_month = month or today.month
        return generate_monthly_report(
            selected_year,
            selected_month,
            save_json=False,
        )

    if report_range in {"previous", "previous_days", "history"}:
        days = max(1, min(_safe_int(days, 30), 365))
        return {
            "report_type": "previous",
            "generated_at": timezone.now().isoformat(),
            "days": days,
            "daily_performance": get_previous_days_report(days),
        }

    if report_range in {"followup", "follow_up", "follow-ups"}:
        rows = get_follow_up_report(500)
        return {
            "report_type": "followup",
            "generated_at": timezone.now().isoformat(),
            "count": len(rows),
            "follow_up_leads": rows,
        }

    if report_range in {"questions", "most_asked_questions"}:
        questions = get_most_asked_questions(days=days, limit=20)
        return {
            "report_type": "questions",
            "generated_at": timezone.now().isoformat(),
            "days": days,
            "most_asked_questions": questions,
        }

    raise ValueError(
        "Invalid report range. Use daily, weekly, monthly, previous, "
        "followup, or questions."
    )


def get_call_detail(session_id: int) -> Optional[Dict[str, Any]]:
    insight = SalesInsight.objects.filter(call_session_id=session_id).select_related(
        "call_session", "call_session__contact"
    ).first()

    if insight is not None:
        detail = _insight_to_call_detail(insight)
        detail["sales_insight_available"] = True
        return detail

    session = CallSession.objects.filter(id=session_id).select_related("contact").first()
    if session is None:
        return None

    return {
        "call_session_id": session.id,
        "call_date": timezone.localtime(session.created_at).isoformat() if session.created_at else None,
        "name": session.contact.name if session.contact else "Unknown",
        "phone_number": session.contact.phone_number if session.contact else "",
        "duration_seconds": _safe_int(session.duration_seconds),
        "duration_display": format_duration(session.duration_seconds),
        "status": session.status,
        "sales_insight_available": False,
    }


def print_daily_summary(report: Dict[str, Any]) -> None:
    summary = report.get("summary", {})
    comparison = report.get("comparison_vs_yesterday", {})
    print("=" * 64)
    print(f"TELICALL DAILY REPORT - {report.get('report_date')}")
    print("=" * 64)
    print(f"New leads:              {summary.get('new_leads', 0)}")
    print(f"Calls attempted:        {summary.get('calls_attempted', 0)}")
    print(f"Calls completed:        {summary.get('calls_completed', 0)}")
    print(f"Completion rate:        {summary.get('completion_rate', 0)}%")
    print(f"Interested:             {summary.get('interested', 0)}")
    print(f"Follow-up required:     {summary.get('follow_up_required', 0)}")
    print(f"Ready to join:          {summary.get('ready_to_join', 0)}")
    print(f"Average call duration:  {summary.get('average_call_duration_display', '0s')}")
    print("-" * 64)
    print("VS YESTERDAY")
    for key in (
        "new_leads", "calls_attempted", "calls_completed",
        "interested", "follow_up_required", "ready_to_join",
    ):
        print(f"{key:24} {comparison.get(key, {}).get('label', '0%')}")
    print("=" * 64)


def print_monthly_summary(report: Dict[str, Any]) -> None:
    summary = report.get("summary", {})
    print("=" * 64)
    print(f"TELICALL MONTHLY REPORT - {report.get('month_name')} {report.get('year')}")
    print("=" * 64)
    print(f"Total leads:            {summary.get('total_leads', 0)}")
    print(f"Calls attempted:        {summary.get('calls_attempted', 0)}")
    print(f"Calls completed:        {summary.get('calls_completed', 0)}")
    print(f"Completion rate:        {summary.get('completion_rate', 0)}%")
    print(f"Interested:             {summary.get('interested', 0)}")
    print(f"Follow-up required:     {summary.get('follow_up_required', 0)}")
    print(f"Ready to join:          {summary.get('ready_to_join', 0)}")
    print(f"Total talk time:        {summary.get('total_call_duration_display', '0s')}")
    print(f"Average duration:       {summary.get('average_call_duration_display', '0s')}")
    print("=" * 64)
