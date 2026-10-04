"""Application settings, loaded from environment variables (and `.env` in local dev)."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_DEV_FINGERPRINT_KEY = "local-dev-only-fingerprint-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Environment = "local"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # SQLite for local development and tests; PostgreSQL (asyncpg) in deployed environments.
    database_url: str = "sqlite+aiosqlite:///./data/jev_invoice.db"
    # Optional: only needed when the arq worker is used.
    redis_url: str | None = None

    # Original uploads are stored here, content-addressed by SHA-256.
    storage_dir: Path = Path("./data/files")
    # Screen uploads return at once and are processed in the background (watch it live on
    # the invoice's journey page). The JSON API always processes before it responds.
    background_processing: bool = True
    max_upload_bytes: int = 10 * 1024 * 1024

    # Key for HMAC fingerprints of bank account numbers (we never store the numbers).
    bank_fingerprint_key: SecretStr = SecretStr(_DEV_FINGERPRINT_KEY)

    # ── Extraction LLM (Groq) ─────────────────────────────────────────────────
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    llm_cost_per_input_token_usd: Decimal = Decimal("0.15") / 1_000_000
    llm_cost_per_output_token_usd: Decimal = Decimal("0.60") / 1_000_000
    llm_max_output_tokens: int = 4096
    llm_timeout_seconds: float = 60.0
    llm_max_attempts: int = 3
    # On a rate limit, wait as long as the provider asks (Groq's free tier counts tokens
    # per minute), but never longer than this.
    llm_retry_after_max_seconds: float = 60.0
    # Tokens one extraction or page transcription is expected to use. Before a model step
    # starts, the pipeline waits until the per-minute allowance has this much room, so
    # the wait is not counted as processing time.
    llm_tokens_per_call: int = Field(default=2500, ge=1)
    llm_daily_budget_usd: Decimal = Decimal("5.00")

    # ── Vision transcription for scans and photos ─────────────────────────────
    # Read on this machine with Ollama, so page images never leave it
    # (`ollama pull glm-ocr:q8_0`). Empty disables it: scans then fail with a clear message.
    vision_model: str | None = "glm-ocr:q8_0"
    ollama_base_url: str = "http://localhost:11434"
    # A page image needs far more than Ollama's default 4096-token context.
    ollama_num_ctx: int = Field(default=16_384, ge=4096)
    # Local models on a CPU are slow; one page can take a minute or more.
    ollama_timeout_seconds: float = 300.0
    # Caps a page that starts repeating itself. About 6,000 characters of text.
    ollama_max_output_tokens: int = Field(default=2048, ge=256)
    vision_max_pages: int = Field(default=5, ge=1, le=20)
    # Pages and photos are scaled so the longest side is at most this many pixels.
    # GLM-OCR dropped lines at 2000 px in testing; 1200 px read every line.
    vision_max_image_px: int = 1200

    # ── Decision model (TypeSafe Jev) ─────────────────────────────────────────
    typesafe_api_key: SecretStr | None = None
    # Pin an exact version so decisions are reproducible. Via BeatAPI: jev-1.13-free / jev-1.13.
    typesafe_model: str = "jev-1.13.0"
    # None = TypeSafe directly. https://api.beatapi.io = Jev through BeatAPI (same API).
    typesafe_base_url: str | None = None
    typesafe_timeout_seconds: float = 10.0
    jev_cost_per_input_token_usd: Decimal = Decimal("0.042") / 1_000_000
    jev_max_state_chars: int = 12_000
    # Minimum gap between Jev calls. 0 = no pacing. BeatAPI's free tier allows one call per
    # minute, so set 61 there: calls then wait their turn instead of failing with HTTP 429.
    jev_min_interval_seconds: float = Field(default=0.0, ge=0.0, le=600.0)

    # ── Business rules ────────────────────────────────────────────────────────
    # Our company: invoices must be addressed (billed) to it.
    company_name: str = "Northwind Analytics Inc"
    # The company's currency. Only invoices in it are processed automatically; any other
    # currency goes to a person (there is no exchange-rate conversion yet).
    base_currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    # Invoices above this total (in the base currency) need a budget owner's approval.
    approval_limit: Decimal = Decimal("5000.00")
    # Allowed difference when checking arithmetic (rounding on printed invoices).
    amount_tolerance: Decimal = Decimal("0.02")
    # How far an invoice may exceed the PO price or the received value before it is
    # flagged: this percentage OR this amount, whichever is smaller ("2% or $50"). A cap
    # matters on large orders: 2% of a $1,000,000 PO alone would let $20,000 through.
    po_price_tolerance_pct: Decimal = Decimal("2.0")
    po_tolerance_max_amount: Decimal = Decimal("50.00")
    # Same vendor and amount within this many days of another invoice: possible duplicate.
    duplicate_window_days: int = Field(default=7, ge=0, le=90)
    # Invoices dated longer ago than this are flagged as stale (late, or re-sent).
    stale_invoice_days: int = Field(default=180, ge=30, le=3650)

    # ── Export to accounting systems (CSV import files) ───────────────────────
    # US sales tax paid to a vendor cannot be reclaimed (unlike VAT): it is part of the cost.
    # True puts it on its own "Sales tax" line, coded like the purchase, with no tax code.
    # False exports it as reclaimable tax spread over the lines (VAT/GST countries).
    export_sales_tax_as_cost: bool = True
    # Tax names must match the target system's settings exactly (US defaults shown:
    # Xero US "Tax on Purchases" / "Tax Exempt", QuickBooks Online US "TAX" / "NON").
    xero_tax_type_taxed: str = "Tax on Purchases"
    xero_tax_type_untaxed: str = "Tax Exempt"
    quickbooks_tax_code_taxed: str = "TAX"
    quickbooks_tax_code_untaxed: str = "NON"
    quickbooks_date_format: str = "%m/%d/%Y"
    # Jev's account choice for a line is used at or above this confidence; below it the
    # vendor's default account applies (Jev's suggestion is still shown).
    coding_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # Approved costs with no known expense account go here to be recoded later.
    export_suspense_account: str = "9999"
    # Intuit recommends importing at most 100 bills at a time.
    export_max_invoices: int = Field(default=100, ge=1, le=1000)

    # ── Resilience (shared by LLM and Jev clients) ────────────────────────────
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_reset_seconds: float = 60.0

    @model_validator(mode="after")
    def _require_secrets_outside_local(self) -> "Settings":
        if self.app_env in ("staging", "production"):
            missing = [
                name
                for name, value in (
                    ("TYPESAFE_API_KEY", self.typesafe_api_key),
                    ("GROQ_API_KEY", self.groq_api_key),
                )
                if value is None or not value.get_secret_value()
            ]
            if self.bank_fingerprint_key.get_secret_value() == _DEV_FINGERPRINT_KEY:
                missing.append("BANK_FINGERPRINT_KEY")
            if missing:
                raise ValueError(f"Missing required settings for {self.app_env}: {missing}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
