"""BedBoard sidecar FastAPI app: wires the SMS webhook, wallboard, and
stale-count nudge scheduler together behind a single ASGI app.

Run locally, entirely standalone (no real FABT deployment, Postgres, or
Twilio account needed -- uses the in-memory mock FABT and a local SQLite
file by default):

    uvicorn app.main:app --reload

Point it at a real FABT deployment and real Twilio by setting
BEDBOARD_USE_MOCK_FABT=false plus BEDBOARD_FABT_API_BASE_URL /
BEDBOARD_FABT_SERVICE_ACCOUNT_TOKEN / BEDBOARD_TWILIO_* (see .env.example).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db.session import get_sessionmaker, init_db
from app.fabt_client import FabtClient, HttpFabtClient
from app.mock_fabt.client import InMemoryFabtClient
from app.mock_fabt.store import InMemoryFabtStore
from app.nudge.scheduler import start_scheduler
from app.sms.webhook import router as sms_router
from app.wallboard.router import router as wallboard_router

logger = logging.getLogger("bedboard")

# Read once at import time (not per-request): flips the whole app between
# "standalone demo against the in-memory mock FABT" (default) and "talking
# to a real FABT deployment" without touching any other module.
USE_MOCK_FABT = os.getenv("BEDBOARD_USE_MOCK_FABT", "true").lower() != "false"


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4:
        return "***"
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]


def _build_fabt_client() -> FabtClient:
    if USE_MOCK_FABT:
        logger.warning(
            "BedBoard is running against the in-memory mock FABT store, not a "
            "real FABT deployment. Set BEDBOARD_USE_MOCK_FABT=false and "
            "configure BEDBOARD_FABT_API_BASE_URL to point at a real one."
        )
        return InMemoryFabtClient(InMemoryFabtStore())
    settings = get_settings()
    return HttpFabtClient(settings.fabt_api_base_url, settings.fabt_api_key, settings.fabt_tenant_id)


def _build_send_sms():
    """Returns the (to, body) -> None coroutine app.nudge.scheduler sends
    nudge/escalation texts through. In mock mode this just logs (masked)
    instead of spending real Twilio credit; in real mode it calls Twilio.
    """
    if USE_MOCK_FABT:

        async def _send_sms(to: str, body: str) -> None:
            logger.info("dev SMS suppressed (mock mode): to=%s len=%d", _mask_phone(to), len(body))

        return _send_sms

    settings = get_settings()
    from twilio.rest import Client as TwilioClient  # imported lazily: only needed in real mode

    twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    async def _send_sms(to: str, body: str) -> None:
        twilio_client.messages.create(to=to, from_=settings.twilio_from_number, body=body)

    return _send_sms


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.fabt_client = _build_fabt_client()
    send_sms = _build_send_sms()
    app.state.scheduler = start_scheduler(app.state.fabt_client, get_sessionmaker(), send_sms)
    try:
        yield
    finally:
        app.state.scheduler.shutdown(wait=False)
        if isinstance(app.state.fabt_client, HttpFabtClient):
            await app.state.fabt_client.aclose()


app = FastAPI(title="BedBoard sidecar", lifespan=lifespan)
app.include_router(sms_router)
app.include_router(wallboard_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "mock_fabt": USE_MOCK_FABT}
