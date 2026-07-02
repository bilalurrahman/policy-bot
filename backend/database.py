from sqlalchemy import create_engine, Column, Integer, String, Date, Boolean, ForeignKey, Float, DateTime, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

# ─────────────────────────────────────────────
# CONNECTION — reads from .env
# ─────────────────────────────────────────────
# Format: mssql+pyodbc://user:password@server/database?driver=ODBC+Driver+17+for+SQL+Server
DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ─────────────────────────────────────────────
# TABLE MODELS
# ─────────────────────────────────────────────

class Employee(Base):
    __tablename__ = "employees"

    id            = Column(Integer, primary_key=True, index=True)
    employee_code = Column(String(20), unique=True, nullable=False)   # EMP001
    full_name     = Column(String(100), nullable=False)
    email         = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    department    = Column(String(100))
    joining_date  = Column(Date)
    is_active     = Column(Boolean, default=True)
    is_manager    = Column(Boolean, default=False)

    # Relationships
    leave_balances     = relationship("LeaveBalance",     back_populates="employee")
    leave_transactions = relationship("LeaveTransaction", back_populates="employee")
    chat_sessions      = relationship("ChatSession",      back_populates="employee")
    pending_actions    = relationship("PendingAction",    back_populates="employee", uselist=False)


class LeaveBalance(Base):
    __tablename__ = "leave_balances"

    id            = Column(Integer, primary_key=True, index=True)
    employee_id   = Column(Integer, ForeignKey("employees.id"), nullable=False)
    year          = Column(Integer, nullable=False)        # 2026
    leave_type    = Column(String(50), nullable=False)     # annual / sick / emergency
    total_allowed = Column(Float, nullable=False)          # 21
    taken         = Column(Float, default=0)               # 4
    remaining     = Column(Float, nullable=False)          # 17

    employee = relationship("Employee", back_populates="leave_balances")


class LeaveTransaction(Base):
    __tablename__ = "leave_transactions"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    leave_type  = Column(String(50), nullable=False)
    start_date  = Column(Date, nullable=False)
    end_date    = Column(Date, nullable=False)
    days_taken  = Column(Float, nullable=False)
    status      = Column(String(20), default="approved")   # approved/pending/rejected
    notes       = Column(String(255))

    employee = relationship("Employee", back_populates="leave_transactions")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    question    = Column(String,  nullable=False)
    answer      = Column(String,  nullable=False)
    query_type  = Column(String(20))
    created_at  = Column(DateTime, default=datetime.utcnow)

    employee = relationship("Employee", back_populates="chat_sessions")


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, unique=True)
    action_type = Column(String(50), nullable=False)
    payload     = Column(JSON,   nullable=False)
    status      = Column(String(100), default="awaiting_confirmation")
    created_at  = Column(DateTime,   default=datetime.utcnow)
    expires_at  = Column(DateTime,   nullable=False)

    employee = relationship("Employee", back_populates="pending_actions")


# ─────────────────────────────────────────────
# HELPER — get DB session
# ─────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────
# CREATE TABLES (run once on startup)
# ─────────────────────────────────────────────
def init_db():
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified")