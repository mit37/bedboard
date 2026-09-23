"""BB-5 inbound SMS webhook: Twilio -> parser -> FABT -> reply.

Wires together app.sms.parser/messages, app.fabt_client.FabtClient (via the
same app.state.fabt_client dependency the wallboard uses), app.db.models
(CoordinatorPhoneModel, SmsUpdateLogModel), and
app.nudge.scheduler.resolve_nudges_for_shelter so a fresh count clears any
pending stale-count nudge for that shelter.

Integration decisions made here (not owned by any single module):
- An inbound message from an unregistered phone number is rejected and
  logged, but still gets a TwiML reply (courteous UX beats silence).
- A message that fails to parse gets the localized help text as its reply.
- A registered coordinator whose shelter_id FABT doesn't recognize (a
  data-integrity problem, not a parsing one) is treated the same as a
  parse failure: nothing is saved, help text is returned, and the log
  entry is marked "rejected" rather than "saved".
- Reply is sent synchronously via TwiML in the webhook response (no
  outbound Twilio API call needed for confirmations/help/rejections --
  only nudges, which aren't replies to an inbound message, use the
  injectable send_sms path in app.nudge.scheduler).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import get_settings
from app.db.models import CoordinatorPhoneModel, SmsUpdateLogModel
from app.db.session import get_session
from app.fabt_client import FabtClient
from app.nudge.scheduler import resolve_nudges_for_shelter
from app.schemas import Locale, SmsParseError
from app.sms.messages import render_confirmation, render_help, render_rejected_unknown_number
from app.sms.parser import parse_sms
from app.wallboard.router import get_fabt_client

router = APIRouter(prefix="/sms", tags=["sms"])


def _twiml_response(body: str) -> Response:
    twiml = MessagingResponse()
    twiml.message(body)
    return Response(content=str(twiml), media_type="application/xml")


async def _validate_twilio_signature(request: Request) -> None:
    """Reject inbound requests that don't carry a valid Twilio signature.

    Skipped entirely when settings.twilio_validate_signature is False
    (the default -- local dev/tests don't have real Twilio credentials).
    Uses the request URL as seen by this process; behind a reverse proxy
    that rewrites scheme/host, TWILIO_VALIDATE_SIGNATURE deployments need
    that proxy configured to forward the original URL, or this check will
    false-reject valid Twilio requests -- a known deployment detail, not a
    bug in this handler.
    """
    settings = get_settings()
    if not settings.twilio_validate_signature:
        return
    validator = RequestValidator(settings.twilio_auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    if not validator.validate(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


@router.post("/inbound")
async def inbound_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    session: AsyncSession = Depends(get_session),
    fabt: FabtClient = Depends(get_fabt_client),
) -> Response:
    await _validate_twilio_signature(request)

    now = datetime.now(timezone.utc)
    phone = From.strip()
    raw_text = Body or ""

    coordinator = (
        await session.execute(
            select(CoordinatorPhoneModel).where(
                CoordinatorPhoneModel.phone_e164 == phone,
                CoordinatorPhoneModel.active.is_(True),
            )
        )
    ).scalar_one_or_none()

    if coordinator is None:
        session.add(
            SmsUpdateLogModel(
                phone_e164=phone,
                shelter_id=None,
                raw_text=raw_text,
                parsed=None,
                result="rejected",
                ts=now,
            )
        )
        await session.commit()
        return _twiml_response(render_rejected_unknown_number(Locale.EN))

    try:
        locale = Locale(coordinator.locale)
    except ValueError:
        locale = Locale.EN

    parsed = parse_sms(raw_text, locale)

    if isinstance(parsed, SmsParseError):
        session.add(
            SmsUpdateLogModel(
                phone_e164=phone,
                shelter_id=coordinator.shelter_id,
                raw_text=raw_text,
                parsed=None,
                result="parse_error",
                ts=now,
            )
        )
        await session.commit()
        return _twiml_response(render_help(locale))

    shelter = await fabt.get_shelter(coordinator.shelter_id)
    if shelter is None:
        session.add(
            SmsUpdateLogModel(
                phone_e164=phone,
                shelter_id=coordinator.shelter_id,
                raw_text=raw_text,
                parsed={bucket.value: count for bucket, count in parsed.counts.items()},
                result="rejected",
                ts=now,
            )
        )
        await session.commit()
        return _twiml_response(render_help(locale))

    for bucket, count in parsed.counts.items():
        population_type = shelter.sms_population_map.get(bucket)
        if population_type is None:
            # This shelter doesn't track that bucket at all (e.g. Gateway
            # never reports "family") -- skip rather than invent a row.
            continue
        await fabt.post_snapshot(
            shelter_id=shelter.id,
            population_type=population_type,
            beds_available=count,
            recorded_by=coordinator.user_id,
            recorded_at=now,
        )

    await resolve_nudges_for_shelter(session, shelter.id, now)

    session.add(
        SmsUpdateLogModel(
            phone_e164=phone,
            shelter_id=shelter.id,
            raw_text=raw_text,
            parsed={bucket.value: count for bucket, count in parsed.counts.items()},
            result="saved",
            ts=now,
        )
    )
    await session.commit()

    return _twiml_response(render_confirmation(parsed.counts, locale))
