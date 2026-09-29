"""Runtime configuration for the Dropship Scout Agent.

Reads credentials, endpoints, and tunables from the real environment plus a
local `.env` file (via python-dotenv). Values already present in the real
environment always win; a missing `.env` is not an error.

Security: this module never logs or prints the values it holds — `Settings`
deliberately stays a plain class (its default `object.__repr__` shows no
field values, so an accidental `repr(settings)` can never leak
`LLM_API_KEY` / `CJ_MCP_TOKEN`).
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

# Step-5 gold-kernel intake destination (plan §7.2). One supplier subfolder
# per engine, each numbered independently; this is the gold-kernel tree the
# updated pipeline produces, distinct from the general `dropship-candidates/`
# intake above.
DEFAULT_OPTIMAL_EXPORT_DIR = (
    "/Users/ahmadammari/PD/my-store-build/inspiration/optimal-dropship-candidates"
)

# Remote CJdropshipping MCP server (StreamableHTTP). The MCP token is
# appended as a path segment at connect time — see cj_mcp_client.py.
DEFAULT_CJ_MCP_BASE_URL = "https://developers.cjdropshipping.com/mcp"

# Saved AliExpress login (`storage_state`) for the Dropshipping Center
# extractor; produced by `scripts/generate_ali_session.py`, git-ignored, and
# injected only when the file exists — the DS Center answers these calls
# anonymously, so its absence is never an error.
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
        # --- AliExpress Dropshipping Center ingestion ---
        # Playwright `storage_state` JSON holding the operator's AliExpress
        # login; injected into the extractor's browser context when the file
        # exists, never required (the DS Center answers anonymously).
        self.ALI_DS_STATE_PATH: str = (
            os.getenv("ALI_DS_STATE_PATH") or DEFAULT_ALI_DS_STATE_PATH
        )
        # Catalogue-search page size per keyword: also the ceiling on how many
        # items one keyword expands through the per-item record call.
        self.ALI_DS_MAX_PRODUCTS: int = _parse_int(
            os.getenv("ALI_DS_MAX_PRODUCTS"), default=20
        )
        # Quantitative "winning product" gate. Both floors must be cleared by
        # every candidate before it reaches the LLM or the image stage; a
        # metric the DS Center does not report counts as unproven, and the
        # item is dropped.
        self.MIN_DS_ORDER_COUNT: int = _parse_int(
            os.getenv("MIN_DS_ORDER_COUNT"), default=500
        )
        self.MIN_DS_RATING: float = _parse_float(
            os.getenv("MIN_DS_RATING"), default=4.5
        )
        # Supplier extractors are tried in this order when the orchestrator
        # runs in auto mode; a comma-separated env override reorders or
        # narrows the chain. Defaults to CJ's MCP server first (no bot
        # walls, no scraping surface).
        self.SUPPLIER_PRIORITY_ORDER: List[str] = self._parse_supplier_order()
        # Supplier list prices are quoted in USD on AliExpress/CJ by
        # default; extractors convert to AUD with this rate so every
        # price_aud / margin figure is a like-for-like AUD number.
        self.USD_TO_AUD: float = _parse_float(os.getenv("USD_TO_AUD"), default=1.55)

        self.TARGET_COUNTRY: str = os.getenv("TARGET_COUNTRY", "AU")
        self.EXPORT_DIR: str = os.getenv("EXPORT_DIR", DEFAULT_EXPORT_DIR)
        self.USER_AGENT: str = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)

        # CJ commercial gate (plan §6.2). CJ reports no historical-sales
        # figure on any of its MCP tools, so `listedNum` — how many
        # dropshippers have imported the listing — is the only demand proof
        # the gate can read; it is applied to the raw search hits, before any
        # detail round-trip. 150 is set from the live spike: on real AU
        # catalogue pages a floor of 20 passed every hit, while 150 keeps the
        # widely-listed products and drops the unproven tail.
        self.MIN_CJ_LISTED_COUNT: int = _parse_int(
            os.getenv("MIN_CJ_LISTED_COUNT"), default=150
        )

        # Which CJ shipping method to cost against, by its own name (e.g.
        # "CJPacket Eub"). Blank selects the cheapest method the freight quote
        # offers, which is the usual intent; a pin is for when the operator
        # wants the cost basis tied to a specific service. A pin the quote
        # does not offer logs and falls back to the cheapest rather than
        # dropping the product.
        self.CJ_FREIGHT_METHOD: str = (
            os.getenv("CJ_FREIGHT_METHOD") or ""
        ).strip()

        # The CJ MCP extractor fetches full galleries per product via the
        # sku-detail tool; this caps how many products one keyword expands to.
        self.CJ_MAX_PRODUCTS_PER_KEYWORD: int = _parse_int(
            os.getenv("CJ_MAX_PRODUCTS"), default=10
        )

        # --- Step 3: Google Shopping gold-standard research (updated
        # multi-step pipeline) ---
        # Apify token, shared with the trends fallback MCP (both bill the same
        # Apify account). The actor is pay-per-result ($3.50/1,000 results
        # observed), so the per-keyword result cap is the spend ceiling.
        self.APIFY_TOKEN: Optional[str] = os.getenv("APIFY_TOKEN") or None
        # The Google Shopping actor id; overridable for a replacement actor
        # without a code change.
        self.APIFY_GS_ACTOR: str = (
            os.getenv("APIFY_GS_ACTOR") or "damilo/google-shopping-apify"
        )
        # Results requested per keyword (actor `num` field; closed set
        # 10/20/30/40/50/100 — anything else fails the run before spending).
        # NOTE (observed live 2026-09-28): the actor returns a full ~40-row
        # SERP page per keyword REGARDLESS of this value, so spend is
        # governed by keywords × ~40 × $0.0035, not by this cap.
        self.APIFY_GS_MAX_RESULTS_PER_KEYWORD: int = _parse_int(
            os.getenv("APIFY_GS_MAX_RESULTS_PER_KEYWORD"), default=10
        )
        # Hard spend ceiling, enforced by Apify itself (the run's
        # `max_total_charge_usd` option): the actor run aborts rather than
        # exceed it. Sized from the OBSERVED envelope (2026-09-28 pilot: the
        # actor returns a full ~40-row SERP page per keyword regardless of
        # `num`, so the full 40-keyword bank is ~1,600 results ≈ $5.60): the
        # ceiling must sit ABOVE that or the production run is aborted by its
        # own safety net, and it stays within the free-tier remainder
        # ($7.58 as of 2026-09-28) so even the worst case bills no real money.
        self.APIFY_GS_MAX_CHARGE_USD: float = _parse_float(
            os.getenv("APIFY_GS_MAX_CHARGE_USD"), default=7.5
        )
        # Step-3 deliverable path (untracked outputs/ tree — all production
        # deliverables of the updated pipeline live there, never a repo
        # artefact); Steps 4 and 6 read the gold product list from here.
        self.GOLD_PRODUCTS_PATH: str = (
            os.getenv("GOLD_PRODUCTS_PATH")
            or "outputs/step-3-gold-standard-products.json"
        )
        # Step-4 deliverable path — the Step-5 "keyword bank": the 50-70
        # supplier search keywords Step 4 generated from the gold products,
        # which Step 5 ingests through BOTH supplier pipelines (CJ MCP and the
        # AliExpress DS Center) and Step 6 ranks against the gold list. Same
        # untracked outputs/ tree as every other production deliverable.
        self.KEYWORD_BANK_PATH: str = (
            os.getenv("KEYWORD_BANK_PATH") or "outputs/step-4-gold-keywords.json"
        )
        # Step-5 export target PER ENGINE: the number of ACCEPTed packages
        # one (keyword, engine) leg may write. It is a per-leg target, so the
        # bank's keyword count multiplies it — 2 keeps a 60-keyword bank from
        # turning into hundreds of packages, while still giving each keyword
        # a real chance to contribute to both supplier folders.
        self.BANK_TARGET_PER_KEYWORD: int = _parse_int(
            os.getenv("BANK_TARGET_PER_KEYWORD"), default=2
        )
        # Step-5 gold-kernel intake root: the two supplier subfolders are
        # created under it by the runner, each numbered independently
        # (`cjdropshipping/product-NN`, `aliexpress/product-NN`).
        self.OPTIMAL_EXPORT_DIR: str = (
            os.getenv("OPTIMAL_EXPORT_DIR") or DEFAULT_OPTIMAL_EXPORT_DIR
        )

        # --- Margin floor (the deterministic half of gate 2) ---
        # An ACCEPT must clear markup_multiplier >= MIN_MARKUP_MULTIPLIER OR
        # estimated_margin_aud > MIN_MARGIN_AUD against the real landed cost;
        # anything else is downgraded to REJECT. Relaxed from the original
        # 3.0x / AUD 25 so realistic premium AU pricing survives (a real DS
        # Center cost is far higher than the welcome-deal prices the retired
        # Apify path reported, so a blind 3x on true cost over-prices the
        # store).
        self.MIN_MARKUP_MULTIPLIER: float = _parse_float(
            os.getenv("MIN_MARKUP_MULTIPLIER"), default=2.5
        )
        self.MIN_MARGIN_AUD: float = _parse_float(
            os.getenv("MIN_MARGIN_AUD"), default=20.0
        )

    @staticmethod
    def _parse_supplier_order() -> List[str]:
        raw = os.getenv("SUPPLIER_PRIORITY_ORDER")
        if not raw or not raw.strip():
            # CJ's official MCP server first (no anti-bot surface), then the
            # AliExpress Dropshipping Center. A stale key that names no
            # registered extractor is skipped with a warning by the chain
            # builder rather than failing the run.
            return ["cjdropshipping", "aliexpress"]
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