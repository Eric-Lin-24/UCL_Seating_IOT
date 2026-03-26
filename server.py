from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker

# =========================================================
# CONFIG
# =========================================================
RESERVATION_MINUTES = 60
NO_SHOW_MINUTES = 10
WARNING_MINUTES = 5

DATABASE_URL = "sqlite:///./seat_system.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

app = FastAPI(title="IoT Seat Check-in Backend")


def now_utc() -> datetime:
    return datetime.utcnow()


def normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# =========================================================
# DATABASE MODELS
# =========================================================
class MockStudentRFID(Base):
    __tablename__ = "mock_student_rfid"

    id = Column(Integer, primary_key=True, index=True)
    rfid_uid = Column(String, unique=True, index=True, nullable=False)
    student_id = Column(String, unique=True, index=True, nullable=False)
    student_name = Column(String, nullable=True)
    active = Column(Boolean, default=True)


class SeatSession(Base):
    __tablename__ = "seat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    seat_id = Column(String, index=True, nullable=False)

    student_id = Column(String, nullable=False)
    rfid_uid = Column(String, nullable=True)

    # reserved_no_show / occupied / expired
    status = Column(String, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
    reservation_start = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    checked_in_at = Column(DateTime(timezone=True), nullable=True)

    # Optional future use if you add a physical seat sensor later
    presence_detected = Column(Boolean, default=False)

    active = Column(Boolean, default=True)


Base.metadata.create_all(bind=engine)


# =========================================================
# EDIT THIS SECTION: YOUR TEST STUDENTS / RFID CARDS
# =========================================================
# Format:
# ("rfid_uid", "student_id", "student_name")
#
# Example RFID format from the Arduino code:
# "12-34-56-78"
#
# Replace these demo values with your own test cards.

TEST_STUDENTS = [
    ("866-865-866", "ucl123456", "Alice Demo"),
    ("123-456-789", "ucl654321", "Bob Demo"),

    # ADD YOUR OWN TEST ENTRIES BELOW:
    # ("YOUR-RFID-UID-HERE", "ucl999999", "Your Name"),
    # ("12-34-56-78", "ucl000001", "Eric Lin"),
]


# =========================================================
# SEED MOCK STUDENT DATABASE
# =========================================================
def seed_mock_students():
    db = SessionLocal()
    try:
        for rfid_uid, student_id, student_name in TEST_STUDENTS:
            existing = db.query(MockStudentRFID).filter(
                MockStudentRFID.rfid_uid == rfid_uid
            ).first()

            if existing is None:
                db.add(
                    MockStudentRFID(
                        rfid_uid=rfid_uid,
                        student_id=student_id,
                        student_name=student_name,
                        active=True
                    )
                )

        db.commit()
    finally:
        db.close()


seed_mock_students()


# =========================================================
# REQUEST / RESPONSE SCHEMAS
# =========================================================
class RegisterSeatRequest(BaseModel):
    seat_id: str


class ReserveSeatRequest(BaseModel):
    seat_id: str
    student_id: str


class TapRequest(BaseModel):
    seat_id: str
    rfid_uid: str
    user_id: Optional[str] = None
    action: str  # "checkin" or "checkout"


class SeatStateResponse(BaseModel):
    seat_id: str
    state: str
    owner_student_id: Optional[str] = None
    expires_at: Optional[str] = None
    seconds_left: Optional[int] = None


# =========================================================
# HELPERS
# =========================================================
def lookup_student_by_rfid(db, rfid_uid: str) -> Optional[MockStudentRFID]:
    return (
        db.query(MockStudentRFID)
        .filter(
            MockStudentRFID.rfid_uid == rfid_uid,
            MockStudentRFID.active == True
        )
        .first()
    )


def cleanup_expired(db) -> None:
    current = now_utc()

    sessions = db.query(SeatSession).filter(SeatSession.active == True).all()

    for session in sessions:
        expires_at = normalize_utc_datetime(session.expires_at)
        reservation_start = normalize_utc_datetime(session.reservation_start)

        # Full session expired
        if current >= expires_at:
            session.active = False
            session.status = "expired"
            continue

        # Reserved but no one checked in within no-show window
        if (
            session.status == "reserved_no_show"
            and session.checked_in_at is None
            and current >= reservation_start + timedelta(minutes=NO_SHOW_MINUTES)
        ):
            session.active = False
            session.status = "expired"

    db.commit()


def get_active_session(db, seat_id: str) -> Optional[SeatSession]:
    cleanup_expired(db)
    return (
        db.query(SeatSession)
        .filter(
            SeatSession.seat_id == seat_id,
            SeatSession.active == True
        )
        .order_by(SeatSession.id.desc())
        .first()
    )


def compute_display_state(session: Optional[SeatSession]):
    if session is None:
        return "OPEN", None

    current = now_utc()
    expires_at = normalize_utc_datetime(session.expires_at)
    seconds_left = int((expires_at - current).total_seconds())
    if seconds_left < 0:
        seconds_left = 0

    if session.status == "reserved_no_show":
        return "RESERVED_NO_SHOW", seconds_left

    if session.status == "occupied":
        if expires_at - current <= timedelta(minutes=WARNING_MINUTES):
            return "OCCUPIED_WARNING", seconds_left
        return "OCCUPIED", seconds_left

    return "OPEN", None


# =========================================================
# API ENDPOINTS
# =========================================================
@app.get("/")
def root():
    return {
        "ok": True,
        "message": "IoT Seat Check-in Backend is running"
    }


@app.post("/register-seat")
def register_seat(req: RegisterSeatRequest):
    return {
        "ok": True,
        "seat_id": req.seat_id
    }


@app.post("/reserve")
def reserve_seat(req: ReserveSeatRequest):
    db = SessionLocal()
    try:
        active = get_active_session(db, req.seat_id)
        if active is not None:
            raise HTTPException(status_code=409, detail="Seat is not available")

        start = now_utc()
        expires = start + timedelta(minutes=RESERVATION_MINUTES)

        session = SeatSession(
            seat_id=req.seat_id,
            student_id=req.student_id,
            rfid_uid=None,
            status="reserved_no_show",
            created_at=start,
            reservation_start=start,
            expires_at=expires,
            checked_in_at=None,
            active=True,
        )

        db.add(session)
        db.commit()
        db.refresh(session)

        return {
            "ok": True,
            "message": "Seat reserved",
            "seat_id": req.seat_id,
            "student_id": req.student_id,
            "expires_at": session.expires_at.isoformat(),
        }
    finally:
        db.close()

@app.post("/tap")
def tap_card(req: TapRequest):
    db = SessionLocal()
    try:
        active = get_active_session(db, req.seat_id)
        current = now_utc()
        action = req.action.strip().lower()

        student = lookup_student_by_rfid(db, req.rfid_uid)
        if student is None:
            return {"ok": False, "action": "unknown_rfid"}

        student_id = student.student_id

        # -------------------------
        # CHECKOUT (button pressed)
        # -------------------------
        if action == "checkout":
            if active is None:
                return {"ok": False, "action": "no_active_session"}

            if active.student_id != student_id:
                return {"ok": False, "action": "not_owner"}

            active.active = False
            active.status = "expired"
            db.commit()

            return {
                "ok": True,
                "action": "checked_out",
                "seat_id": req.seat_id,
            }

        # -------------------------
        # CHECKIN (normal tap)
        # -------------------------
        if action == "checkin":

            # Seat open → new session
            if active is None:
                expires = current + timedelta(minutes=RESERVATION_MINUTES)

                session = SeatSession(
                    seat_id=req.seat_id,
                    student_id=student_id,
                    rfid_uid=req.rfid_uid,
                    status="occupied",
                    created_at=current,
                    reservation_start=current,
                    expires_at=expires,
                    checked_in_at=current,
                    active=True,
                )
                db.add(session)
                db.commit()

                return {
                    "ok": True,
                    "action": "checked_in",
                    "seat_id": req.seat_id,
                    "expires_at": expires.isoformat(),
                }

            # Reserved by same user
            if active.status == "reserved_no_show":
                if active.student_id == student_id:
                    active.status = "occupied"
                    active.checked_in_at = current
                    active.rfid_uid = req.rfid_uid
                    db.commit()

                    return {
                        "ok": True,
                        "action": "reservation_checked_in",
                        "seat_id": req.seat_id,
                    }
                else:
                    return {
                        "ok": False,
                        "action": "denied_reserved_for_someone_else",
                    }

            # Occupied
            if active.status == "occupied":
                if active.student_id == student_id:
                    # DO NOTHING (must press button to checkout)
                    return {
                        "ok": True,
                        "action": "already_checked_in",
                    }
                else:
                    return {
                        "ok": False,
                        "action": "denied_occupied",
                    }

        return {"ok": False, "action": "invalid_action"}

    finally:
        db.close()


@app.get("/seat/{seat_id}", response_model=SeatStateResponse)
def get_seat_state(seat_id: str):
    db = SessionLocal()
    try:
        active = get_active_session(db, seat_id)
        state, seconds_left = compute_display_state(active)

        return SeatStateResponse(
            seat_id=seat_id,
            state=state,
            owner_student_id=active.student_id if active else None,
            expires_at=active.expires_at.isoformat() if active else None,
            seconds_left=seconds_left,
        )
    finally:
        db.close()


@app.get("/students")
def list_students():
    db = SessionLocal()
    try:
        students = db.query(MockStudentRFID).all()
        return [
            {
                "rfid_uid": student.rfid_uid,
                "student_id": student.student_id,
                "student_name": student.student_name,
                "active": student.active,
            }
            for student in students
        ]
    finally:
        db.close()


@app.get("/sessions")
def list_sessions():
    db = SessionLocal()
    try:
        sessions = db.query(SeatSession).order_by(SeatSession.id.desc()).all()
        return [
            {
                "id": session.id,
                "seat_id": session.seat_id,
                "student_id": session.student_id,
                "rfid_uid": session.rfid_uid,
                "status": session.status,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "reservation_start": session.reservation_start.isoformat() if session.reservation_start else None,
                "checked_in_at": session.checked_in_at.isoformat() if session.checked_in_at else None,
                "expires_at": session.expires_at.isoformat() if session.expires_at else None,
                "active": session.active,
            }
            for session in sessions
        ]
    finally:
        db.close()