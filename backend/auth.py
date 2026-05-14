from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db, Employee
import os

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SECRET_KEY      = os.getenv("JWT_SECRET_KEY", "change-this-to-random-string-in-production")
ALGORITHM       = "HS256"
TOKEN_EXPIRE_HOURS = 8   # employee stays logged in for 8 hours

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12
)
bearer_scheme = HTTPBearer()


# ─────────────────────────────────────────────
# PASSWORD HELPERS
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─────────────────────────────────────────────
# JWT HELPERS
# ─────────────────────────────────────────────
def create_token(employee_id: int, employee_code: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(employee_id),
        "code": employee_code,
        "exp": expire
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ─────────────────────────────────────────────
# AUTHENTICATE EMPLOYEE (used on login)
# ─────────────────────────────────────────────
def authenticate_employee(db: Session, employee_code: str, password: str) -> Optional[Employee]:
    employee = db.query(Employee).filter(
        Employee.employee_code == employee_code,
        Employee.is_active == True
    ).first()

    if not employee:
        return None
    if not verify_password(password, employee.password_hash):
        return None
    return employee


# ─────────────────────────────────────────────
# GET CURRENT EMPLOYEE (used on every protected route)
# ─────────────────────────────────────────────
def get_current_employee(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db)
) -> Employee:
    token = credentials.credentials
    payload = decode_token(token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please login again.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    employee = db.query(Employee).filter(
        Employee.id == int(payload["sub"]),
        Employee.is_active == True
    ).first()

    if not employee:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Employee account not found or deactivated."
        )

    return employee