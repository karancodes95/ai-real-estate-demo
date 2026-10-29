"""Runtime configuration, validated via pydantic-settings (loads from .env)."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Async SQLAlchemy URL for the local Postgres leads DB.
    database_url: str = "postgresql+asyncpg://karan@localhost:5432/lead_to_lease"

    # Static demo API key gating the JSON API (chat + CRM data endpoints). The
    # static Pages frontends send it in X-API-Key. This is a low-value bot-gate,
    # not a real secret (it ships in the client bundle), but it keeps casual
    # cross-origin traffic off the endpoints. Must be non-empty (see validator).
    api_key: str = ""

    # Comma-separated CORS origins for production (e.g. the GitHub Pages URL).
    # Empty default keeps the current "*" behavior for local dev; set to the
    # deployed frontend origin(s) before launch.
    allowed_origins: str = ""

    # Demo agency branding (fictional).
    agency_name: str = "Amberstone Realty"
    agency_email: str = "hello@amberstone.example"

    # Day-0 confirmation email via Zoho SMTP (warmup_send.py pattern).
    # Leave zoho_email empty to skip sending (dev without creds).
    zoho_email: str = ""
    zoho_app_password: str = ""
    smtp_host: str = "smtp.zoho.in"
    smtp_port: int = 465

    # Abuse guard: max chat messages per client IP per day (0 = unlimited).
    chat_daily_ip_cap: int = 10

    # Trust the proxy's X-Real-IP (and CF-IPCountry) only when this is on AND
    # the socket peer is loopback (nginx on the same host); X-Forwarded-For is
    # never read. Otherwise a client could spoof the header to dodge the cap.
    # Set true in prod (VPS/proxy).
    trust_proxy: bool = False

    # Admin API login (POST /admin/login, see app/admin_auth.py), the same
    # scheme as the clinic and winery demos. Separate from api_key, which ships
    # in the public client bundle and must never unlock /admin.
    # admin_password_hash: argon2id hash of the admin password, from
    #   .venv/bin/python tool/hash_admin_password.py. Empty = login is off and
    #   every /admin call answers 401.
    # admin_jwt_secret: HMAC key signing the session JWT (HS256), 32+ random
    #   bytes, e.g. python -c "import secrets; print(secrets.token_hex(32))".
    #   Rotating it signs every admin out.
    # admin_session_ttl_hours: how long a freshly issued token is valid.
    admin_password_hash: str = ""
    admin_jwt_secret: str = ""
    admin_session_ttl_hours: int = 168  # 7 days

    @field_validator("api_key")
    @classmethod
    def _api_key_must_be_set(cls, v: str) -> str:
        # An empty api_key turns `x_api_key == api_key` into an empty-header
        # bypass, so fail closed at load rather than silently disable the gate.
        if not v or not v.strip():
            raise ValueError("API_KEY must be a non-empty value")
        return v


settings = Settings()
