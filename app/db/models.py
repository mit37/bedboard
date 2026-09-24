"""BedBoard sidecar's own tables (PRD page 9), as SQLAlchemy 2.0 models.

These are the only tables the sidecar owns and writes to directly; shelter,
bed_capacity, availability_snapshot, reservation, etc. all live in FABT's
Postgres and are reached only via app.fabt_client (see that module's
docstring for why).

Deviations from the PRD's literal SQL, called out explicitly:
- `locale` added to coordinator_phone (needed for BB-10 SMS replies in
  Spanish/Vietnamese; the PRD's SQL sketch predates that requirement).
- `phone_hash`/`phone_encrypted` are as the PRD specifies (an HMAC lookup
  hash paired with a separately encrypted column, see app/crypto.py for
  both), but the encryption key is one operator-supplied application
  secret (Settings.phone_encryption_key) rather than a real KMS/HSM --
  swap in real KMS-backed key management before a production pilot
  handles real coordinator phone numbers at real scale; see README
  "Known gaps".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
)
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
    # HMAC-SHA256 of the E.164 number -- the lookup key (app.crypto.hash_phone).
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False, index=True)
    # AES-256-GCM ciphertext of the E.164 number (app.crypto.encrypt_phone)
    # -- decrypted only when the real number is actually needed (sending a
    # nudge SMS via Twilio); never used for lookup, since it's non-deterministic.
    phone_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    shift: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    locale: Mapped[str] = mapped_column(String, default="en", nullable=False)


class SmsUpdateLogModel(Base):
    __tablename__ = "sms_update_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Same HMAC-SHA256 lookup hash as coordinator_phone.phone_hash -- this
    # log never needs to recover the real number, only to correlate
    # entries by sender, so it stores no encrypted/plaintext number at all.
    phone_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, index=True)
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


class ShelterSmsPopulationMapOverrideModel(Base):
    """BedBoard's own per-shelter override of which FABT population_type
    an SMS bucket (women/men/family) maps onto for that shelter. FABT has
    no concept of this mapping -- see app/sms_population_map.py for why
    it exists and how it's resolved (only app/sms/webhook.py reads it;
    every FabtClient implementation still supplies its own default map
    via ShelterSummary.sms_population_map, which this overrides).
    """

    __tablename__ = "shelter_sms_population_map_override"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    shelter_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sms_population_type: Mapped[str] = mapped_column(String, nullable=False)  # women|men|family
    # NULL = explicitly disable this bucket for this shelter (it will no
    # longer accept SMS updates for it at all, even if the FabtClient's
    # own default map would otherwise include it). A row's ABSENCE
    # (no override at all for that bucket) instead means "fall back to
    # whatever the shelter's own default map says" -- these are two
    # different, both-legitimate outcomes, not interchangeable.
    fabt_population_type: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("shelter_id", "sms_population_type", name="uq_shelter_sms_pop_override"),
    )
