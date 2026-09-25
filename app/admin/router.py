"""Admin UI for BedBoard's own tables that have no other UI yet
(coordinator_phone, shelter_sms_population_map_override) -- the primary
way to manage them now, replacing scripts/seed_dev_coordinator.py and
scripts/set_shelter_sms_population_map.py for day-to-day use (both
scripts still work too, useful for scripting/CI).

No authentication. This router is exactly as unauthenticated as the rest
of this app right now (see README "Known gaps": no OIDC/county-SSO yet).
Unlike the read-only wallboard/webhook routes, this one can create,
deactivate, and delete data -- do not expose it on an untrusted network
without a reverse-proxy auth layer (or real county SSO, once that
exists) in front of it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_phone, encrypt_phone, hash_phone, mask_phone
from app.db.models import CoordinatorPhoneModel, ShelterSmsPopulationMapOverrideModel
from app.db.session import get_session
from app.fabt_client import DEFAULT_SMS_POPULATION_MAP, FabtClient
from app.sms_population_map import upsert_override
from app.wallboard.router import get_fabt_client

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect(path: str, **query: str) -> RedirectResponse:
    """Builds a redirect URL with properly encoded query params.

    Hand-building "path?flash=a+b+c" strings is a real footgun here: a
    literal "+" in a value (e.g. a masked phone number, which starts with
    one) is indistinguishable from the "+"-means-space convention used
    for the surrounding words, and gets silently eaten as a space when
    Starlette decodes it back out of the query string. urlencode()
    percent-encodes a literal "+" as %2B while still turning real spaces
    into "+", so it round-trips correctly either way.
    """
    return RedirectResponse(url=f"{path}?{urlencode(query)}" if query else path, status_code=303)


@router.get("/", include_in_schema=False)
async def admin_index() -> RedirectResponse:
    return RedirectResponse(url="/admin/coordinators", status_code=303)


async def _shelter_options(fabt: FabtClient) -> list[dict]:
    """(id, name) pairs for every shelter the current FabtClient can see
    (already excludes DV shelters -- every FabtClient.list_shelters()
    implementation does, see app/fabt_client.py). Used both for the
    dropdown and to resolve a shelter_id to a human name for display.
    """
    return [{"id": s.id, "name": s.name} for s in await fabt.list_shelters()]


# --- coordinators -------------------------------------------------------


@router.get("/coordinators", response_class=HTMLResponse)
async def list_coordinators(
    request: Request,
    session: AsyncSession = Depends(get_session),
    fabt: FabtClient = Depends(get_fabt_client),
) -> HTMLResponse:
    shelters = await _shelter_options(fabt)
    names = {s["id"]: s["name"] for s in shelters}

    rows = (await session.execute(select(CoordinatorPhoneModel))).scalars().all()
    coordinators = [
        {
            "id": c.id,
            "user_id": c.user_id,
            "shelter_name": names.get(c.shelter_id, c.shelter_id),
            # Decrypted only for this display; never stored/logged
            # unmasked -- see app.crypto.mask_phone.
            "masked_phone": mask_phone(decrypt_phone(c.phone_encrypted)),
            "shift": c.shift,
            "locale": c.locale,
            "active": c.active,
        }
        for c in rows
    ]

    return templates.TemplateResponse(
        request,
        "coordinators.html",
        {
            "coordinators": coordinators,
            "shelters": shelters,
            "flash": request.query_params.get("flash"),
            "flash_error": request.query_params.get("error") == "1",
        },
    )


@router.post("/coordinators")
async def create_coordinator(
    shelter_id: str = Form(...),
    user_id: str = Form(...),
    phone_e164: str = Form(...),
    shift: str = Form("day"),
    locale: str = Form("en"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    phone_e164 = phone_e164.strip()
    session.add(
        CoordinatorPhoneModel(
            user_id=user_id.strip(),
            shelter_id=shelter_id,
            phone_hash=hash_phone(phone_e164),
            phone_encrypted=encrypt_phone(phone_e164),
            shift=shift,
            active=True,
            locale=locale,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # phone_hash is UNIQUE -- this number is already registered.
        await session.rollback()
        return _redirect("/admin/coordinators", error="1", flash="That phone number is already registered.")
    return _redirect("/admin/coordinators", flash=f"Registered {mask_phone(phone_e164)} for {shelter_id}.")


@router.post("/coordinators/{coordinator_id}/toggle-active")
async def toggle_coordinator_active(
    coordinator_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    coordinator = await session.get(CoordinatorPhoneModel, coordinator_id)
    if coordinator is not None:
        coordinator.active = not coordinator.active
        await session.commit()
    return RedirectResponse(url="/admin/coordinators", status_code=303)


@router.post("/coordinators/{coordinator_id}/delete")
async def delete_coordinator(
    coordinator_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    coordinator = await session.get(CoordinatorPhoneModel, coordinator_id)
    if coordinator is not None:
        await session.delete(coordinator)
        await session.commit()
    return RedirectResponse(url="/admin/coordinators", status_code=303)


# --- SMS population-map overrides ---------------------------------------


@router.get("/population-map", response_class=HTMLResponse)
async def list_population_map(
    request: Request,
    session: AsyncSession = Depends(get_session),
    fabt: FabtClient = Depends(get_fabt_client),
) -> HTMLResponse:
    shelters = await _shelter_options(fabt)
    names = {s["id"]: s["name"] for s in shelters}

    rows = (
        await session.execute(select(ShelterSmsPopulationMapOverrideModel))
    ).scalars().all()
    overrides = [
        {
            "id": o.id,
            "shelter_name": names.get(o.shelter_id, o.shelter_id),
            "sms_population_type": o.sms_population_type,
            "fabt_population_type": o.fabt_population_type,
        }
        for o in rows
    ]

    return templates.TemplateResponse(
        request,
        "population_map.html",
        {
            "overrides": overrides,
            "shelters": shelters,
            "default_map": {bucket.value: pt for bucket, pt in DEFAULT_SMS_POPULATION_MAP.items()},
            "flash": request.query_params.get("flash"),
            "flash_error": request.query_params.get("error") == "1",
        },
    )


@router.post("/population-map")
async def create_population_map_override(
    shelter_id: str = Form(...),
    sms_population_type: str = Form(...),
    fabt_population_type: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    value = fabt_population_type.strip() or None
    await upsert_override(session, shelter_id, sms_population_type, value)
    verb = "disabled" if value is None else f"now -> {value}"
    return _redirect(
        "/admin/population-map", flash=f"Saved: {shelter_id} {sms_population_type} {verb}."
    )


@router.post("/population-map/{override_id}/delete")
async def delete_population_map_override(
    override_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    override = await session.get(ShelterSmsPopulationMapOverrideModel, override_id)
    if override is not None:
        await session.delete(override)
        await session.commit()
    return RedirectResponse(url="/admin/population-map", status_code=303)
