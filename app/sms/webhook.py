"""BB-5 inbound SMS webhook: Twilio -> parser -> FABT -> reply.

Wires together app.sms.parser/messages, app.fabt_client.FabtClient (via the
same app.state.fabt_client dependency the wallboard uses), app.db.models
(CoordinatorPhoneModel, SmsUpdateLogModel), app.sms_population_map (any
per-shelter override of which FABT population_type an SMS bucket maps
onto), and app.nudge.scheduler.resolve_nudges_for_shelter so a fresh count
clears any pending stale-count nudge for that shelter.

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
from app.fabt_client import FabtApiError, FabtClient
from app.nudge.scheduler import resolve_nudges_for_shelter
from app.schemas import Locale, SmsParseError
from app.sms_population_map import resolve_sms_population_map
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

    population_map = await resolve_sms_population_map(session, shelter)

    actual_counts: dict = {}
    for bucket, count in parsed.counts.items():
        population_type = population_map.get(bucket)
        if population_type is None:
            # Either this shelter's default map doesn't serve that bucket
            # (e.g. Gateway never reports "family"), or a BedBoard admin
            # explicitly disabled it via a
            # ShelterSmsPopulationMapOverrideModel row -- either way, skip
            # rather than invent a row (see app.sms_population_map).
            continue
        try:
            saved = await fabt.post_snapshot(
                shelter_id=shelter.id,
                population_type=population_type,
                beds_available=count,
                recorded_by=coordinator.user_id,
                recorded_at=now,
            )
        except FabtApiError:
            # FABT rejected this one bucket (e.g. no configured capacity
            # for it). Don't let one bad bucket lose buckets that already
            # succeeded earlier in this same loop, or crash the whole
            # webhook to a bare 500 with no TwiML reply -- skip it and
            # keep going; whatever's in actual_counts once the loop ends
            # reflects exactly what was really saved.
            continue
        # Always confirm with what FABT actually saved, not what the
        # coordinator typed -- discovered live against the real FABT API
        # (see HttpFabtClient.post_snapshot's docstring): if a population
        # type has an active hold, FABT's returned beds_available is
        # lower than the requested value (holds are subtracted
        # server-side on top of occupancy). Echoing the raw input back
        # would tell a coordinator a number that doesn't match what the
        # wallboard/next search will actually show.
        actual_counts[bucket] = saved.beds_available

    if not actual_counts:
        # Nothing was actually saved to FABT -- every bucket was either
        # unsupported/disabled for this shelter, or every attempted post
        # failed. Don't claim "saved" or clear a pending stale-count
        # nudge for data that never reached FABT.
        session.add(
            SmsUpdateLogModel(
                phone_e164=phone,
                shelter_id=shelter.id,
                raw_text=raw_text,
                parsed={bucket.value: count for bucket, count in parsed.counts.items()},
                result="rejected",
                ts=now,
            )
        )
        await session.commit()
        return _twiml_response(render_help(locale))

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

    return _twiml_response(render_confirmation(actual_counts, locale))
