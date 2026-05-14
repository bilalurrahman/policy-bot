from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv
import httpx
import os
from datetime import datetime

load_dotenv()

from database import get_db, Employee, LeaveBalance, LeaveTransaction, init_db
from auth import authenticate_employee, create_token, get_current_employee, hash_password
from query_classifier import classify_query, QueryType, get_personal_context

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
OPENWEBUI_URL     = os.getenv("OPENWEBUI_URL", "http://host.docker.internal:3000")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY")
COLLECTION_ID     = os.getenv("COLLECTION_ID")
MODEL_ID          = os.getenv("MODEL_ID", "hr-policy-bot")

SYSTEM_PROMPT = """You are a professional HR Policy Assistant for our company.

STRICT RULES:
1. Answer ONLY from the provided company policy documents and employee personal data.
2. IMPORTANT: When EMPLOYEE PERSONAL DATA is provided in the context:
   - Use it DIRECTLY to answer personal questions
   - Always state exact numbers: days taken, days remaining
   - Never ask the employee for information you already have
   - Example: if data shows remaining=17, say "You have 17 days remaining"
3. Never say "I need to know your years of service" if the data is already provided.
4. If truly not found anywhere, say: "Please contact HR directly."
5. Be comprehensive — list ALL relevant policy details.
6. Format with bullet points for clarity.
7. Never make up information.
"""

# ─────────────────────────────────────────────
app = FastAPI(title="HR Policy Bot API v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create DB tables on startup
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
    query_type: str  # "policy" or "personal" — useful for frontend


# ─────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────
@app.post("/api/auth/login", response_model=LoginResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    employee = authenticate_employee(db, req.employee_code, req.password)

    if not employee:
        raise HTTPException(
            status_code=401,
            detail="Invalid employee code or password."
        )

    token = create_token(employee.id, employee.employee_code)

    return LoginResponse(
        token=token,
        employee_name=employee.full_name,
        employee_code=employee.employee_code,
        department=employee.department or "N/A"
    )

@app.get("/api/auth/me")
def get_me(current_employee: Employee = Depends(get_current_employee)):
    return {
        "employee_code": current_employee.employee_code,
        "full_name": current_employee.full_name,
        "department": current_employee.department,
        "email": current_employee.email
    }


# ─────────────────────────────────────────────
# CHAT ENDPOINT (protected)
# ─────────────────────────────────────────────
@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    current_employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db)
):
    current_year = datetime.now().year

    # 1. Classify the query
    query_type = classify_query(req.question)

    # 2. If personal/combined — fetch employee data from DB
    personal_context = ""
    if query_type in [QueryType.PERSONAL, QueryType.COMBINED]:
        balances = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == current_employee.id,
            LeaveBalance.year == current_year
        ).all()

        transactions = db.query(LeaveTransaction).filter(
            LeaveTransaction.employee_id == current_employee.id
        ).order_by(LeaveTransaction.start_date.desc()).limit(10).all()

        personal_context = get_personal_context(
            current_employee, balances, transactions
        )

    # 3. Build messages for LLM
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Inject personal context as system-level info if available
    if personal_context:
        messages.append({
            "role": "system",
            "content": f"CURRENT EMPLOYEE CONTEXT:\n{personal_context}"
        })

    # Add conversation history
    for turn in req.history:
        messages.append(turn)

    # Add current question
    messages.append({"role": "user", "content": req.question})

    # 4. Call Open WebUI
    payload = {
        "model": MODEL_ID,
        "messages": messages,
        "files": [{"type": "collection", "id": COLLECTION_ID}],
        "stream": False
    }

    headers = {
        "Authorization": f"Bearer {OPENWEBUI_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{OPENWEBUI_URL}/api/chat/completions",
                json=payload,
                headers=headers
            )

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code,
                                detail=f"LLM error: {response.text}")

        data     = response.json()
        answer   = data["choices"][0]["message"]["content"]
        sources  = []
        if "citations" in data:
            sources = [c.get("source", "") for c in data["citations"]]

        return ChatResponse(
            answer=answer,
            sources=sources,
            query_type=query_type.value
        )

    except httpx.ConnectError:
        raise HTTPException(status_code=503,
                            detail="Cannot connect to LLM service.")


# ─────────────────────────────────────────────
# LEAVE BALANCE ENDPOINT (protected)
# ─────────────────────────────────────────────
@app.get("/api/my/leave-balance")
def get_leave_balance(
    current_employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db)
):
    current_year = datetime.now().year
    balances = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == current_employee.id,
        LeaveBalance.year == current_year
    ).all()

    return {
        "employee": current_employee.full_name,
        "year": current_year,
        "balances": [
            {
                "type": b.leave_type,
                "total_allowed": b.total_allowed,
                "taken": b.taken,
                "remaining": b.remaining
            }
            for b in balances
        ]
    }


# ─────────────────────────────────────────────
# ADMIN — CREATE EMPLOYEE (for setup only)
# ─────────────────────────────────────────────
@app.post("/api/admin/create-employee")
def create_employee(
    employee_code: str,
    full_name: str,
    email: str,
    password: str,
    department: str,
    admin_key: str,
    db: Session = Depends(get_db)
):
    # Simple admin key protection
    if admin_key != os.getenv("ADMIN_KEY", "changeme"):
        raise HTTPException(status_code=403, detail="Invalid admin key")

    existing = db.query(Employee).filter(
        Employee.employee_code == employee_code
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Employee code already exists")

    emp = Employee(
        employee_code=employee_code,
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        department=department,
        is_active=True
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    # Create default leave balances for current year
    current_year = datetime.now().year
    default_leaves = [
        ("annual",    21.0),
        ("sick",      30.0),
        ("emergency", 3.0),
    ]
    for leave_type, total in default_leaves:
        balance = LeaveBalance(
            employee_id=emp.id,
            year=current_year,
            leave_type=leave_type,
            total_allowed=total,
            taken=0,
            remaining=total
        )
        db.add(balance)
    db.commit()

    return {"message": f"Employee {full_name} created successfully", "id": emp.id}


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_ID,
        "collection": COLLECTION_ID
    }