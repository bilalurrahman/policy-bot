from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from types import SimpleNamespace
import httpx
import os
from datetime import datetime, timedelta, timezone

# Load environment-specific .env file
_env      = os.getenv("ENVIRONMENT", "development")
_env_file = f".env.{_env}"
load_dotenv(_env_file if os.path.exists(_env_file) else ".env")

from database import get_db, Employee, LeaveBalance, PendingAction, init_db
from auth import authenticate_employee, create_token, get_current_employee, hash_password
from llm_classifier import classify_with_llm, Bucket, ActionType
from response_builder import (
    build_balance_response,
    build_requests_response,
    build_leave_confirmation,
    build_wfh_confirmation,
    build_missing_info_prompt,
    build_success_message,
    build_no_pending_message,
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
OPENWEBUI_URL     = os.getenv("OPENWEBUI_URL", "http://host.docker.internal:3000")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY")
COLLECTION_ID     = os.getenv("COLLECTION_ID")
MODEL_ID          = os.getenv("MODEL_ID", "hr-policy")
HRMS_API_URL      = os.getenv("HRMS_API_URL", "http://host.docker.internal:5100")

PENDING_ACTION_EXPIRY_MIN = 10

SYSTEM_PROMPT = """You are a professional HR Policy Assistant for our company.

STRICT RULES:
1. Answer ONLY from the provided company policy documents.
2. If truly not found, say: "Please contact HR directly."
3. Be comprehensive — list ALL relevant policy details.
4. Format with bullet points for clarity.
5. Never make up information.
"""

# ─────────────────────────────────────────────
app = FastAPI(title="HR Policy Bot API v4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    init_db()


# ─────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────
class LoginRequest(BaseModel):
    employee_code: str
    password: str

class LoginResponse(BaseModel):
    token: str
    employee_name: str
    employee_code: str
    department: str

class ChatRequest(BaseModel):
    question: str
    history: list[dict] = []

class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []
    query_type: str  # "policy" | "personal" | "action"


# ─────────────────────────────────────────────
# HRMS RESPONSE NORMALISATION
# HRMS returns camelCase JSON; response_builder uses snake_case attributes.
# ─────────────────────────────────────────────
def _norm_leave_request(r: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id            = r.get("id"),
        leave_type    = r.get("leaveType") or "leave",
        start_date    = r.get("startDate"),
        end_date      = r.get("endDate"),
        days_requested= r.get("daysRequested") or 0,
        status        = r.get("status") or "pending",
        manager_note  = r.get("managerNote"),
        requested_at  = r.get("requestedAt") or "",
        category      = "leave",
    )

def _norm_wfh_request(r: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id            = r.get("id"),
        leave_type    = "work_from_home",
        start_date    = r.get("startDate"),
        end_date      = r.get("endDate"),
        days_requested= r.get("daysRequested") or 0,
        status        = r.get("status") or "pending",
        manager_note  = r.get("managerNote"),
        requested_at  = r.get("requestedAt") or "",
        category      = "wfh",
    )


# ─────────────────────────────────────────────
# PENDING ACTION CRUD
# ─────────────────────────────────────────────
def _get_pending(db: Session, employee_id: int):
    action = db.query(PendingAction).filter(
        PendingAction.employee_id == employee_id,
        PendingAction.status == "awaiting_confirmation",
    ).first()
    if not action:
        return None
    if datetime.now(timezone.utc).replace(tzinfo=None) > action.expires_at:
        action.status = "expired"
        db.commit()
        return None
    return action


def _create_pending(db: Session, employee_id: int, action_type: str, payload: dict):
    now     = datetime.now(timezone.utc).replace(tzinfo=None)
    expires = now + timedelta(minutes=PENDING_ACTION_EXPIRY_MIN)
    existing = db.query(PendingAction).filter(
        PendingAction.employee_id == employee_id
    ).first()
    if existing:
        existing.action_type = action_type
        existing.payload     = payload
        existing.status      = "awaiting_confirmation"
        existing.created_at  = now
        existing.expires_at  = expires
        db.commit()
        db.refresh(existing)
        return existing
    action = PendingAction(
        employee_id=employee_id,
        action_type=action_type,
        payload=payload,
        status="awaiting_confirmation",
        created_at=now,
        expires_at=expires,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def _clear_pending(db: Session, employee_id: int):
    action = db.query(PendingAction).filter(
        PendingAction.employee_id == employee_id
    ).first()
    if action:
        db.delete(action)
        db.commit()


# ─────────────────────────────────────────────
# HRMS API HELPERS
# ─────────────────────────────────────────────
def _hrms_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _submit_leave(token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{HRMS_API_URL}/api/bot/leave-request",
            json={
                "leaveType":     payload["leave_type"],
                "startDate":     payload["start_date"],
                "endDate":       payload["end_date"],
                "daysRequested": payload.get("days") or 1,
                "reason":        payload.get("reason") or "Requested via HR Chatbot",
            },
            headers=_hrms_headers(token),
        )
    return {"status_code": r.status_code, "data": r.json()}


async def _submit_wfh(token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{HRMS_API_URL}/api/bot/wfh-request",
            json={
                "startDate":     payload["start_date"],
                "endDate":       payload["end_date"],
                "daysRequested": payload.get("days") or 1,
                "reason":        payload.get("reason") or "Requested via HR Chatbot",
            },
            headers=_hrms_headers(token),
        )
    return {"status_code": r.status_code, "data": r.json()}


async def _fetch_my_requests(token: str) -> tuple[list, list]:
    """Returns (leave_requests, wfh_requests) as normalised SimpleNamespace lists."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{HRMS_API_URL}/api/bot/my-requests",
                headers=_hrms_headers(token),
            )
        if r.status_code == 200:
            data = r.json().get("data") or {}
            leave_reqs = [_norm_leave_request(x) for x in (data.get("leaveRequests") or [])]
            wfh_reqs   = [_norm_wfh_request(x)   for x in (data.get("wfhRequests")   or [])]
            return leave_reqs, wfh_reqs
    except httpx.ConnectError:
        pass
    return [], []


# ─────────────────────────────────────────────
# EXECUTE CONFIRMED ACTIONS
# ─────────────────────────────────────────────
async def _execute_leave(
    db: Session, employee, payload: dict, token: str, current_year: int
) -> str:
    try:
        result = await _submit_leave(token, payload)
        if result["status_code"] in (200, 201):
            _clear_pending(db, employee.id)
            request_id = ((result["data"] or {}).get("data") or {}).get("id", 0)
            balance = db.query(LeaveBalance).filter(
                LeaveBalance.employee_id == employee.id,
                LeaveBalance.year        == current_year,
                LeaveBalance.leave_type  == payload["leave_type"],
            ).first()
            remaining_after = None
            if balance and payload.get("days"):
                remaining_after = float(balance.remaining or 0) - payload["days"]
            return build_success_message(
                request_id      = request_id,
                action_type     = "apply_leave",
                leave_type      = payload["leave_type"],
                start_date      = payload["start_date"],
                end_date        = payload["end_date"],
                days            = payload.get("days") or 1,
                remaining_after = remaining_after,
            )
        error = ((result["data"] or {}).get("error") or "Please try again.")
        return f"❌ Could not submit your leave request: {error}"
    except httpx.ConnectError:
        return "❌ Could not connect to the HR system. Please try again or use the HR dashboard."


async def _execute_wfh(db: Session, employee, payload: dict, token: str) -> str:
    try:
        result = await _submit_wfh(token, payload)
        if result["status_code"] in (200, 201):
            _clear_pending(db, employee.id)
            request_id = ((result["data"] or {}).get("data") or {}).get("id", 0)
            return build_success_message(
                request_id  = request_id,
                action_type = "apply_wfh",
                leave_type  = None,
                start_date  = payload["start_date"],
                end_date    = payload["end_date"],
                days        = payload.get("days") or 1,
            )
        error = ((result["data"] or {}).get("error") or "Please try again.")
        return f"❌ Could not submit your WFH request: {error}"
    except httpx.ConnectError:
        return "❌ Could not connect to the HR system. Please try again or use the HR dashboard."


# ─────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────
@app.post("/api/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    employee = authenticate_employee(db, req.employee_code, req.password)
    if not employee:
        raise HTTPException(status_code=401, detail="Invalid employee code or password.")
    token = create_token(employee.id, employee.employee_code)
    return LoginResponse(
        token         = token,
        employee_name = employee.full_name,
        employee_code = employee.employee_code,
        department    = employee.department or "N/A",
    )

@app.get("/api/auth/me")
def get_me(current_employee: Employee = Depends(get_current_employee)):
    return {
        "employee_code": current_employee.employee_code,
        "full_name":     current_employee.full_name,
        "department":    current_employee.department,
        "email":         current_employee.email,
    }


# ─────────────────────────────────────────────
# CHAT ENDPOINT
# ─────────────────────────────────────────────
@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    current_employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    raw_token    = request.headers.get("authorization", "").replace("Bearer ", "", 1)
    current_year = datetime.now().year

    # ── 1. Pending action check ───────────────────────────────────────────────
    pending = _get_pending(db, current_employee.id)

    # ── 2. LLM classification ─────────────────────────────────────────────────
    clf        = await classify_with_llm(req.question, has_pending=pending is not None)
    bucket     = clf["bucket"]
    action_type= clf["action_type"]
    leave_type = clf.get("leave_type")
    start_date = clf.get("start_date")
    end_date   = clf.get("end_date")
    days       = clf.get("days")

    # ── 3. ACTION bucket ──────────────────────────────────────────────────────
    if bucket == Bucket.ACTION.value:

        # Confirm a pending action
        if action_type == ActionType.CONFIRM.value:
            if not pending:
                return ChatResponse(
                    answer=build_no_pending_message(), sources=[], query_type="action"
                )
            if pending.action_type == "apply_leave":
                answer = await _execute_leave(db, current_employee, pending.payload, raw_token, current_year)
            else:
                answer = await _execute_wfh(db, current_employee, pending.payload, raw_token)
            return ChatResponse(answer=answer, sources=[], query_type="action")

        # Reject a pending action
        if action_type == ActionType.REJECT.value:
            if pending:
                _clear_pending(db, current_employee.id)
            return ChatResponse(
                answer="Got it, I've cancelled that. Is there anything else I can help you with?",
                sources=[],
                query_type="action",
            )

        # Check request status
        if action_type == ActionType.CHECK_STATUS.value:
            leave_reqs, wfh_reqs = await _fetch_my_requests(raw_token)
            return ChatResponse(
                answer=build_requests_response(leave_reqs, wfh_reqs),
                sources=[],
                query_type="action",
            )

        # Cancel request — redirect to dashboard + show current requests
        if action_type == ActionType.CANCEL_REQUEST.value:
            leave_reqs, wfh_reqs = await _fetch_my_requests(raw_token)
            status_text = build_requests_response(leave_reqs, wfh_reqs)
            return ChatResponse(
                answer=(
                    "To cancel a request, visit the **HRMS dashboard** and click the Cancel button.\n\n"
                    + status_text
                ),
                sources=[],
                query_type="action",
            )

        # Apply leave
        if action_type == ActionType.APPLY_LEAVE.value:
            missing = build_missing_info_prompt("apply_leave", leave_type, start_date, end_date, days)
            if missing:
                return ChatResponse(answer=missing, sources=[], query_type="action")

            balance = db.query(LeaveBalance).filter(
                LeaveBalance.employee_id == current_employee.id,
                LeaveBalance.year        == current_year,
                LeaveBalance.leave_type  == leave_type,
            ).first()
            balance_remaining = float(balance.remaining) if balance else 0.0

            payload = {
                "leave_type": leave_type,
                "start_date": start_date,
                "end_date":   end_date,
                "days":       days,
                "reason":     "",
            }
            _create_pending(db, current_employee.id, "apply_leave", payload)
            answer = build_leave_confirmation(leave_type, start_date, end_date, days, balance_remaining)
            return ChatResponse(answer=answer, sources=[], query_type="action")

        # Apply WFH
        if action_type == ActionType.APPLY_WFH.value:
            missing = build_missing_info_prompt("apply_wfh", None, start_date, end_date, days)
            if missing:
                return ChatResponse(answer=missing, sources=[], query_type="action")

            payload = {
                "start_date": start_date,
                "end_date":   end_date,
                "days":       days,
                "reason":     "",
            }
            _create_pending(db, current_employee.id, "apply_wfh", payload)
            answer = build_wfh_confirmation(start_date, end_date, days)
            return ChatResponse(answer=answer, sources=[], query_type="action")

    # ── 4. PERSONAL bucket — fetch from DB + HRMS, return formatted data ──────
    if bucket == Bucket.PERSONAL.value:
        balances = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == current_employee.id,
            LeaveBalance.year        == current_year,
        ).all()
        leave_reqs, wfh_reqs = await _fetch_my_requests(raw_token)
        all_requests = leave_reqs + wfh_reqs
        return ChatResponse(
            answer=build_balance_response(current_employee, balances, all_requests),
            sources=[],
            query_type="personal",
        )

    # ── 5. POLICY bucket — RAG pipeline ──────────────────────────────────────
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in req.history:
        messages.append(turn)
    messages.append({"role": "user", "content": req.question})

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            response = await client.post(
                f"{OPENWEBUI_URL}/api/chat/completions",
                json={
                    "model":    MODEL_ID,
                    "messages": messages,
                    "files":    [{"type": "collection", "id": COLLECTION_ID}],
                    "stream":   False,
                },
                headers={
                    "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
                    "Content-Type":  "application/json",
                },
            )
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"LLM error: {response.text}",
            )
        data    = response.json()
        answer  = data["choices"][0]["message"]["content"]
        sources = [c.get("source", "") for c in data.get("citations", [])]
        return ChatResponse(answer=answer, sources=sources, query_type="policy")

    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot connect to LLM service.")


# ─────────────────────────────────────────────
# LEAVE BALANCE ENDPOINT (used by frontend sidebar)
# ─────────────────────────────────────────────
@app.get("/api/my/leave-balance")
def get_leave_balance(
    current_employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    current_year = datetime.now().year
    balances = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == current_employee.id,
        LeaveBalance.year        == current_year,
    ).all()
    return {
        "employee": current_employee.full_name,
        "year":     current_year,
        "balances": [
            {
                "type":          b.leave_type,
                "total_allowed": b.total_allowed,
                "taken":         b.taken,
                "remaining":     b.remaining,
            }
            for b in balances
        ],
    }


# ─────────────────────────────────────────────
# ADMIN — CREATE EMPLOYEE
# ─────────────────────────────────────────────
@app.post("/api/admin/create-employee")
def create_employee(
    employee_code: str,
    full_name: str,
    email: str,
    password: str,
    department: str,
    admin_key: str,
    db: Session = Depends(get_db),
):
    if admin_key != os.getenv("ADMIN_KEY", "changeme"):
        raise HTTPException(status_code=403, detail="Invalid admin key")

    if db.query(Employee).filter(Employee.employee_code == employee_code).first():
        raise HTTPException(status_code=400, detail="Employee code already exists")

    emp = Employee(
        employee_code = employee_code,
        full_name     = full_name,
        email         = email,
        password_hash = hash_password(password),
        department    = department,
        is_active     = True,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    current_year = datetime.now().year
    for leave_type, total in [("annual", 21.0), ("sick", 30.0), ("emergency", 3.0)]:
        db.add(LeaveBalance(
            employee_id   = emp.id,
            year          = current_year,
            leave_type    = leave_type,
            total_allowed = total,
            taken         = 0,
            remaining     = total,
        ))
    db.commit()

    return {"message": f"Employee {full_name} created successfully", "id": emp.id}


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "model": MODEL_ID, "collection": COLLECTION_ID}
