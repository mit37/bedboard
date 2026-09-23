"""BedBoard sidecar's own tables (PRD page 9), as SQLAlchemy 2.0 models.

These are the only tables the sidecar owns and writes to directly; shelter,
bed_capacity, availability_snapshot, reservation, etc. all live in FABT's
Postgres and are reached only via app.fabt_client (see that module's
docstring for why).

Deviations from the PRD's literal SQL, called out explicitly:
- `locale` added to coordinator_phone (needed for BB-10 SMS replies in
  Spanish/Vietnamese; the PRD's SQL sketch predates that requirement).
- `phone_e164` stored directly instead of `phone_hash` + `phone_encrypted`.
  The PRD pairs an HMAC lookup hash with an encrypted-at-rest column so a
  DB dump alone doesn't reveal phone numbers; that needs a real KMS/HSM
  key, which is out of scope for this first slice running on SQLite in
  dev. Swap in a proper hash+encrypt column pair before handling real
  coordinator phone numbers -- see README "Known gaps".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class CoordinatorPhoneModel(Base):
    __tablename__ = "coordinator_phone"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    shelter_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    phone_e164: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    shift: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    locale: Mapped[str] = mapped_column(String, default="en", nullable=False)


class SmsUpdateLogModel(Base):
    __tablename__ = "sms_update_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_e164: Mapped[str] = mapped_column(String, nullable=False, index=True)
    shelter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_text: Mapped[str] = mapped_column(String, nullable=False)
    parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String, nullable=False)  # saved|rejected|parse_error
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NudgeModel(Base):
    __tablename__ = "nudge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shelter_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1=coordinator, 2=lead
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
