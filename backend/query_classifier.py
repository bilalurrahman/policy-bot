from enum import Enum
import re

class QueryType(Enum):
    PERSONAL  = "personal"   # needs employee DB data
    POLICY    = "policy"     # needs RAG only
    COMBINED  = "combined"   # needs both DB + RAG

# ─────────────────────────────────────────────
# KEYWORDS THAT SIGNAL PERSONAL QUERY
# ─────────────────────────────────────────────
PERSONAL_KEYWORDS = [
    # Direct personal pronouns
    "my leave", "my balance", "my remaining", "my annual",
    "my sick", "my vacation", "my days", "my history",
    "my record", "my entitlement", "my allowance",
    "i have", "i took", "i have taken", "i used",
    "do i have", "have i", "can i take", "am i eligible",

    # Remaining/balance questions
    "how many annual leave days do i",
    "how many leave days do i",
    "how many days do i",
    "how many days have i",
    "how many vacation",
    "how much leave do i",
    "days left", "days remaining", "days i have",
    "leave left", "leave remaining", "balance left",
    "i have remaining", "remaining this year",
    "left this year", "taken so far",

    # Action queries
    "can i still take", "can i take",
    "do i have enough", "enough leave",
    "want to take", "planning to take",
]

POLICY_KEYWORDS = [
    "policy", "rule", "regulation", "allowed by", "according to",
    "company policy", "what is the", "how many days are",
    "entitlement for", "general", "all employees",
]

def classify_query(question: str) -> QueryType:
    """
    Determines whether a question needs:
    - Personal DB data only
    - Policy RAG only  
    - Both combined
    """
    q = question.lower().strip()

    has_personal = any(kw in q for kw in PERSONAL_KEYWORDS)
    has_policy   = any(kw in q for kw in POLICY_KEYWORDS)

    if has_personal and has_policy:
        return QueryType.COMBINED
    elif has_personal:
        return QueryType.COMBINED  # always combine — gives richer answer
    else:
        return QueryType.POLICY


def get_personal_context(employee, leave_balances, leave_transactions) -> str:
    """
    Builds a text summary of employee's personal data
    to inject into the LLM prompt alongside RAG context
    """
    if not leave_balances:
        return ""

    lines = [
        f"EMPLOYEE PERSONAL DATA:",
        f"- Name: {employee.full_name}",
        f"- Employee Code: {employee.employee_code}",
        f"- Department: {employee.department}",
        f"",
        f"LEAVE BALANCES (Current Year):",
    ]

    for balance in leave_balances:
        lines.append(
            f"- {balance.leave_type.title()} Leave: "
            f"{balance.taken} days taken out of {balance.total_allowed} allowed "
            f"→ {balance.remaining} days remaining"
        )

    if leave_transactions:
        lines.append(f"")
        lines.append(f"RECENT LEAVE HISTORY:")
        for tx in leave_transactions[:5]:  # last 5 transactions
            lines.append(
                f"- {tx.leave_type.title()} leave: "
                f"{tx.start_date} to {tx.end_date} "
                f"({tx.days_taken} days) — {tx.status}"
            )

    return "\n".join(lines)