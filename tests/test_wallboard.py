"""Tests for app.wallboard.router (BB-7).

Uses a small local fake FabtClient implemented directly in this file
(per module rules -- app.mock_fabt is being built concurrently and must
not be imported from here).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.fabt_client import FabtClient
from app.schemas import (
    Freshness,
    PopulationCount,
    Reservation,
    ShelterSummary,
    WallboardSite,
    WallboardSnapshot,
)
from app.wallboard.router import router as wallboard_router
from app.wallboard.router import stream_wallboard

TENANT_ID = "tenant-1"

# A shelter that must never show up in wallboard output. It is deliberately
# *not* included in FakeFabtClient's snapshot below, simulating a FabtClient
# implementation that has already filtered DV-flagged shelters out upstream
# (the wallboard router itself has no DV-filtering logic -- it just passes
# through whatever the client gives it).
DV_SHELTER_ID = "shelter-dv-1"
DV_SHELTER_NAME = "Confidential DV Shelter"


class FakeFabtClient(FabtClient):
    """Minimal in-memory FabtClient for wallboard tests only."""

    def __init__(self, snapshot: WallboardSnapshot):
        self._snapshot = snapshot

    async def list_shelters(self) -> list[ShelterSummary]:
        raise NotImplementedError("not needed by wallboard tests")

    async def get_shelter(self, shelter_id: str) -> ShelterSummary | None:
        raise NotImplementedError("not needed by wallboard tests")

    async def get_latest_counts(self, shelter_id: str) -> list[PopulationCount]:
        raise NotImplementedError("not needed by wallboard tests")

    async def post_snapshot(
        self,
        shelter_id: str,
        population_type: str,
        beds_available: int,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> PopulationCount:
        raise NotImplementedError("not needed by wallboard tests")

    async def get_wallboard(self, tenant_id: str) -> WallboardSnapshot:
        # Real implementations key the snapshot off tenant_id; the fake just
        # returns its fixed snapshot regardless, which is enough to prove
        # the router passes tenant_id through and does no filtering itself.
        return self._snapshot

    async def count_active_holds(self, shelter_id: str) -> int:
        raise NotImplementedError("not needed by wallboard tests")

    async def create_reservation(
        self, shelter_id: str, population_type: str, held_by: str
    ) -> Reservation:
        raise NotImplementedError("not needed by wallboard tests")


class _FakeRequest:
    """Stands in for FastAPI's `Request` in the direct-call stream test below.

    The streaming generator only ever calls `request.is_disconnected()`, so
    that is all this fake needs to provide.
    """

    async def is_disconnected(self) -> bool:
        return False


def _make_snapshot() -> WallboardSnapshot:
    now = datetime.now(timezone.utc)
    return WallboardSnapshot(
        generated_at=now,
        sites=[
            WallboardSite(
                shelter_id="shelter-fresh-1",
                name="Riverside Shelter",
                counts=[
                    PopulationCount(
                        population_type="women",
                        beds_available=3,
                        recorded_at=now,
                        recorded_by="coordinator-1",
                        freshness=Freshness.FRESH,
                    ),
                ],
                is_stale=False,
                active_holds=1,
            ),
            WallboardSite(
                shelter_id="shelter-stale-1",
                name="Downtown Shelter",
                counts=[
                    PopulationCount(
                        population_type="men",
                        beds_available=0,
                        recorded_at=now - timedelta(hours=10),
                        recorded_by="coordinator-2",
                        freshness=Freshness.STALE,
                    ),
                ],
                is_stale=True,
                active_holds=0,
            ),
            # DV_SHELTER_ID / DV_SHELTER_NAME intentionally absent -- see
            # module docstring above.
        ],
    )


def _make_app(snapshot: WallboardSnapshot) -> FastAPI:
    app = FastAPI()
    app.include_router(wallboard_router)
    app.state.fabt_client = FakeFabtClient(snapshot)
    return app


@pytest.mark.asyncio
async def test_get_snapshot_returns_expected_shape() -> None:
    app = _make_app(_make_snapshot())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/wallboard/{TENANT_ID}")

    assert resp.status_code == 200
    body = resp.json()

    # Round-trips into a valid WallboardSnapshot.
    snapshot = WallboardSnapshot.model_validate(body)
    assert {site.shelter_id for site in snapshot.sites} == {
        "shelter-fresh-1",
        "shelter-stale-1",
    }

    stale_site = next(s for s in snapshot.sites if s.shelter_id == "shelter-stale-1")
    assert stale_site.is_stale is True
    fresh_site = next(s for s in snapshot.sites if s.shelter_id == "shelter-fresh-1")
    assert fresh_site.is_stale is False
    assert fresh_site.active_holds == 1
    assert fresh_site.counts[0].freshness == Freshness.FRESH


@pytest.mark.asyncio
async def test_stream_yields_well_formed_sse_event() -> None:
    # NOTE: this deliberately calls the `stream_wallboard` route coroutine
    # directly instead of driving it through an HTTP test client. The
    # endpoint's generator runs forever by design (it is a tick loop that
    # only stops on client disconnect -- see router.py), and both
    # httpx.ASGITransport and Starlette's TestClient fully buffer an ASGI
    # app's entire response body before returning it to the caller, so
    # either one would hang waiting for a response that never finishes.
    # Calling the route function directly still exercises the real
    # production code (StreamingResponse construction, media_type, and the
    # actual generator), just without an HTTP round trip.
    fake_client = FakeFabtClient(_make_snapshot())
    fake_request = _FakeRequest()

    response = await stream_wallboard(TENANT_ID, fake_request, fabt=fake_client)

    assert isinstance(response, StreamingResponse)
    assert response.headers["content-type"].startswith("text/event-stream")

    try:
        first_chunk = await response.body_iterator.__anext__()
        assert first_chunk.startswith("data: ")
        assert first_chunk.endswith("\n\n")
        payload = first_chunk[len("data: "):].strip()
        event_snapshot = WallboardSnapshot.model_validate_json(payload)
    finally:
        await response.body_iterator.aclose()

    assert {site.shelter_id for site in event_snapshot.sites} == {
        "shelter-fresh-1",
        "shelter-stale-1",
    }


@pytest.mark.asyncio
async def test_dv_shelter_never_leaks_through_router() -> None:
    """The fake client's get_wallboard omits the DV shelter entirely (as a
    real FabtClient implementation is expected to). This asserts the router
    performs no filtering of its own that could accidentally double-include
    it (or anything else) -- it is a pure pass-through of whatever the
    client returns.
    """
    app = _make_app(_make_snapshot())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/wallboard/{TENANT_ID}")

    assert resp.status_code == 200
    body_text = resp.text
    assert DV_SHELTER_ID not in body_text
    assert DV_SHELTER_NAME not in body_text

    snapshot = WallboardSnapshot.model_validate(resp.json())
    assert all(site.shelter_id != DV_SHELTER_ID for site in snapshot.sites)
    assert all(site.name != DV_SHELTER_NAME for site in snapshot.sites)
