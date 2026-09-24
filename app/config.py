from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

# Named so app.main's startup check can compare against it without
# duplicating the literal string (and so it's easy to grep for everywhere
# it's referenced).
DEV_PHONE_ENCRYPTION_KEY = "dev-only-insecure-phone-key-do-not-use-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BEDBOARD_", extra="ignore")

    # Flips the whole app between the standalone in-memory mock FABT
    # (default) and a real FABT deployment. Read through Settings (not raw
    # os.getenv) specifically so it's sourced from .env like every other
    # setting -- a prior version read this via os.getenv() directly, which
    # silently ignored .env and left real deployments stuck in mock mode.
    use_mock_fabt: bool = True

    # --- FABT upstream API ---
    fabt_api_base_url: str = "http://localhost:8000"
    # Real FABT auth (reconciled against the actual API, see
    # app/fabt_client.py): a COC_ADMIN-scoped API key sent as the
    # `X-API-Key` header, created via `POST /api/v1/api-keys` with
    # `shelterId: null` by a human FABT admin -- not a Bearer JWT.
    fabt_api_key: str = "dev-api-key"
    # FABT's real API derives tenant scope entirely from the API key
    # server-side (no tenant_id in requests or responses) -- this is used
    # only to label ShelterSummary.tenant_id locally, never sent to FABT.
    fabt_tenant_id: str = "unknown-tenant"

    # --- Database (sidecar tables only) ---
    database_url: str = "sqlite+aiosqlite:///./bedboard.db"

    # Master secret coordinator phone numbers are hashed/encrypted with
    # (see app/crypto.py). One secret, not two: app/crypto.py derives
    # separate HMAC and AES-256-GCM subkeys from it via HKDF, so there's
    # only one value to generate and rotate. The default below is an
    # obviously-fake dev value (matching the pattern the real FABT project
    # uses for its own dev-only secrets) -- generate a real one with
    # `openssl rand -base64 32` and never use this default outside
    # BEDBOARD_USE_MOCK_FABT=true. app.main's startup check refuses to
    # boot with this default when USE_MOCK_FABT is false.
    phone_encryption_key: str = DEV_PHONE_ENCRYPTION_KEY

    # --- Twilio ---
    twilio_account_sid: str = "dev-account-sid"
    twilio_auth_token: str = "dev-auth-token"
    twilio_from_number: str = "+10000000000"
    # When true, inbound webhook signature validation is enforced. False
    # (the default) skips it -- local dev/tests only; production should set
    # this true (see .env.example).
    twilio_validate_signature: bool = False

    # --- Nudge scheduler ---
    nudge_timezone: str = "America/Los_Angeles"
    nudge_check_hours: tuple[int, ...] = (17, 21)  # 5pm and 9pm local
    nudge_escalation_minutes: int = 30
    stale_hours_threshold: float = 8.0
    aging_hours_threshold: float = 2.0

    # --- Wallboard ---
    wallboard_refresh_seconds: int = 30

    default_locale: str = "en"
    supported_locales: tuple[str, ...] = ("en", "es", "vi")

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.nudge_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
