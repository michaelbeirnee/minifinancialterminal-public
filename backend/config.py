"""Application configuration.

Values are read from environment variables (prefixed ``MFT_``) with sensible
defaults so the platform runs out-of-the-box for local development.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

#: The shipped placeholder. Named so the startup guard in backend.main can
#: recognise it rather than repeating the literal.
DEV_SECRET_KEY = "dev-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MFT_", env_file=".env", extra="ignore")

    # --- App ---
    app_name: str = "Mini Financial Terminal"
    # True marks a local development run. Set MFT_DEBUG=false when deploying:
    # backend.main refuses to boot on the placeholder secret key below unless
    # this is on, so a public deploy cannot silently ship forgeable tokens.
    debug: bool = True

    # --- Security ---
    # NOTE: override MFT_SECRET_KEY in any real deployment. This value signs
    # login tokens; anyone who knows it can mint one for any account.
    secret_key: str = DEV_SECRET_KEY
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24

    # Browser origins allowed to call this API cross-origin, comma-separated.
    # Empty means same-origin only, which is the arrangement the Dockerfile
    # ships: one process serves both the UI and the API. Populate it only when
    # the frontend moves to its own host.
    cors_origins: str = ""

    # Whether POST /api/auth/register accepts new accounts. Open sign-up on a
    # public host hands strangers the Assistant tab, which spends your Anthropic
    # credits; turn it off once your own account exists.
    allow_registration: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def using_dev_secret(self) -> bool:
        return self.secret_key == DEV_SECRET_KEY

    # --- Persistence ---
    database_url: str = f"sqlite:///{BASE_DIR / 'terminal.db'}"

    # --- Cache ---
    # TTL (seconds) for cached market-data responses.
    cache_ttl_seconds: int = 60 * 60
    cache_dir: str = str(BASE_DIR / "data_cache")

    # --- Platform (OpenBB-style data layer) ---
    http_timeout_seconds: float = 30.0
    # Require a bearer token on the auto-generated /api/v1/* data endpoints.
    platform_require_auth: bool = True
    # Command history rows kept per user; older runs are pruned as new ones land.
    max_history_rows_per_user: int = 500
    # SEC asks automated clients to identify themselves; put a real contact here.
    sec_user_agent: str = "Mini Financial Terminal research@example.com"

    # --- Optional free API keys -------------------------------------------
    # Every one of these is free to obtain, and every command that can use one
    # has a key-free fallback path, so the platform runs fully unconfigured.
    fred_api_key: Optional[str] = None  # https://fred.stlouisfed.org/docs/api/api_key.html
    eia_api_key: Optional[str] = None  # https://www.eia.gov/opendata/register.php
    bls_api_key: Optional[str] = None  # https://data.bls.gov/registrationEngine/
    nasdaq_api_key: Optional[str] = None  # https://data.nasdaq.com/sign-up

    # --- Live streaming ----------------------------------------------------
    # Yahoo's public streamer needs nothing and is the default. Alpaca is the
    # optional second source: a free app key (no funded account) buys licensed
    # trades *and* bid/ask over the IEX feed. https://alpaca.markets
    alpaca_api_key: Optional[str] = None
    alpaca_api_secret: Optional[str] = None
    # iex (free) | sip (paid) | delayed_sip (free, 15-minute) | test (FAKEPACA)
    alpaca_feed: str = "iex"
    # yahoo | alpaca — which source a stream request uses when it names none.
    # "alpaca" is only honoured once its keys are set; otherwise Yahoo serves.
    stream_default_provider: str = "yahoo"
    # The paper-trading endpoint the Alpaca execution adapter talks to. This
    # project places NO real-money orders: the adapter refuses any host other
    # than paper-api.alpaca.markets, so pointing this at the live API is an
    # error, not a feature.
    alpaca_paper_base: str = "https://paper-api.alpaca.markets"

    # --- Playground ---------------------------------------------------------
    # The Python playground executes arbitrary code as the server's own user —
    # that is the feature, and also why it follows the registration switch's
    # shape: on for local work, off on an internet-reachable deployment unless
    # switched on deliberately. Unset means "on exactly when MFT_DEBUG is".
    playground_enabled: Optional[bool] = None
    # Wall-clock ceiling per run; a kernel that exceeds it is killed (and its
    # variables lost), which is the honest cost of an infinite loop.
    playground_timeout_seconds: float = 120.0

    @property
    def playground_on(self) -> bool:
        if self.playground_enabled is not None:
            return self.playground_enabled
        return self.debug

    # --- Tick recorder -------------------------------------------------------
    # Where recorded live ticks live, as date-partitioned Parquet. Deliberately
    # NOT under cache_dir: the cache is clearable, recorded history is not
    # rebuildable — a tick nobody wrote down is gone.
    tick_store_dir: str = str(BASE_DIR / "tick_store")
    # Symbols to start recording at boot, comma-separated (e.g. "SPY,QQQ,BTC-USD").
    # Empty means the recorder starts idle and is driven from the API instead.
    record_symbols: str = ""

    # --- Assistant (the only paid dependency in the stack) -----------------
    # The one feature that is NOT free. Leave the key unset and every other
    # part of the platform still works; the Assistant tab simply reports that
    # it is switched off. https://platform.claude.com/
    anthropic_api_key: Optional[str] = None
    assistant_model: str = "claude-opus-5"
    # low | medium | high | xhigh | max — how hard the model thinks per reply.
    # "medium" suits a chat explainer that also calls a handful of tools.
    assistant_effort: str = "medium"
    # Caps thinking + reply text together; streaming, so a generous ceiling is
    # free until it is actually used.
    assistant_max_tokens: int = 16000
    # Conversation turns kept per request (the API is stateless — the browser
    # replays history, and this bounds what we forward).
    assistant_max_history: int = 40
    # Tool-call rounds allowed before the assistant must answer with what it has.
    assistant_max_tool_rounds: int = 6
    # Rows a single run_command tool result may put in front of the model.
    assistant_max_tool_rows: int = 40

    # --- Signal calibration ------------------------------------------------
    # The graded signal log is what turns the thesis engine's gate weights from
    # guesses into measurements, and it only fills if grading actually runs.
    # Hours between background sweeps; 0 switches the clock off and leaves
    # grading to POST /api/theses/signals/grade.
    grading_interval_hours: float = 12.0
    # Events examined per sweep. Grading is incremental, so a modest batch
    # catches up over several passes instead of one long stall at boot.
    grading_batch_size: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
