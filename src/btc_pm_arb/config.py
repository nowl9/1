"""Application configuration via Pydantic Settings (populated from .env)."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Deribit ──────────────────────────────────────────────────────────────
    deribit_ws_url: str = "wss://www.deribit.com/ws/api/v2"
    deribit_testnet_ws_url: str = "wss://test.deribit.com/ws/api/v2"
    deribit_use_testnet: bool = False

    @property
    def deribit_url(self) -> str:
        return self.deribit_testnet_ws_url if self.deribit_use_testnet else self.deribit_ws_url

    # ── Polymarket (public data only — US-restricted, no execution) ──────────
    # Trading credentials are intentionally NOT defined here. The agent runs
    # without them; any signal targeting Polymarket is logged as
    # `signal.polymarket_data_only` and skipped at the execution layer.
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_chain_id: int = 137

    # ── Kalshi ────────────────────────────────────────────────────────────────
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private_key.pem"
    kalshi_use_demo: bool = True
    kalshi_demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_prod_url: str = "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def kalshi_base_url(self) -> str:
        return self.kalshi_demo_url if self.kalshi_use_demo else self.kalshi_prod_url

    # ── Strategy ──────────────────────────────────────────────────────────────
    min_edge: float = Field(default=0.03, ge=0.0, le=1.0)
    min_days_to_expiry: int = Field(default=1, ge=0)
    max_days_to_expiry: int = Field(default=90, ge=1)
    max_position_usd: float = Field(default=1000.0, gt=0)
    max_total_exposure_usd: float = Field(default=10000.0, gt=0)
    # Strike increment δ for digital pricer finite-difference approximation (USD)
    strike_delta: float = Field(default=500.0, gt=0)

    # ── Paper-trading ledger (Round 8) ────────────────────────────────────────
    # Append-only JSONL files persist would-be-trade records across restarts.
    # See execution/paper_ledger.py module docstring for storage rationale.
    paper_ledger_dir: str = "./paper_ledger"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v.upper()

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        allowed = {"json", "console"}
        if v.lower() not in allowed:
            raise ValueError(f"log_format must be one of {allowed}")
        return v.lower()


# Module-level singleton — import this everywhere.
settings = Settings()
