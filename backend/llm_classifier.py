"""
llm_classifier.py

Uses the main LLM (gemma3:12b via Open WebUI) to classify
employee intent instead of fragile keyword matching.

Returns a structured result with bucket + extracted details.
"""

import httpx
import json
import os
import re
from datetime import datetime, date, timedelta
from enum import Enum
from typing import Optional

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
OPENWEBUI_URL     = os.getenv("OPENWEBUI_URL", "http://host.docker.internal:3000")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
MODEL_ID          = os.getenv("MODEL_ID", "hr-policy")


class Bucket(Enum):
    POLICY   = "policy"    # RAG pipeline
    PERSONAL = "personal"  # SQL DB query
    ACTION   = "action"    # Execute something


class ActionType(Enum):
    APPLY_LEAVE    = "apply_leave"
    APPLY_WFH      = "apply_wfh"
    CANCEL_REQUEST = "cancel_request"
    CHECK_STATUS   = "check_status"
    CONFIRM        = "confirm"
    REJECT         = "reject"
    UNKNOWN        = "unknown"


# ─────────────────────────────────────────────
# CLASSIFICATION PROMPT
# ─────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are an HR assistant intent classifier.
Classify the employee message into exactly one bucket and extract details.

Today's date: {today}
Employee has a pending action waiting for confirmation: {has_pending}

BUCKETS:
1. "policy" - asking about company rules, policies, procedures, entitlements in general
   Examples: "what is the leave policy", "what is the remote work policy",
   "can I carry forward unused leave", "what are working hours"
   NOTE: "how many sick days am I entitled to" is policy.
   NOTE: "I'm sick" or "feeling unwell" are NOT policy — they are actions (apply_leave/sick).

2. "personal" - asking about THEIR OWN specific data, history, or balance
   Examples: "how many days do I have left", "show my leave balance",
   "what leave did I take this year", "my remaining annual days", "my WFH history"

3. "action" - the employee wants to DO something or is expressing a state that implies a request
   Examples: "I want 3 days leave", "apply WFH tomorrow", "cancel my request",
   "I need sick leave", "book leave for next week", "yes proceed", "no cancel it",
   "I'm sick today" → apply_leave/sick (today is start_date, days=1),
   "feeling unwell" → apply_leave/sick,
   "not feeling well today" → apply_leave/sick,
   "I have a fever" → apply_leave/sick,
   "family emergency" → apply_leave/emergency,
   "I want to take a day off tomorrow" → apply_leave/annual

IMPORTANT RULES:
- Statements about being sick/ill/unwell ALWAYS map to bucket=action, action_type=apply_leave, leave_type=sick
- "today" means start_date={today_iso}, end_date={today_iso}, days=1
- "tomorrow" means the next calendar day
- If the employee says "I'm sick" with no date, assume today (start_date={today_iso}, days=1)

For "action" bucket, also set action_type:
- "apply_leave" - wants to apply for annual/sick/emergency leave
- "apply_wfh" - wants to apply for work from home
- "cancel_request" - wants to cancel a pending request
- "check_status" - wants to know status of their requests
- "confirm" - confirming a pending action (yes, proceed, correct, submit)
- "reject" - rejecting a pending action (no, cancel, forget it, abort)

For leave/WFH actions, extract:
- leave_type: "annual", "sick", "emergency", or null
- start_date: exact "YYYY-MM-DD" or null
- end_date: exact "YYYY-MM-DD" or null
- days: integer or null

Employee message: "{message}"

Reply with ONLY valid JSON, no explanation:
{{
  "bucket": "policy|personal|action",
  "action_type": "apply_leave|apply_wfh|cancel_request|check_status|confirm|reject|null",
  "leave_type": "annual|sick|emergency|null",
  "start_date": "YYYY-MM-DD or null",
  "end_date": "YYYY-MM-DD or null",
  "days": null,
  "confidence": 0.95
}}"""


# ─────────────────────────────────────────────
# MAIN CLASSIFIER
# ─────────────────────────────────────────────

async def classify_with_llm(
    message: str,
    has_pending: bool = False
) -> dict:
    """
    Uses LLM to classify the employee message.
    Falls back to rule-based if LLM fails.
    """
    today     = date.today().strftime("%A, %d %B %Y")
    today_iso = date.today().strftime("%Y-%m-%d")

    prompt = CLASSIFIER_PROMPT.format(
        today=today,
        today_iso=today_iso,
        has_pending="YES — if they say yes/confirm/proceed, use confirm action_type"
                    if has_pending else "NO",
        message=message
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{OPENWEBUI_URL}/api/chat/completions",
                json={
                    "model":    MODEL_ID,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":   False,
                    "temperature": 0.1  # low temperature for consistent classification
                },
                headers={
                    "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
                    "Content-Type":  "application/json"
                }
            )

        if response.status_code != 200:
            return _fallback_classify(message, has_pending)

        content = response.json()["choices"][0]["message"]["content"]

        # Extract JSON from response
        result = _parse_json_response(content)
        if result:
            return _normalize_result(result, message)

    except Exception as e:
        print(f"⚠️ LLM classification failed: {e}")

    # Fallback to rule-based
    return _fallback_classify(message, has_pending)


def _parse_json_response(content: str) -> Optional[dict]:
    """Extract JSON from LLM response — handles markdown code blocks"""
    # Remove markdown code blocks if present
    content = re.sub(r'```(?:json)?\s*', '', content).strip()
    content = content.replace('```', '').strip()

    # Find JSON object
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try parsing the whole content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _normalize_result(result: dict, message: str) -> dict:
    """Normalize and validate LLM classification result"""
    bucket      = result.get("bucket", "policy")
    action_type = result.get("action_type") or "unknown"
    leave_type  = result.get("leave_type")
    start_date  = result.get("start_date")
    end_date    = result.get("end_date")
    days        = result.get("days")
    confidence  = float(result.get("confidence", 0.8))

    # Clean null strings
    if action_type in ("null", "none", ""):  action_type = "unknown"
    if leave_type  in ("null", "none", ""):  leave_type  = None
    if start_date  in ("null", "none", ""):  start_date  = None
    if end_date    in ("null", "none", ""):  end_date    = None

    # Validate dates
    start_date = _validate_date(start_date)
    end_date   = _validate_date(end_date)

    # Calculate days if we have both dates
    if start_date and end_date and not days:
        days = float(_count_working_days(start_date, end_date))

    # Calculate end_date if we have start + days
    if start_date and days and not end_date:
        end_date = _add_working_days(start_date, int(days))

    return {
        "bucket":      bucket,
        "action_type": action_type,
        "leave_type":  leave_type,
        "start_date":  start_date,
        "end_date":    end_date,
        "days":        int(days) if days else None,
        "confidence":  confidence
    }


def _validate_date(date_str: Optional[str]) -> Optional[str]:
    """Validate and return date string or None"""
    if not date_str:
        return None
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except (ValueError, TypeError):
        return None


def _count_working_days(start_str: str, end_str: str) -> int:
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.strptime(end_str,   "%Y-%m-%d")
    count = 0
    cur   = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _add_working_days(start_str: str, days: int) -> str:
    start = datetime.strptime(start_str, "%Y-%m-%d")
    cur   = start
    count = 0
    while count < days:
        if cur.weekday() < 5:
            count += 1
        if count < days:
            cur += timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────
# RULE-BASED FALLBACK
# Used when LLM is unavailable or fails
# ─────────────────────────────────────────────

_CONFIRM_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
    "confirm", "proceed", "go ahead", "submit", "correct",
    "sounds good", "please proceed", "please submit",
    "that's right", "do it", "approved", "perfect"
}

_REJECT_WORDS = {
    "no", "nope", "nah", "cancel", "abort", "stop",
    "forget it", "nevermind", "never mind", "discard",
    "don't", "dont", "skip it", "reject"
}

_PERSONAL_PHRASES = [
    "my leave", "my balance", "my remaining", "my history",
    "my request", "my wfh", "days i have", "days do i have",
    "how many days", "how many leave", "remaining days",
    "leave balance", "days left", "days remaining",
    "have i taken", "i have taken", "my annual", "my sick",
    "my emergency", "pending request", "my requests",
]

_ACTION_PHRASES = [
    "apply", "request", "book", "submit", "take leave",
    "need leave", "want leave", "work from home", "wfh",
    "cancel my", "withdraw", "i am sick", "i'm sick",
    "feeling unwell", "not well", "days off", "time off",
]

_LEAVE_TYPE_MAP = {
    "annual":    ["annual", "vacation", "holiday", "paid leave"],
    "sick":      ["sick", "medical", "ill", "unwell", "doctor", "health"],
    "emergency": ["emergency", "urgent", "family emergency"],
}


def _fallback_classify(message: str, has_pending: bool) -> dict:
    """Simple rule-based fallback when LLM unavailable"""
    q = message.lower().strip()

    base = {
        "bucket": "policy", "action_type": "unknown",
        "leave_type": None, "start_date": None,
        "end_date": None, "days": None, "confidence": 0.6
    }

    # Confirm/Reject
    if has_pending:
        words = set(q.split())
        if words & _CONFIRM_WORDS or any(p in q for p in ["yes", "go ahead", "proceed"]):
            return {**base, "bucket": "action", "action_type": "confirm", "confidence": 0.9}
        if words & _REJECT_WORDS or any(p in q for p in ["no ", "cancel", "abort"]):
            return {**base, "bucket": "action", "action_type": "reject", "confidence": 0.9}

    # WFH
    if "work from home" in q or "wfh" in q or "work remotely" in q:
        return {**base, "bucket": "action", "action_type": "apply_wfh", "confidence": 0.8}

    # Cancel request (must check before generic action — "cancel my" is in _ACTION_PHRASES)
    if any(p in q for p in ["cancel my", "withdraw my", "retract my", "cancel leave", "cancel request"]):
        return {**base, "bucket": "action", "action_type": "cancel_request", "confidence": 0.85}

    # Check status
    if any(p in q for p in ["status", "request status", "my requests", "pending approval", "approved yet", "any update"]):
        return {**base, "bucket": "action", "action_type": "check_status", "confidence": 0.8}

    # Action (apply leave)
    if any(p in q for p in _ACTION_PHRASES):
        leave_type = None
        for lt, keywords in _LEAVE_TYPE_MAP.items():
            if any(kw in q for kw in keywords):
                leave_type = lt
                break
        return {
            **base,
            "bucket":      "action",
            "action_type": "apply_leave",
            "leave_type":  leave_type,
            "confidence":  0.7
        }

    # Personal
    if any(p in q for p in _PERSONAL_PHRASES):
        return {**base, "bucket": "personal", "confidence": 0.75}

    # Default to policy
    return {**base, "bucket": "policy", "confidence": 0.65}
