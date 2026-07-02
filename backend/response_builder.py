"""
response_builder.py

Builds formatted responses for personal data queries
and action confirmations.
"""

from datetime import datetime, timedelta
from typing import Optional


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


# ─────────────────────────────────────────────
# PERSONAL DATA RESPONSES
# ─────────────────────────────────────────────

def build_balance_response(employee, balances: list, requests: list) -> str:
    """Format leave balance as a friendly message"""
    if not balances:
        return (
            "I couldn't find your leave balances. "
            "Please contact HR directly."
        )

    year = datetime.now().year
    lines = [f"Here are your leave balances for **{year}**, {employee.full_name}:\n"]

    for b in balances:
        taken     = b.taken or 0
        remaining = b.remaining
        total     = b.total_allowed
        pct       = int((taken / total * 100)) if total else 0

        emoji = {
            "annual":    "🏖️",
            "sick":      "🤒",
            "emergency": "🚨"
        }.get(b.leave_type, "📋")

        # Progress bar
        filled = int(pct / 10)
        bar    = "█" * filled + "░" * (10 - filled)

        status = ""
        if remaining <= 0:
            status = " ⚠️ **Exhausted**"
        elif remaining <= 2:
            status = " ⚠️ **Low**"

        lines.append(
            f"{emoji} **{b.leave_type.title()} Leave**{status}\n"
            f"   {bar} {pct}% used\n"
            f"   Remaining: **{remaining:.0f}** of {total:.0f} days "
            f"({taken:.0f} taken)"
        )

    # Pending requests
    pending = [r for r in requests if r.status == "pending"]
    if pending:
        lines.append(f"\n⏳ **Pending Requests:** {len(pending)}")
        for r in pending[:3]:
            start = datetime.fromisoformat(str(r.start_date)).strftime("%d %b")
            end   = datetime.fromisoformat(str(r.end_date)).strftime("%d %b %Y")
            lines.append(
                f"   • {r.leave_type.title()} leave: "
                f"{start} → {end} ({r.days_requested:.0f} days)"
            )

    lines.append(
        "\n💡 _Type 'apply for leave' or 'work from home' "
        "to submit a new request._"
    )

    return "\n".join(lines)


def build_requests_response(leave_requests: list, wfh_requests: list) -> str:
    """Format request history as a friendly message"""
    all_requests = []

    for r in leave_requests:
        all_requests.append({
            "type":        r.leave_type.title() + " Leave",
            "start":       r.start_date,
            "end":         r.end_date,
            "days":        r.days_requested,
            "status":      r.status,
            "note":        r.manager_note,
            "requested_at": r.requested_at,
            "id":          r.id,
            "category":    "leave"
        })

    for r in wfh_requests:
        all_requests.append({
            "type":        "Work From Home",
            "start":       r.start_date,
            "end":         r.end_date,
            "days":        r.days_requested,
            "status":      r.status,
            "note":        r.manager_note,
            "requested_at": r.requested_at,
            "id":          r.id,
            "category":    "wfh"
        })

    if not all_requests:
        return (
            "You have no leave or WFH requests on record.\n\n"
            "💡 _Type 'I want to apply for leave' to submit your first request._"
        )

    # Sort by requested_at descending
    all_requests.sort(key=lambda x: str(x["requested_at"]), reverse=True)

    status_emoji = {
        "pending":   "⏳",
        "approved":  "✅",
        "rejected":  "❌",
        "cancelled": "🚫"
    }

    lines = ["Here are your recent requests:\n"]

    for req in all_requests[:8]:
        emoji  = status_emoji.get(req["status"], "❓")
        start  = datetime.fromisoformat(str(req["start"])).strftime("%d %b")
        end    = datetime.fromisoformat(str(req["end"])).strftime("%d %b %Y")

        lines.append(
            f"{emoji} **{req['type']}** — Request #{req['id']}\n"
            f"   📅 {start} → {end} ({req['days']:.0f} days)\n"
            f"   Status: **{req['status'].title()}**"
            + (f"\n   Manager note: _{req['note']}_" if req.get("note") else "")
        )

    pending_count = sum(1 for r in all_requests if r["status"] == "pending")
    if pending_count > 0:
        lines.append(
            f"\n⏳ You have **{pending_count}** pending request(s) "
            f"awaiting manager approval."
        )

    return "\n\n".join(lines)


# ─────────────────────────────────────────────
# ACTION CONFIRMATION MESSAGES
# ─────────────────────────────────────────────

def build_leave_confirmation(
    leave_type: str,
    start_date: str,
    end_date: str,
    days: Optional[int],
    balance_remaining: float
) -> str:
    """Build confirmation message before submitting leave"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    if days is None:
        days = _count_working_days(start_date, end_date)

    remaining_after = balance_remaining - days

    emoji = {"annual": "🏖️", "sick": "🤒", "emergency": "🚨"}.get(leave_type, "📋")

    warning = ""
    if remaining_after < 0:
        warning = (
            f"\n\n⚠️ **Warning:** This exceeds your balance by "
            f"**{abs(remaining_after):.0f} days**."
        )
    elif remaining_after == 0:
        warning = "\n\n⚠️ This will use your **entire** remaining balance."
    elif remaining_after <= 2:
        warning = (
            f"\n\n⚠️ You'll only have **{remaining_after:.0f} day(s)** "
            f"remaining after this."
        )

    return (
        f"Here's your **{leave_type.title()} Leave** request summary:\n\n"
        f"{emoji} **Type:** {leave_type.title()} Leave\n"
        f"📅 **From:** {start.strftime('%A, %d %B %Y')}\n"
        f"📅 **To:** {end.strftime('%A, %d %B %Y')}\n"
        f"⏱️ **Duration:** {days} working day(s)\n"
        f"📊 **Current Balance:** {balance_remaining:.0f} days\n"
        f"📊 **Remaining After:** {max(0, remaining_after):.0f} days"
        f"{warning}\n\n"
        f"Reply **yes** to submit this request, or **no** to cancel."
    )


def build_wfh_confirmation(
    start_date: str,
    end_date: str,
    days: Optional[int]
) -> str:
    """Build WFH confirmation message"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    if days is None:
        days = _count_working_days(start_date, end_date)

    return (
        f"Here's your **Work From Home** request summary:\n\n"
        f"🏠 **Type:** Work From Home\n"
        f"📅 **From:** {start.strftime('%A, %d %B %Y')}\n"
        f"📅 **To:** {end.strftime('%A, %d %B %Y')}\n"
        f"⏱️ **Duration:** {days} working day(s)\n\n"
        f"This will be sent to your manager for approval.\n\n"
        f"Reply **yes** to submit, or **no** to cancel."
    )


def build_missing_info_prompt(
    action_type: str,
    leave_type: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    days: Optional[int]
) -> str:
    """Ask for missing info in a natural conversational way"""
    missing = []

    if action_type == "apply_leave" and not leave_type:
        missing.append("**What type of leave?** (annual / sick / emergency)")

    if not start_date:
        missing.append("**When would you like to start?** (e.g. next Monday, 19th May)")

    if not end_date and not days:
        missing.append("**How many days** do you need?")

    if not missing:
        return ""

    type_label = {
        "apply_leave": "leave",
        "apply_wfh":   "Work From Home"
    }.get(action_type, "request")

    intro = f"I'd love to help you apply for {type_label}! I just need a few more details:\n\n"
    items = "\n".join(f"• {m}" for m in missing)
    example = _get_example(action_type, leave_type)

    return intro + items + (f"\n\n💡 {example}" if example else "")


def _get_example(action_type: str, leave_type: Optional[str]) -> str:
    """Return a helpful example based on action type"""
    if action_type == "apply_wfh":
        return 'Example: _"I want to work from home next Monday for 2 days"_'
    if leave_type == "sick":
        return 'Example: _"I need sick leave from tomorrow for 3 days"_'
    if leave_type == "emergency":
        return 'Example: _"Emergency leave from 19th May to 20th May"_'
    return 'Example: _"I want 3 days annual leave from next Monday"_'


# ─────────────────────────────────────────────
# SUCCESS MESSAGES
# ─────────────────────────────────────────────

def build_success_message(
    request_id: int,
    action_type: str,
    leave_type: Optional[str],
    start_date: str,
    end_date: str,
    days: int,
    remaining_after: Optional[float] = None
) -> str:
    """Build success message after request submitted"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    if action_type == "apply_wfh":
        emoji = "🏠"
        type_label = "Work From Home"
    else:
        emoji = {"annual": "🏖️", "sick": "🤒", "emergency": "🚨"}.get(
            leave_type or "", "📋"
        )
        type_label = f"{(leave_type or '').title()} Leave"

    balance_line = ""
    if remaining_after is not None and action_type != "apply_wfh":
        balance_line = f"\n📊 Remaining balance: **{remaining_after:.0f} days**"

    return (
        f"✅ **Request submitted successfully!**\n\n"
        f"{emoji} **{type_label}** — Request #{request_id}\n"
        f"📅 {start.strftime('%d %b')} → {end.strftime('%d %b %Y')}\n"
        f"⏱️ {days} working day(s)"
        f"{balance_line}\n"
        f"⏳ Status: **Pending Manager Approval**\n\n"
        f"Your manager will review and notify you of the decision.\n"
        f"_You can check the status anytime by asking 'what is my request status'_"
    )


def build_cancel_success(request_type: str) -> str:
    return (
        f"✅ Your **{request_type}** request has been cancelled successfully.\n\n"
        f"Your leave balance remains unchanged."
    )


def build_no_pending_message() -> str:
    return (
        "There's nothing pending to confirm.\n\n"
        "What would you like to do? I can help you:\n"
        "• Apply for annual, sick, or emergency leave\n"
        "• Submit a work from home request\n"
        "• Check your leave balance\n"
        "• Check company policies"
    )
