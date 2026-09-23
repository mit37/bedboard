# BedBoard sidecar

BedBoard's own original-code layer, per the [PRD & Tech Spec](docs/BedBoard%20%E2%80%94%20PRD%20%26%20Tech%20Spec.pdf): a Python 3.12 / FastAPI
service that sits beside the open-source **Finding A Bed Tonight** (FABT)
platform and adds the county-pilot pieces FABT doesn't already have —
SMS bed updates, stale-count nudges, the Here4You wallboard, and
Spanish/Vietnamese translations.

**This build is the sidecar only**, stubbed against an in-memory mock of
the FABT API so the whole thing runs standalone without the real
Java/Spring FABT deployment. See "Known gaps" below for exactly what's
mocked and what a real pilot still needs.

## What's implemented (PRD requirement IDs)

| ID | Requirement | Where |
|----|---|---|
| BB-2 | Freshness (FRESH/AGING/STALE) | [`app/freshness.py`](app/freshness.py) |
| BB-5 | SMS bed update | [`app/sms/parser.py`](app/sms/parser.py), [`app/sms/webhook.py`](app/sms/webhook.py) |
| BB-6 | Stale-count nudge + escalation | [`app/nudge/scheduler.py`](app/nudge/scheduler.py) |
| BB-7 | Here4You wallboard | [`app/wallboard/router.py`](app/wallboard/router.py) |
| BB-8 | DV opaque referral (partial) | `is_dv` filtering in [`app/mock_fabt/store.py`](app/mock_fabt/store.py) — see gaps |
| BB-10 | Spanish + Vietnamese | [`app/sms/messages.py`](app/sms/messages.py), [`app/nudge/messages.py`](app/nudge/messages.py) |

Not built in this slice: BB-1/3/4/9/11/12/13 and the FABT PWA itself all
live in FABT (Java/Spring), which this build doesn't fork — see the PRD's
"build vs. adopt" call.

## Architecture

```
Twilio SMS <--> app/sms/webhook.py --+
                                      |--> app.fabt_client.FabtClient (ABC)
app/wallboard/router.py (SSE) -------+          |
                                                 +-- HttpFabtClient  -> real FABT API (not built here)
app/nudge/scheduler.py (APScheduler) --+        +-- InMemoryFabtClient -> app/mock_fabt (default, this repo)

BedBoard's own tables (app/db/models.py, via SQLAlchemy async):
  coordinator_phone, sms_update_log, nudge
```

The sidecar never touches FABT's Postgres directly (per the PRD's
architecture diagram) — everything upstream goes through `FabtClient`.
Swapping `InMemoryFabtClient` for `HttpFabtClient` (already written, in
[`app/fabt_client.py`](app/fabt_client.py)) is the entire integration
surface for pointing this at a real FABT deployment.

## Run it (standalone, no setup beyond Python)

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then, since `coordinator_phone` is BedBoard's own table and there's no
admin UI yet (out of scope for this slice), register a demo coordinator:

```bash
python scripts/seed_dev_coordinator.py "+15551234567" shelter-first-street --shift day --locale en
```

Try it:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/wallboard/santa-clara-county
curl -X POST http://localhost:8000/sms/inbound \
  --data-urlencode "From=+15551234567" --data-urlencode "Body=W 3 M 1 F 0"
```

(Seeded demo shelters: `shelter-first-street`, `shelter-gateway`,
`shelter-willow-glen`, plus `shelter-safe-haven` — a DV shelter that
`list_shelters`/the wallboard always exclude, and that only shows up via a
direct `get_shelter` lookup.)

## Run it with Docker (Postgres instead of SQLite)

```bash
docker compose up --build
```

## Tests

```bash
pytest        # 62 tests: parser, mock FABT, nudge scheduler, wallboard, full SMS-to-wallboard integration
```

## Known gaps vs. the full PRD (read before a real pilot)

These are the real open items this slice deliberately left for the next
phase, not bugs:

- **FABT itself isn't forked yet.** `app/mock_fabt` is a hand-built stand-in
  for FABT's actual REST API; `HttpFabtClient`'s endpoint paths are a
  best guess at FABT's real OpenAPI contract and need reconciling once
  FABT is actually deployed (per the PRD's discovery/build-vs-adopt plan).
- **Coordinator phone numbers are stored in plaintext** (`phone_e164`),
  not the PRD's `phone_hash` (HMAC lookup) + `phone_encrypted` pair. That
  needs a real KMS/HSM key and is deferred — see the comment in
  `app/db/models.py`.
- **DV opacity is only "excluded from listings."** The PRD's full design
  (time-limited referral token + warm handoff call) isn't built — this
  slice only proves the *invariant* (a DV shelter never appears in
  enumeration/wallboard output) holds at the mock-FABT layer.
- **"On-shift coordinator" nudging isn't shift-aware.** There's no real
  shift-schedule data yet, so BB-6 nudges *all* active coordinators for a
  shelter, not just the one on shift. Level-2 escalation does target
  `shift == "lead"` coordinators specifically.
- **Wallboard SSE is polling, not push.** It re-fetches every 30s
  (`BEDBOARD_WALLBOARD_REFRESH_SECONDS`) rather than pushing within 5s of
  a real change, which needs a pub/sub layer FABT doesn't have yet.
- **No Alembic migrations** — `init_db()` just calls
  `Base.metadata.create_all`. Fine for a pilot's 3 tables; add Alembic
  before schema changes get risky.
- **No auth on the API itself** beyond the FABT service-account bearer
  token pattern and Twilio signature validation (off by default locally,
  on via `BEDBOARD_TWILIO_VALIDATE_SIGNATURE`). OIDC/county-SSO for admin
  routes isn't built (there are no admin routes yet either).
- **SMS locale token choices are this build's own design**, not something
  the PRD specifies (Spanish M/H/F, Vietnamese N/D/G, both ASCII-only) —
  see the doc comment at the top of `app/sms/parser.py` before this goes
  in front of real Spanish/Vietnamese-speaking coordinators for review.

## Next steps toward the PRD's 6-weekend MVP

1. Fork/deploy FABT's Lite tier (Spring Boot + React PWA + Postgres) per
   the PRD, then reconcile `HttpFabtClient`'s assumed endpoints against
   its real API.
2. Swap `BEDBOARD_USE_MOCK_FABT=false` and point at it.
3. Replace plaintext phone storage with hash+encrypt.
4. Add Alembic, an admin UI for `coordinator_phone` (replacing
   `scripts/seed_dev_coordinator.py`), and the HMIS nightly export bridge.
5. Tabletop exercise with Here4You + 2 pilot shelters (PRD's pilot gate).
