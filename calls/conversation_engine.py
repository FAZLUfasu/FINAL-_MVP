import json
import os
import re
from copy import deepcopy

import ollama


CONVERSATION_STAGES = {
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

LEAD_OUTCOMES = {
    "UNDECIDED",
    "NOT_INTERESTED",
    "INTERESTED",
    "NEED_MORE_INFORMATION",
    "READY_TO_JOIN",
    "CALLBACK_REQUESTED",
}

DEFAULT_PROFILE = {
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


def _clean(value, limit=4000):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _history_text(history):
    rows = []
    for item in (history or [])[-12:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).lower().strip()
        text = _clean(item.get("text", ""), 600)
        if role == "user":
            rows.append(f"Customer: {text}")
        elif role == "assistant":
            rows.append(f"AI: {text}")
    return "\n".join(rows) or "(No previous conversation)"


def _lead_text(lead):
    lead = lead or {}
    return (
        f"Name: {_clean(lead.get('name'), 120) or '(unknown)'}\n"
        f"Phone: {_clean(lead.get('phone_number'), 60) or '(unknown)'}\n"
        f"Existing details: {_clean(lead.get('details'), 1000) or '(none)'}"
    )


def _profile_for_prompt(profile):
    merged = deepcopy(DEFAULT_PROFILE)
    if isinstance(profile, dict):
        for k in merged:
            if k in profile:
                merged[k] = profile[k]
    return merged


AFFIRMATIVE_PERMISSION_PATTERNS = (
    r"\byes\b",
    r"\byeah\b",
    r"\byep\b",
    r"\bok\b",
    r"\bokay\b",
    r"\bsure\b",
    r"\bgo ahead\b",
    r"\bplease tell me\b",
    r"\btell me\b",
    r"\bcontinue\b",
    r"\byou can\b",
    r"\bplease continue\b",
)

NEGATIVE_PERMISSION_PATTERNS = (
    r"\bno\b",
    r"\bnot interested\b",
    r"\bdon'?t call\b",
    r"\bdo not call\b",
    r"\bstop\b",
    r"\bno thanks\b",
    r"\bno thank you\b",
)


def _permission_signal(text):
    """Return ALLOW, REFUSE, or None for clear permission-stage messages."""
    value = _clean(text, 500).lower()
    if not value:
        return None

    for pattern in NEGATIVE_PERMISSION_PATTERNS:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return "REFUSE"

    for pattern in AFFIRMATIVE_PERMISSION_PATTERNS:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return "ALLOW"

    return None


async def generate_conversation_turn(
    *,
    company_config,
    lead,
    history,
    customer_profile,
    stage,
    user_text,
    model="llama3.2",
):
    """
    Shared Telicall conversation brain.

    Input:
      company_config: verified company facts
      lead: customer/lead facts
      history: previous user/assistant turns
      customer_profile: structured live sales state
      stage: current conversation stage
      user_text: newest customer message

    Output:
      structured dict with natural spoken reply + updated sales state.
    """

    config = company_config or {}
    company = _clean(config.get("company"), 200) or "the company"
    bot_name = _clean(config.get("bot_name"), 120) or "AI Assistant"
    details = str(config.get("details") or "").strip()
    closing = _clean(config.get("closing"), 600)
    followup = _clean(config.get("followup_message"), 600)
    language = _clean(config.get("default_language"), 80) or "English"

    current_stage = str(stage or "WAIT_PERMISSION").strip().upper()
    if current_stage not in CONVERSATION_STAGES:
        current_stage = "WAIT_PERMISSION"

    original_stage = current_stage
    permission_signal = None

    # WAIT_PERMISSION must not depend entirely on the language model.
    # Clear affirmative responses advance the conversation immediately.
    if current_stage == "WAIT_PERMISSION":
        permission_signal = _permission_signal(user_text)

        if permission_signal == "ALLOW":
            current_stage = "NEED_DISCOVERY"

        elif permission_signal == "REFUSE":
            current_stage = "CLOSING"

    profile = _profile_for_prompt(customer_profile)
    history_text = _history_text(history)
    lead_text = _lead_text(lead)
    current_message = _clean(user_text, 1200)

    system_prompt = f"""
You are {bot_name}, a natural outbound sales-call assistant representing {company}.

IMPORTANT IDENTITY
- {company} is the company.
- The lead/customer is the person being contacted.
- Never confuse the customer with the company.
- Use the customer's name mainly in the first greeting only.
- Do not start ordinary replies with "Hello" or the customer's name.

VERIFIED COMPANY KNOWLEDGE
{details}

LEAD INFORMATION
{lead_text}

CURRENT SALES STATE
Previous stage: {original_stage}
Effective stage for this turn: {current_stage}
Permission signal: {permission_signal or "NONE"}
Profile:
{json.dumps(profile, ensure_ascii=False)}

CRITICAL PERMISSION TRANSITION RULE
- If Permission signal is ALLOW, the customer has already given permission.
- NEVER ask "May I tell you more?", "Can I continue?", or any equivalent permission question again.
- Do not repeat the opening greeting or generic company introduction.
- Continue directly into a useful next step: briefly explain the relevant offering or ask one natural need-discovery question.
- If the customer said "please tell me", "tell me", "yes", "okay", "sure", or "go ahead", treat that as permission to continue.
- If Permission signal is REFUSE, close politely and do not continue selling.

DEFAULT LANGUAGE
{language}

CORE PRINCIPLE
The company information above is FACTUAL REFERENCE MATERIAL, not a script.
Use it to know what is true, but communicate the relevant facts naturally in your own words.

NATURAL CONVERSATION
- Speak like a helpful human sales representative on a phone call.
- Understand imperfect spelling, grammar, short phrases, and conversational language.
- Answer the meaning of the customer's question, not just matching keywords.
- Normally use 1-3 short spoken sentences.
- Vary wording naturally.
- Do not repeat the same sentence or generic company introduction already used.
- Do not repeat a question whose answer is already known.
- Ask only ONE useful question at a time.
- Answer the customer's exact question first, then move the conversation forward naturally when useful.

FACTUAL SAFETY
- Never invent a course, product, service, fee, schedule, batch, certificate,
  discount, guarantee, address, URL, phone number, approval, or company policy.
- If something is not present in VERIFIED COMPANY KNOWLEDGE, treat it as not verified.
- Do not guess just because it is common in the industry.

UNLISTED COURSE / CUSTOM REQUIREMENT
If the customer asks for a course, topic, service, or syllabus that is not listed:
- Do NOT simply say "No" and end the discussion.
- Politely explain that it is not currently listed as a standard offering.
- Explore what the customer actually needs.
- When appropriate, say the team can CHECK whether a customized syllabus,
  suitable option, or special learning plan can be arranged for the requirement.
- Never guarantee customization unless company knowledge explicitly guarantees it.
Example style:
"That isn't currently listed as one of our standard training areas, but we can check
whether a customized option can be arranged around what you need. What are you hoping
to use it for?"

SALES BEHAVIOR
- Your goal is to understand the customer's profile and need, give relevant verified
  information, determine interest, and guide them toward a sensible next step.
- Do not behave like an FAQ bot.
- If the customer is unsure what they need, ask about their goal before listing everything.
- If the customer clearly refuses or asks not to be contacted, stop selling and close politely.
- If the customer requests a callback, acknowledge it.
- If the customer wants a human or exact unavailable details, mark more information/follow-up.
- If the customer clearly wants to join/proceed, mark READY_TO_JOIN.
- Never pressure the customer.

STAGE GUIDANCE
WAIT_PERMISSION:
  Determine whether the customer permits the conversation to continue.
  Once permission is given, NEVER remain in WAIT_PERMISSION and never ask permission again.
PROFILE_COLLECTION:
  Identify relevant background only if it is not already known.
NEED_DISCOVERY:
  Understand what the customer wants to achieve.
PRODUCT_EXPLANATION:
  Explain only the most relevant verified offering.
DETAIL_COLLECTION:
  Collect useful missing customer details naturally, one at a time.
INTEREST_CHECK:
  Determine interest / more info / callback / ready to join.
FOLLOW_UP:
  Confirm human follow-up when appropriate.
CLOSING:
  Close politely without starting a new sales question.
FINISHED:
  Do not continue selling.

RECENT CONVERSATION
{history_text}

CURRENT CUSTOMER MESSAGE
Customer: {current_message}

Return ONLY valid JSON with exactly this shape:
{{
  "reply": "natural spoken reply",
  "next_stage": "WAIT_PERMISSION|PROFILE_COLLECTION|NEED_DISCOVERY|PRODUCT_EXPLANATION|DETAIL_COLLECTION|INTEREST_CHECK|FOLLOW_UP|CLOSING|FINISHED",
  "customer_profile": {{
    "customer_type": null,
    "education": null,
    "occupation": null,
    "interest_area": null,
    "course_interest": null
  }},
  "interest_status": "UNDECIDED|NOT_INTERESTED|INTERESTED|NEED_MORE_INFORMATION|READY_TO_JOIN|CALLBACK_REQUESTED",
  "needs_more_information": false,
  "ready_to_join": false,
  "callback_requested": false
}}

FINAL CHECK BEFORE RETURNING
- Is the reply natural rather than brochure-like?
- Did it answer the exact current message?
- Is it materially different from previous AI replies?
- Did it avoid unnecessary customer-name repetition?
- Did it avoid inventing facts?
- If an unlisted course was requested, did it politely explore the need instead of hallucinating?
- If permission was already given, did it avoid asking permission again?
- Did it avoid repeating the opening greeting?
"""

    client = ollama.AsyncClient()
    response = await client.generate(
        model=model,
        prompt=system_prompt,
        stream=False,
        format="json",
        keep_alive="30m",
        options={
            "temperature": 0.35,
            "num_predict": 180,
            "num_ctx": 4096,
            "num_thread": max(1, (os.cpu_count() or 4) - 1),
        },
    )

    raw = str(response.get("response", "") or "").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "reply": raw or "Could you tell me a little more about what you need?",
            "next_stage": current_stage,
            "customer_profile": {},
            "interest_status": profile["interest_status"],
            "needs_more_information": profile["needs_more_information"],
            "ready_to_join": profile["ready_to_join"],
            "callback_requested": profile["callback_requested"],
        }

    reply = _clean(result.get("reply"), 700)
    if not reply:
        reply = "Could you tell me a little more about what you need?"

    next_stage = str(result.get("next_stage", current_stage) or "").upper()
    if next_stage not in CONVERSATION_STAGES:
        next_stage = current_stage

    # Deterministic permission-stage guard.
    if original_stage == "WAIT_PERMISSION":
        if permission_signal == "ALLOW":
            # Never allow the model to loop back to permission after consent.
            if next_stage == "WAIT_PERMISSION":
                next_stage = "NEED_DISCOVERY"

            # Defensive cleanup if the model still tries to repeat permission language.
            repeated_permission = re.search(
                r"\b(may i|can i|shall i|would you like me to|"
                r"briefly tell you|tell you more)\b",
                reply,
                flags=re.IGNORECASE,
            )
            repeated_opening = re.search(
                r"\b(ai calling assistant|we provide practical ai training programs)\b",
                reply,
                flags=re.IGNORECASE,
            )

            if repeated_permission or repeated_opening:
                reply = (
                    "Of course. I can explain the available options based on what "
                    "you are looking for. What would you mainly like help with?"
                )

        elif permission_signal == "REFUSE":
            next_stage = "CLOSING"
            reply = closing or "No problem. Thank you for your time. Have a good day."

    status = str(
        result.get("interest_status", profile["interest_status"]) or ""
    ).upper()
    if status not in LEAD_OUTCOMES:
        status = profile["interest_status"]

    extracted = result.get("customer_profile")
    if not isinstance(extracted, dict):
        extracted = {}

    out_profile = deepcopy(profile)
    for key in (
        "customer_type",
        "education",
        "occupation",
        "interest_area",
        "course_interest",
    ):
        value = extracted.get(key)
        if value not in (None, "", "null"):
            out_profile[key] = _clean(value, 250)

    out_profile["interest_status"] = status
    out_profile["needs_more_information"] = bool(
        result.get("needs_more_information", out_profile["needs_more_information"])
    )
    out_profile["ready_to_join"] = bool(
        result.get("ready_to_join", out_profile["ready_to_join"])
    )
    out_profile["callback_requested"] = bool(
        result.get("callback_requested", out_profile["callback_requested"])
    )

    if out_profile["ready_to_join"]:
        out_profile["interest_status"] = "READY_TO_JOIN"
    elif out_profile["callback_requested"]:
        out_profile["interest_status"] = "CALLBACK_REQUESTED"
    elif out_profile["needs_more_information"] and status == "UNDECIDED":
        out_profile["interest_status"] = "NEED_MORE_INFORMATION"

    return {
        "reply": reply,
        "next_stage": next_stage,
        "customer_profile": out_profile,
        "interest_status": out_profile["interest_status"],
        "needs_more_information": out_profile["needs_more_information"],
        "ready_to_join": out_profile["ready_to_join"],
        "callback_requested": out_profile["callback_requested"],
    }
