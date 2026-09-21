"""Runtime configuration for the Dropship Scout Agent.

Reads credentials, endpoints, and tunables from the real environment plus a
local `.env` file (via python-dotenv). Values already present in the real
environment always win; a missing `.env` is not an error.

Security: this module never logs or prints the values it holds — `Settings`
deliberately stays a plain class (its default `object.__repr__` shows no
field values, so an accidental `repr(settings)` can never leak
`APIFY_API_TOKEN` / `LLM_API_KEY` / `CJ_MCP_TOKEN` / `ETSY_API_KEY`).
"""

import os
from typing import List, Optional

from dotenv import load_dotenv

# Load .env once at import time. Real environment variables take precedence
# over .env entries (override=False, the default), and a missing .env file is
# silently ignored.
load_dotenv()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_EXPORT_DIR = (
    "/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates"
)

# Remote CJdropshipping MCP server (StreamableHTTP). The MCP token is
# appended as a path segment at connect time — see cj_mcp_client.py.
DEFAULT_CJ_MCP_BASE_URL = "https://developers.cjdropshipping.com/mcp"

# AliExpress actor used by aliexpress_apify.py. Pinned to a pay-per-result
# actor: the free-tier monthly credit covers per-result charges but cannot
# pay an actor's flat monthly rental fee.
DEFAULT_APIFY_ALIEXPRESS_ACTOR = "cryptosignals/aliexpress-scraper"

# Saved AliExpress login (`storage_state`) used by the Dropshipping Center
# gate; produced by `scripts/generate_ali_session.py`, git-ignored, and read
# only when ENABLE_DS_CENTER_GATE is true.
DEFAULT_ALI_DS_STATE_PATH = "ali_ds_state.json"


# Blank or absent values resolve to the caller's default, so a `.env` key that
# is present but empty means "use the code default", never zero/False.
def _parse_bool(raw, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"true", "1", "yes"}


def _parse_int(raw, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _parse_float(raw, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


class Settings:
    """Snapshot of env-driven configuration. See module docstring for secrets."""

    def __init__(self) -> None:
        # --- Credentials (all optional; extractors validate at use time) ---
        self.APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN") or ""
        self.LLM_BASE_URL: str = os.getenv("LLM_BASE_URL") or ""
        self.LLM_API_KEY: str = os.getenv("LLM_API_KEY") or ""
        # Plan Part C: defaults to deepseek-v4-flash:cloud when unset or
        # empty; an explicit LLM_MODEL in .env / the environment still wins.
        self.LLM_MODEL: str = os.getenv("LLM_MODEL") or "deepseek-v4-flash:cloud"
        # CJdropshipping MCP token (src/pipeline/cj_mcp_client.py). Generated
        # on CJ's API Authorization page; embedded in the MCP endpoint URL as
        # a path segment. Optional — a missing token makes the CJ engine
        # raise ExtractorNotConfiguredError and the chain falls through.
        self.CJ_MCP_TOKEN: Optional[str] = os.getenv("CJ_MCP_TOKEN") or None
        # Remote MCP endpoint without the token; the client appends
        # `/{CJ_MCP_TOKEN}` at connect time. Overridable for testing/proxying;
        # a blank value falls back to the default endpoint.
        self.CJ_MCP_BASE_URL: str = (
            os.getenv("CJ_MCP_BASE_URL") or DEFAULT_CJ_MCP_BASE_URL
        )
        # Etsy Open API v3 key (src/extractors/etsy_api.py).
        self.ETSY_API_KEY: str = os.getenv("ETSY_API_KEY") or ""

        # --- Supplier-first extractor config (plan Part F) ---
        # Apify actor used by aliexpress_apify.py, and how many listings a
        # single actor run may return.
        self.APIFY_ALIEXPRESS_ACTOR: str = (
            os.getenv("APIFY_ACTOR_ID") or DEFAULT_APIFY_ALIEXPRESS_ACTOR
        )
        self.APIFY_MAX_ITEMS_PER_KEYWORD: int = _parse_int(
            os.getenv("APIFY_MAX_ITEMS"), default=20
        )
        # Budget guard for the pay-per-result actor: the per-result price is
        # used for the run's cost estimate, and the per-run item cap bounds
        # worst-case spend for one `fetch_products` call (the Apify account's
        # monthly spend limit is the authoritative ceiling).
        self.APIFY_PRICE_PER_RESULT_USD: float = _parse_float(
            os.getenv("APIFY_PRICE_PER_RESULT_USD"), default=0.005
        )
        self.APIFY_MAX_ITEMS_PER_RUN: int = _parse_int(
            os.getenv("APIFY_MAX_ITEMS_PER_RUN"), default=100
        )
        # Tight bound so a stuck proxy/captcha cannot drain the monthly
        # credit; the run is abandoned and the chain moves on.
        self.APIFY_RUN_TIMEOUT_SECS: int = _parse_int(
            os.getenv("APIFY_RUN_TIMEOUT_SECS"), default=60
        )
        # --- AliExpress Dropshipping Center gate ---
        # Off by default: the gate adds one authenticated DS Center lookup per
        # AliExpress candidate and requires a saved session (see
        # `scripts/generate_ali_session.py`). A blank/absent value means the
        # gate is disabled, never "unset".
        self.ENABLE_DS_CENTER_GATE: bool = _parse_bool(
            os.getenv("ENABLE_DS_CENTER_GATE"), default=False
        )
        # Playwright `storage_state` JSON holding the operator's AliExpress
        # login; injected into the extractor's browser context when the gate
        # is enabled.
        self.ALI_DS_STATE_PATH: str = (
            os.getenv("ALI_DS_STATE_PATH") or DEFAULT_ALI_DS_STATE_PATH
        )
        # Supplier extractors are tried in this order when the orchestrator
        # runs in auto mode; a comma-separated env override reorders or
        # narrows the chain. Defaults to CJ's MCP server first (no bot
        # walls, no scraping surface).
        self.SUPPLIER_PRIORITY_ORDER: List[str] = self._parse_supplier_order()
        # Supplier list prices are quoted in USD on AliExpress/CJ/Etsy by
        # default; extractors convert to AUD with this rate so every
        # price_aud / margin figure is a like-for-like AUD number.
        self.USD_TO_AUD: float = _parse_float(os.getenv("USD_TO_AUD"), default=1.55)

        self.TARGET_COUNTRY: str = os.getenv("TARGET_COUNTRY", "AU")
        self.EXPORT_DIR: str = os.getenv("EXPORT_DIR", DEFAULT_EXPORT_DIR)
        self.USER_AGENT: str = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)

        # The CJ MCP extractor fetches full galleries per product via the
        # sku-detail tool; this caps how many products one keyword expands to.
        self.CJ_MAX_PRODUCTS_PER_KEYWORD: int = _parse_int(
            os.getenv("CJ_MAX_PRODUCTS"), default=10
        )

    @staticmethod
    def _parse_supplier_order() -> List[str]:
        raw = os.getenv("SUPPLIER_PRIORITY_ORDER")
        if not raw or not raw.strip():
            # CJ's official MCP server first (no anti-bot surface), then the
            # Apify-hosted AliExpress scraper, then Etsy (needs a key).
            return ["cjdropshipping", "aliexpress", "etsy"]
        return [key.strip().lower() for key in raw.split(",") if key.strip()]


def load_settings() -> Settings:
    """Build a fresh `Settings` snapshot.

    Re-reads `os.environ` on every call so tests (and later phases) can
    `monkeypatch.setenv(...)` and re-snapshot without touching the cached
    module-level singleton below.
    """
    return Settings()


# Module-level singleton for convenient `from src.config import settings`.
settings = load_settings()