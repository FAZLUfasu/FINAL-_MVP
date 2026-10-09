import json
import os
import re
from copy import deepcopy

from google import genai
from google.genai import types

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

# Telicall's supported live-call languages.
LANGUAGE_NAMES = {
    "en": "English",
    "ml": "Malayalam",
    "ta": "Tamil",
    "hi": "Hindi",
    "ar": "Arabic",
}

DEFAULT_LANGUAGE_CODE = "en"

# Cloud API Model Hierarchy (Auto-failover on capacity spikes/503)
GEMINI_PRIMARY_MODEL = (
    os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    or "gemini-3.8-flash"
)

# Keep the primary model first and try each distinct model only once.
GEMINI_MODELS_POOL = list(dict.fromkeys([
    GEMINI_PRIMARY_MODEL,
    "gemini-3.5-flash-lite",
]))

FALLBACK_REPLIES = {
    "en": "Could you tell me a little more about what you need?",
    "ml": "നിങ്ങൾക്ക് എന്താണ് വേണ്ടതെന്ന് കുറച്ച് കൂടി പറയാമോ?",
    "ta": "உங்களுக்கு என்ன தேவை என்பதை கொஞ்சம் விரிவாக சொல்ல முடியுமா?",
    "hi": "क्या आप थोड़ा और बता सकते हैं कि आपको किस बारे में जानकारी चाहिए?",
    "ar": "هل يمكنك أن تخبرني بمزيد من التفاصيل عما تحتاج إليه؟",
}

CONTINUE_REPLIES = {
    "en": "Of course. What would you mainly like help with?",
    "ml": "തീർച്ചയായും. പ്രധാനമായും എന്തിനെക്കുറിച്ചാണ് സഹായം വേണ്ടത്?",
    "ta": "நிச்சயமாக. முக்கியமாக எந்த விஷயத்தில் உதவி வேண்டும்?",
    "hi": "ज़रूर। आपको मुख्य रूप से किस बारे में मदद चाहिए?",
    "ar": "بالتأكيد. ما الشيء الرئيسي الذي تود الحصول على مساعدة بشأنه؟",
}

REFUSAL_REPLIES = {
    "en": "No problem. Thank you for your time. Have a good day.",
    "ml": "പ്രശ്നമില്ല. നിങ്ങളുടെ സമയത്തിന് നന്ദി. നല്ല ദിവസം ആശംസിക്കുന്നു.",
    "ta": "பரவாயில்லை. உங்கள் நேரத்திற்கு நன்றி. நல்ல நாளாக அமையட்டும்.",
    "hi": "कोई समस्या नहीं। आपके समय के लिए धन्यवाद। आपका दिन शुभ हो।",
    "ar": "لا مشكلة. شكرًا لوقتك. أتمنى لك يومًا سعيدًا.",
}


def _get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is missing or empty.")
    return genai.Client(api_key=api_key)


def _language_code(value):
    value = str(value or "").strip().lower()
    if value in LANGUAGE_NAMES:
        return value

    reverse = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
    return reverse.get(value, DEFAULT_LANGUAGE_CODE)


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
    r"\byes\b", r"\byeah\b", r"\byep\b", r"\bok\b", r"\bokay\b",
    r"\bsure\b", r"\bgo ahead\b", r"\bplease tell me\b", r"\btell me\b",
    r"\bcontinue\b", r"അതെ", r"ശരി", r"പറയൂ", r"തുടരൂ", r"ஆம்", r"ஆமாம்",
    r"சரி", r"சொல்லுங்கள்", r"தொடருங்கள்", r"हाँ", r"हां", r"ठीक है",
    r"बताइए", r"बताओ", r"जारी रखें", r"نعم", r"حسنا", r"حسنًا", r"تفضل", r"تابع"
)

NEGATIVE_PERMISSION_PATTERNS = (
    r"\bno\b", r"\bnot interested\b", r"\bdon'?t call\b", r"\bdo not call\b",
    r"\bstop\b", r"\bno thanks\b", r"\bno thank you\b", r"വേണ്ട", r"താൽപര്യമില്ല",
    r"വിളിക്കേണ്ട", r"നിർത്തൂ", r"வேண்டாம்", r"ஆர்வம் இல்லை", r"அழைக்க வேண்டாம்",
    r"நிறுத்துங்கள்", r"नहीं", r"रुचि नहीं", r"फोन मत करना", r"बंद करें",
    r"لا", r"غير مهتم", r"لا تتصل", r"توقف"
)


def _permission_signal(text):
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


async def _call_gemini_with_fallback(client, prompt: str) -> tuple[str, str]:
    """Tries the configured Gemini model pool in order until one succeeds."""
    last_error = None
    for model_name in GEMINI_MODELS_POOL:
        try:
            print(f"🧠 [GEMINI API] Requesting turn from model={model_name}...")
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.25,
                    max_output_tokens=250,
                ),
            )
            raw_text = (response.text or "").strip()
            if not raw_text:
                raise ValueError("Gemini returned an empty response.")

            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Gemini returned invalid JSON: {exc}"
                ) from exc

            if not isinstance(parsed, dict):
                raise ValueError(
                    "Gemini response must be a JSON object."
                )

            reply = parsed.get("reply")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError(
                    "Gemini JSON is missing a non-empty reply."
                )

            return raw_text, model_name
        except Exception as exc:
            print(f"⚠️ [GEMINI API FAILOVER] {model_name} failed: {exc}")
            last_error = exc
            continue

    raise RuntimeError(f"All Gemini models in pool failed. Last error: {last_error}")


async def generate_gemini_json(
    prompt: str,
    *,
    temperature: float = 0.1,
    max_output_tokens: int = 400,
) -> tuple[str, str]:
    """Generate a JSON object with Gemini and model failover."""
    client = _get_gemini_client()
    last_error = None

    for model_name in GEMINI_MODELS_POOL:
        try:
            print(f"[GEMINI POST-CALL] Requesting JSON from {model_name}...")
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                ),
            )
            raw_text = (response.text or "").strip()
            if not raw_text:
                raise ValueError("Gemini returned an empty response.")

            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                raise ValueError("Gemini response must be a JSON object.")

            return raw_text, model_name
        except Exception as exc:
            print(f"[GEMINI POST-CALL FAILOVER] {model_name} failed: {exc}")
            last_error = exc

    raise RuntimeError(
        f"All Gemini models in pool failed. Last error: {last_error}"
    )


async def generate_conversation_turn(
    *,
    company_config,
    lead,
    history,
    customer_profile,
    stage,
    user_text,
    response_language=DEFAULT_LANGUAGE_CODE,
    language_locked=False,
):
    """
    100% Cloud API Conversation Engine for Telicall.
    """
    config = company_config or {}
    company = _clean(config.get("company"), 200) or "the company"
    bot_name = _clean(config.get("bot_name"), 120) or "AI Assistant"
    details = str(config.get("details") or "").strip()
    closing = _clean(config.get("closing"), 600)

    language_code = _language_code(response_language)
    language = LANGUAGE_NAMES[language_code]

    current_stage = str(stage or "WAIT_PERMISSION").strip().upper()
    if current_stage not in CONVERSATION_STAGES:
        current_stage = "WAIT_PERMISSION"

    original_stage = current_stage
    permission_signal = None

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

RESPONSE LANGUAGE — STRICT
{language} ({language_code})

- Reply to the customer ONLY in {language}.
- Do not switch to English unless the selected response language is English.
- Keep company names, course names, URLs, phone numbers and established technical terms unchanged when appropriate.
- Understand code-mixed input, but keep the natural spoken reply primarily in {language}.
- The JSON property names must remain exactly as requested in English; only the human-facing "reply" text follows {language}.

CORE PRINCIPLE
The company information above is FACTUAL REFERENCE MATERIAL, not a script.
Use it to know what is true, but communicate the relevant facts naturally in your own words.

NATURAL PHONE CONVERSATION — VERY IMPORTANT
- Speak like a real, calm, helpful human telecaller, not like a website, brochure, chatbot, or advertisement.
- Use simple everyday spoken language.
- Prefer 1-2 short sentences. Usually keep the reply under about 35 spoken words unless the customer explicitly asks for details.
- Answer the customer's exact message FIRST.
- Then, only when useful, ask ONE short follow-up question.
- Never give a long list unless the customer specifically asks for a list.
- Never dump all company information at once.
- Never repeat the company introduction after it has already been given.
- Never repeat a question whose answer is already known.
- Never start ordinary replies with "Hello", "Hi", the customer's name, or a fresh company introduction.
- Avoid marketing phrases such as "comprehensive", "cutting-edge", "revolutionary", "empower", "transform your future", or similar promotional wording.
- Do not sound overexcited. Do not praise every customer response.
- Acknowledge briefly when needed, then continue naturally.
- Understand imperfect spelling, grammar, short phrases, code-mixed language, and conversational language.
- Vary wording naturally and avoid repeating the same sentence structure.
- If the customer's message is unclear, ask one simple clarification question instead of guessing.

FACTUAL SAFETY
- Never invent a course, product, service, fee, schedule, batch, certificate, discount, guarantee, address, URL, phone number, approval, or company policy.
- If something is not present in VERIFIED COMPANY KNOWLEDGE, treat it as not verified.

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
"""

    client = _get_gemini_client()
    raw = ""
    used_model = "unknown"
    api_success = False

    try:
        raw, used_model = await _call_gemini_with_fallback(client, system_prompt)
        api_success = bool(raw and used_model != "unknown")
    except Exception as exc:
        print(f"❌ [CRITICAL API ERROR] {exc}")
        raw = ""

    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        result = {
            "reply": raw or FALLBACK_REPLIES[language_code],
            "next_stage": current_stage,
            "customer_profile": {},
            "interest_status": profile["interest_status"],
            "needs_more_information": profile["needs_more_information"],
            "ready_to_join": profile["ready_to_join"],
            "callback_requested": profile["callback_requested"],
        }

    reply = _clean(result.get("reply"), 700)
    if not reply:
        reply = FALLBACK_REPLIES[language_code]

    reply_sentences = re.split(r"(?<=[.!?।؟])\s+", reply)
    if len(reply_sentences) > 3:
        reply = " ".join(reply_sentences[:3]).strip()

    if len(reply) > 420:
        reply = reply[:420].rsplit(" ", 1)[0].strip() or reply[:420].strip()

    next_stage = str(result.get("next_stage", current_stage) or "").upper()
    if next_stage not in CONVERSATION_STAGES:
        next_stage = current_stage

    if original_stage == "WAIT_PERMISSION":
        if permission_signal == "ALLOW":
            if next_stage == "WAIT_PERMISSION":
                next_stage = "NEED_DISCOVERY"
            repeated_permission = re.search(
                r"\b(may i|can i|shall i|would you like me to|briefly tell you|tell you more)\b",
                reply,
                flags=re.IGNORECASE,
            )
            repeated_opening = re.search(
                r"\b(ai calling assistant|we provide practical ai training programs)\b",
                reply,
                flags=re.IGNORECASE,
            )
            if repeated_permission or repeated_opening:
                reply = CONTINUE_REPLIES[language_code]

        elif permission_signal == "REFUSE":
            next_stage = "CLOSING"
            reply = closing or REFUSAL_REPLIES[language_code]

    status = str(result.get("interest_status", profile["interest_status"]) or "").upper()
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
        "response_language": language_code,
        "response_model": used_model,
        "response_model_route": f"api:{used_model}",
        "api_success": api_success,
        "next_stage": next_stage,
        "customer_profile": out_profile,
        "interest_status": out_profile["interest_status"],
        "needs_more_information": out_profile["needs_more_information"],
        "ready_to_join": out_profile["ready_to_join"],
        "callback_requested": out_profile["callback_requested"],
    }