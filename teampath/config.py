"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    app_name: str = "TeamPath"
    environment: str = "development"  # development | production
    base_url: str = "http://localhost:8000"

    # Two database roles: the app role is NOT the table owner, so row-level
    # security applies to it. The owner role is used only by migrations.
    database_url: str = "postgresql+psycopg://teampath_app:teampath_app@localhost:5432/teampath"
    migration_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/teampath"
    app_db_password: str = "teampath_app"

    secret_key: str = Field(default="dev-only-change-me-dev-only-change-me")
    flet_secret_key: str = Field(default="dev-only-flet-secret-change-me")
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt MFA secrets at rest.
    encryption_key: str = Field(default="")

    data_dir: Path = Path("./data")
    max_upload_mb: int = 200

    # Sessions
    session_idle_minutes: int = 30
    session_max_hours: int = 12
    remember_days: int = 14

    # Login protection
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    password_min_length: int = 12
    hibp_check: bool = False  # breached-password check via k-anonymity API

    # New organizations wait for platform-admin approval
    require_org_approval: bool = True

    # Email. If smtp_host is empty, emails are written to the log (dev only).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@example.com"
    smtp_starttls: bool = True

    # Optional antivirus scanning of uploads through clamd
    clamd_host: str = ""
    clamd_port: int = 3310

    # Apply pending database migrations when the web app starts. Defaults to on
    # outside production; in Docker the entrypoint migrates before starting.
    auto_migrate: bool | None = None

    # Content-Security-Policy: enforce | report | off
    csp_mode: str = "enforce"

    # Q&A escalation
    question_escalate_hours: int = 24

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def upload_tmp_dir(self) -> Path:
        return self.data_dir / "incoming"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.auto_migrate is None:
        s.auto_migrate = not s.is_production
    if s.is_production:
        weak = [k for k in ("secret_key", "flet_secret_key") if "dev-only" in getattr(s, k)]
        if weak or not s.encryption_key:
            raise RuntimeError(
                f"Refusing to start in production with default secrets: {weak or ['encryption_key']}"
            )
    return s
