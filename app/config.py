"""Application configuration.

All external integrations are gated by explicit `require_*()` checks that
raise `ConfigurationError` with the exact missing environment variable(s).
Settings are otherwise loadable with no credentials at all, so DRY_RUN /
TEST_MODE workflows, the dashboard, and the test suite can run without any
external account.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when a subsystem is invoked without its required credentials."""


def _parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(hour=int(hour), minute=int(minute))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime mode ---
    test_mode: bool = True
    dry_run: bool = True
    timezone: str = "Asia/Karachi"

    # --- Google Sheets ---
    google_service_account_json: str | None = None
    google_service_account_file: str | None = None
    google_spreadsheet_id: str | None = None
    google_worksheet_name: str = "Consumers"

    # --- Telephony ---
    telephony_provider: str = "twilio"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_phone_number: str | None = None
    public_base_url: str | None = None

    # --- AI / LLM ---
    ai_provider: str = "anthropic"
    ai_api_key: str | None = None
    ai_model: str = "claude-sonnet-5"

    # --- Speech ---
    google_speech_credentials_json: str | None = None
    google_speech_credentials_file: str | None = None

    # --- Call recording ---
    call_recording_enabled: bool = False

    # --- Retry / concurrency ---
    max_call_retries: int = 3
    max_concurrent_calls: int = 1

    # --- Schedule (HH:MM, in `timezone`) ---
    call_start_1: str = "09:20"
    call_end_1: str = "12:45"
    call_start_2: str = "14:20"
    call_end_2: str = "17:20"
    call_start_3: str = "17:45"
    call_end_3: str = "18:30"

    # --- Database ---
    database_url: str = "sqlite:///./data/gsm_recovery.db"

    # --- Test-mode fixtures ---
    test_phone_number: str = "+923000000000"
    test_consumer_name: str = "Test Consumer"
    test_outstanding_amount: str = "5000"
    test_due_date: str = "2026-08-15"
    test_scheme: str = "3 installments of Rs. 1667"

    # --- Control API ---
    control_api_token: str | None = None

    # --- App server ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # --- Logging ---
    log_level: str = "INFO"

    @property
    def call_windows(self) -> list[tuple[time, time]]:
        """The three daily calling sessions as (start, end) time tuples."""
        return [
            (_parse_hhmm(self.call_start_1), _parse_hhmm(self.call_end_1)),
            (_parse_hhmm(self.call_start_2), _parse_hhmm(self.call_end_2)),
            (_parse_hhmm(self.call_start_3), _parse_hhmm(self.call_end_3)),
        ]

    def has_google_credentials(self) -> bool:
        return bool(self.google_service_account_json or self.google_service_account_file)

    def require_google_sheets(self) -> None:
        missing = []
        if not self.has_google_credentials():
            missing.append("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")
        if not self.google_spreadsheet_id:
            missing.append("GOOGLE_SPREADSHEET_ID")
        if missing:
            raise ConfigurationError(
                "Google Sheets integration is not configured. Missing: "
                + ", ".join(missing)
                + ". See README 'Google Sheets setup'."
            )

    def require_twilio(self) -> None:
        missing = []
        if self.telephony_provider != "twilio":
            raise ConfigurationError(
                f"TELEPHONY_PROVIDER={self.telephony_provider!r} has no implementation yet; "
                "only 'twilio' is implemented."
            )
        if not self.twilio_account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not self.twilio_auth_token:
            missing.append("TWILIO_AUTH_TOKEN")
        if not self.twilio_phone_number:
            missing.append("TWILIO_PHONE_NUMBER")
        if not self.public_base_url:
            missing.append("PUBLIC_BASE_URL")
        if missing:
            raise ConfigurationError(
                "Twilio telephony is not configured. Missing: "
                + ", ".join(missing)
                + ". See README 'Telephony provider setup'."
            )

    def require_ai(self) -> None:
        if not self.ai_api_key:
            raise ConfigurationError(
                "AI conversation engine is not configured. Missing: AI_API_KEY. "
                "See README 'AI provider setup'."
            )

    def require_speech(self) -> None:
        has_speech_creds = bool(
            self.google_speech_credentials_json
            or self.google_speech_credentials_file
            or self.has_google_credentials()
        )
        if not has_speech_creds:
            raise ConfigurationError(
                "Google Cloud Speech is not configured. Missing: "
                "GOOGLE_SPEECH_CREDENTIALS_JSON/FILE (or reuse GOOGLE_SERVICE_ACCOUNT_JSON/FILE "
                "if that service account has Speech API roles). See README 'Speech setup'."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
