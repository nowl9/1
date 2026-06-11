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

    # ── Auxiliary latency-analysis capture (capture-only; --record-feeds) ─────
    # These streams are recorded ONLY under --record-feeds for offline latency
    # analysis (fast-spot vs Chainlink push vs Polymarket 5-min repricing lag).
    # NONE of them flow into pricing, signals, gates, or execution -- they are
    # recording-only and add no contract to the arb's tracked/signal universe.
    # See feeds/aux_capture.py and outputs/recorder_widening_report.md.
    #
    # Chainlink BTC/USD round state via raw JSON-RPC eth_call (no web3 dep).
    # Default RPC is publicnode (no API key required, reachable as of the
    # 2026-05-31 feasibility probe).  polygon-rpc.com returns 401 and Ankr
    # requires an API key, so neither is the default.  Operators behind a
    # network allowlist MUST add this host to it; if the RPC is unreachable
    # the chainlink capture self-disables (logs a warning) without affecting
    # the live trading path.
    chainlink_polygon_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    # Chainlink BTC/USD aggregator proxy on Polygon mainnet.
    chainlink_btc_usd_feed: str = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

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
    # Round 9 Commit 9a: lowered from 0.03 to 0.01 to unblock paper-trade
    # data flow.  At 3% the paper ledger stayed empty after Round 8's smoke
    # test (5 min runtime, no orders).  1% is a "noise floor" — below it the
    # signal is plausibly artifact of pipeline noise (basis-adjuster error,
    # BS rounding, IV smile interpolation).  This is a data-collection floor,
    # NOT a calibrated threshold; Round 9c replaces it with values backed
    # by realized P&L analysis on the accumulated paper-trade dataset.
    min_edge: float = Field(default=0.01, ge=0.0, le=1.0)
    min_days_to_expiry: int = Field(default=1, ge=0)
    max_days_to_expiry: int = Field(default=90, ge=1)
    max_position_usd: float = Field(default=1000.0, gt=0)
    max_total_exposure_usd: float = Field(default=10000.0, gt=0)
    # Strike increment δ for digital pricer finite-difference approximation (USD)
    strike_delta: float = Field(default=500.0, gt=0)

    # ── Recorder capture-path trust (2026-06-10 ENOSPC incident) ─────────────
    # Preflight floor: --record-feeds refuses to START when the recording
    # volume has less free space than this (GiB).  Measured exclusively via
    # shutil.disk_usage -- never by summing file sizes (sparse files lie).
    # Rationale: on 2026-06-10 a full disk (ENOSPC) silently killed all six
    # capture streams 2.7 h into an overnight run.
    recorder_min_free_gb: float = Field(default=20.0, ge=0.0)
    # Watchdog: a stream silent longer than this raises a CRITICAL alarm,
    # appends the WATCHDOG_ALARM sentinel, and attempts a supervised
    # restart of the dead component (capped per stream per run).
    recorder_watchdog_silence_s: float = Field(default=120.0, gt=0.0)
    recorder_watchdog_interval_s: float = Field(default=10.0, gt=0.0)
    recorder_watchdog_max_restarts: int = Field(default=3, ge=0)
    # Soft free-space alarm floor (GiB): CRITICAL while writes still work,
    # giving the operator a window to free space before hard ENOSPC.
    recorder_disk_soft_free_gb: float = Field(default=5.0, ge=0.0)
    # Persistent recorder/watchdog evidence log (gitignored outputs/).
    recorder_file_log_path: str = "./outputs/recorder.log"

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
