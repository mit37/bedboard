"""BB-7: read-only Here4You call-center wallboard.

"Large-screen view for the call center: all pilot sites, counts, freshness,
active holds", auto-refreshing with stale sites flagged. This module is a
thin transport layer only -- it never computes freshness or filters
shelters itself; that is entirely the job of the FabtClient implementation
behind it (per app/schemas.py's WallboardSite.is_stale and
app/freshness.py's compute_freshness()).

Assumption: the PRD's architecture diagram shows the wallboard reaching the
FABT API over SSE with updates "within 5s of a change" (its ideal/real
architecture). Building true push-on-change would need a pub/sub layer
(FABT publishing change events, or this sidecar diffing/watching FABT
state) that does not exist yet anywhere in this build. For this
sidecar-only slice we instead implement the SSE stream as a periodic tick:
every `get_settings().wallboard_refresh_seconds` (default 30s, matching the
PRD's plain "auto-refresh every 30s" requirement for the degraded/fallback
case) we simply re-fetch `fabt.get_wallboard(tenant_id)` and push whatever
comes back, whether or not anything changed. This is a real gap versus the
"within 5s" ideal-architecture goal -- flagged here rather than building a
full pub/sub system for a single endpoint.

Assumption: dependency injection pattern for the FabtClient. This module
must not import app.mock_fabt (or any concrete client) -- other agents are
building those concurrently. Instead it reads the client off
`request.app.state.fabt_client` through the `get_fabt_client` dependency
function below. A later integration step wires this up simply by setting,
before the app starts serving:

    app.state.fabt_client = <some FabtClient instance>  # e.g. HttpFabtClient(...)
                                                          # or the mock_fabt client

and including this router:

    from app.wallboard.router import router as wallboard_router
    app.include_router(wallboard_router)

If `app.state.fabt_client` is never set, `get_fabt_client` raises a clear
RuntimeError rather than an opaque AttributeError.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.fabt_client import FabtClient
from app.schemas import WallboardSnapshot

router = APIRouter(prefix="/wallboard", tags=["wallboard"])


def get_fabt_client(request: Request) -> FabtClient:
    """FastAPI dependency: resolves the FabtClient set on app state.

    Kept as a free function (rather than inlined in each endpoint) so an
    integration test, or another router, can override it via FastAPI's
    `app.dependency_overrides[get_fabt_client] = ...` without touching
    `app.state` at all.
    """
    client = getattr(request.app.state, "fabt_client", None)
    if client is None:
        raise RuntimeError(
            "app.state.fabt_client is not set. Wire up a FabtClient "
            "implementation (e.g. `app.state.fabt_client = HttpFabtClient(...)`) "
            "before serving requests through the wallboard router."
        )
    return client


@router.get("/{tenant_id}", response_model=WallboardSnapshot)
async def get_wallboard(
    tenant_id: str,
    fabt: FabtClient = Depends(get_fabt_client),
) -> WallboardSnapshot:
    """Plain snapshot for an initial page load."""
    return await fabt.get_wallboard(tenant_id)


async def _wallboard_event_stream(
    request: Request,
    fabt: FabtClient,
    tenant_id: str,
) -> AsyncIterator[str]:
    interval = get_settings().wallboard_refresh_seconds
    try:
        while True:
            if await request.is_disconnected():
                break
            snapshot = await fabt.get_wallboard(tenant_id)
            yield f"data: {snapshot.model_dump_json()}\n\n"
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # Client went away mid-sleep (or mid-fetch); let the generator end
        # quietly instead of leaking the loop or the sleeping task.
        raise


@router.get("/{tenant_id}/stream")
async def stream_wallboard(
    tenant_id: str,
    request: Request,
    fabt: FabtClient = Depends(get_fabt_client),
) -> StreamingResponse:
    """SSE stream: re-fetches and pushes the wallboard snapshot on every tick.

    See module docstring's Assumption note -- this is a periodic re-fetch,
    not true push-on-change.
    """
    return StreamingResponse(
        _wallboard_event_stream(request, fabt, tenant_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Prevent an intermediary reverse proxy (e.g. nginx) from
            # buffering the stream and defeating the point of SSE.
            "X-Accel-Buffering": "no",
        },
    )
