from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker

# =========================
# Config
# =========================
RESERVATION_MINUTES = 60
NO_SHOW_MINUTES = 10
WARNING_MINUTES = 5

DATABASE_URL = "sqlite:///./seats.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

app = FastAPI(title="Seat Check-in Backend")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =========================
# DB Model
# =========================
class SeatSession(Base):
    __tablename__ = "seat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    seat_id = Column(String, index=True, nullable=False)

    # user identity
    user_id = Column(String, nullable=False)   # student ID / RFID owner / web user
    rfid_uid = Column(String, nullable=True)

    # session info
    status = Column(String, nullable=False)  # reserved_no_show / occupied / expired
    created_at = Column(DateTime(timezone=True), nullable=False)
    reservation_start = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    checked_in_at = Column(DateTime(timezone=True), nullable=True)

    # optional future sensor hook
    presence_detected = Column(Boolean, default=False)

    # active flag
    active = Column(Boolean, default=True)


Base.metadata.create_all(bind=engine)


# =========================
# Schemas
# =========================
class RegisterSeatRequest(BaseModel):
    seat_id: str


class ReserveSeatRequest(BaseModel):
    seat_id: str
    user_id: str


class TapRequest(BaseModel):
    seat_id: str
    rfid_uid: str
    user_id: str  # map card to student however you want


class SeatStateResponse(BaseModel):
    seat_id: str
    state: str
    owner_user_id: Optional[str] = None
    expires_at: Optional[str] = None
    seconds_left: Optional[int] = None


# =========================
# Utility logic
# =========================
def get_active_session(db, seat_id: str) -> Optional[SeatSession]:
    cleanup_expired(db)
    return (
        db.query(SeatSession)
        .filter(SeatSession.seat_id == seat_id, SeatSession.active == True)
        .order_by(SeatSession.id.desc())
        .first()
    )


def cleanup_expired(db) -> None:
    current = now_utc()
    sessions = db.query(SeatSession).filter(SeatSession.active == True).all()

    for s in sessions:
        # full session expired
        if current >= s.expires_at:
            s.active = False
            s.status = "expired"
            continue

        # reserved but never checked in within no-show window
        if (
            s.status == "reserved_no_show"
            and current >= s.reservation_start + timedelta(minutes=NO_SHOW_MINUTES)
            and s.checked_in_at is None
        ):
            s.active = False
            s.status = "expired"

    db.commit()


def compute_display_state(session: Optional[SeatSession]) -> tuple[str, Optional[int]]:
    if session is None:
        return "OPEN", None

    current = now_utc()
    seconds_left = int((session.expires_at - current).total_seconds())
    if seconds_left < 0:
        seconds_left = 0

    if session.status == "reserved_no_show":
        return "RESERVED_NO_SHOW", seconds_left

    if session.status == "occupied":
        if session.expires_at - current <= timedelta(minutes=WARNING_MINUTES):
            return "OCCUPIED_WARNING", seconds_left
        return "OCCUPIED", seconds_left

    return "OPEN", None


# =========================
# Endpoints
# =========================
@app.post("/register-seat")
def register_seat(req: RegisterSeatRequest):
    # For this MVP, registration is just an acknowledgement
    return {"ok": True, "seat_id": req.seat_id}


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
            user_id=req.user_id,
            rfid_uid=None,
            status="reserved_no_show",
            created_at=now_utc(),
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

        # Seat open -> direct walk-up check-in for 1 hour
        if active is None:
            expires = current + timedelta(minutes=RESERVATION_MINUTES)
            session = SeatSession(
                seat_id=req.seat_id,
                user_id=req.user_id,
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

        # Reserved by same person -> convert to occupied
        if active.status == "reserved_no_show":
            if active.user_id == req.user_id:
                active.status = "occupied"
                active.checked_in_at = current
                active.rfid_uid = req.rfid_uid
                db.commit()
                return {
                    "ok": True,
                    "action": "reservation_checked_in",
                    "seat_id": req.seat_id,
                    "expires_at": active.expires_at.isoformat(),
                }
            else:
                return {
                    "ok": False,
                    "action": "denied_reserved_for_someone_else",
                    "seat_id": req.seat_id,
                }

        # Occupied by same person -> optional early checkout
        if active.status == "occupied":
            if active.user_id == req.user_id:
                # toggle checkout
                active.active = False
                active.status = "expired"
                db.commit()
                return {
                    "ok": True,
                    "action": "checked_out",
                    "seat_id": req.seat_id,
                }
            else:
                return {
                    "ok": False,
                    "action": "denied_occupied",
                    "seat_id": req.seat_id,
                }

        return {"ok": False, "action": "unknown_state"}
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
            owner_user_id=active.user_id if active else None,
            expires_at=active.expires_at.isoformat() if active else None,
            seconds_left=seconds_left,
        )
    finally:
        db.close()


@app.post("/presence/{seat_id}")
def update_presence(seat_id: str, present: bool):
    """
    Optional endpoint for future pressure/IR seat sensor.
    """
    db = SessionLocal()
    try:
        active = get_active_session(db, seat_id)
        if active is None:
            return {"ok": False, "message": "No active session"}

        active.presence_detected = present
        db.commit()
        return {"ok": True, "seat_id": seat_id, "present": present}
    finally:
        db.close()