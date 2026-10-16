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
from pathlib import Path
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

# Step-5 Morning Anthem gold-kernel intake destination (plan §7.2). One
# supplier subfolder per engine (CJdropshipping only), numbered independently;
# this is the gold-kernel tree the Morning Anthem pipeline produces, distinct
# from the general `dropship-candidates/` intake above.
DEFAULT_MORNING_ANTHEM_EXPORT_DIR = (
    "/Users/ahmadammari/PD/my-store-build/inspiration/morning-anthem-candidates"
)

# Deliverable layout of the untracked `outputs/` tree. A run writes three
# kinds of artefact and each gets its own folder, so a run's output stays
# findable instead of piling up flat: structured deliverables in `json/`,
# their readable digests in `md/`, run logs in `logs/`.
OUTPUTS_DIR = Path("outputs")
OUTPUTS_JSON_DIR = OUTPUTS_DIR / "json"
OUTPUTS_MD_DIR = OUTPUTS_DIR / "md"


def digest_path(structured_path: Path) -> Path:
    """Where a structured deliverable's readable `.md` digest belongs.

    Inside the organised tree the digest goes to the sibling `md/` folder —
    a plain `with_suffix(".md")` would drop it back into `json/`, undoing
    the split. For any other destination the digest still sits beside its
    JSON, so a custom `--output` is never silently relocated.
    """
    # Match the trailing `outputs/json` pair, not a bare `json` folder name:
    # `--output /tmp/json/report.json` is outside the organised tree and must
    # keep its digest beside it.
    if (
        structured_path.parent.name == OUTPUTS_JSON_DIR.name
        and structured_path.parent.parent.name == OUTPUTS_DIR.name
    ):
        return structured_path.parent.parent / OUTPUTS_MD_DIR.name / (
            f"{structured_path.stem}.md"
        )
    return structured_path.with_suffix(".md")


# Remote CJdropshipping MCP server (StreamableHTTP). The MCP token is
# appended as a path segment at connect time — see cj_mcp_client.py.
DEFAULT_CJ_MCP_BASE_URL = "https://developers.cjdropshipping.com/mcp"


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
        # Plan Part C: defaults to deepseek-v4.1-flash when unset or empty; an
        # explicit LLM_MODEL in .env / the environment still wins. The pinned
        # build matters: ollama.com retired `deepseek-v4-flash:0731` on
        # 2026-09-25, so the old `deepseek-v4-flash:cloud` alias now answers
        # HTTP 410 Gone on every call. `deepseek-v4.1-flash` is the live
        # successor in the same tier.
        self.LLM_MODEL: str = os.getenv("LLM_MODEL") or "deepseek-v4.1-flash"
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
        # Supplier extractors are tried in this order when the orchestrator
        # runs in auto mode; a comma-separated env override reorders or
        # narrows the chain. Defaults to CJ's MCP server — The Morning
        # Anthem's sole supplier (the AliExpress Dropshipping Center engine
        # was retired on the morning-anthem branch).
        self.SUPPLIER_PRIORITY_ORDER: List[str] = self._parse_supplier_order()
        # CJ quotes list prices and freight in USD; the extractors convert to
        # AUD with this rate so every price_aud / margin figure is a
        # like-for-like AUD number (the store prices, carts and customer
        # payments are strictly in AUD at launch).
        self.USD_TO_AUD: float = _parse_float(os.getenv("USD_TO_AUD"), default=1.55)

        # The Morning Anthem launches selling and shipping to Australia
        # first; every freight quote and demand signal targets AU until the
        # store expands globally via Shopify Markets.
        self.TARGET_COUNTRY: str = os.getenv("TARGET_COUNTRY", "AU")
        # The CJ freight quote's destination lane (and the liveness gate's
        # preferred stock country). All launch products ship from CJ's China
        # warehouse directly into Australia, so the quote must name an
        # Australian delivery lane (e.g. CJPacket EUB). Kept as its own key
        # rather than reusing TARGET_COUNTRY: the ship-to lane and the demand
        # geography can diverge once the store opens more markets.
        self.CJ_DEFAULT_DEST_COUNTRY: str = (
            os.getenv("CJ_DEFAULT_DEST_COUNTRY") or "AU"
        )
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

        # --- Step 3: Google Shopping gold-standard research (Morning
        # Anthem pipeline). HasData is the EXCLUSIVE data provider — the
        # Apify actor wrapper and every APIFY_* key were retired on the
        # morning-anthem branch. ---
        # The same credential the Step-2 Google Trends MCP server uses
        # (`.mcp.json` reads it through scripts/mcp_headers.py; the Step-3
        # source reads it here to call the REST API directly). Optional — a
        # missing key makes the HasData source raise
        # HasDataNotConfiguredError.
        self.HASDATA_API_KEY: Optional[str] = os.getenv("HASDATA_API_KEY") or None
        # HasData REST base; the client appends `/scrape/google/shopping`.
        self.HASDATA_GS_BASE_URL: str = (
            os.getenv("HASDATA_GS_BASE_URL") or "https://api.hasdata.com"
        )
        # LOCAL cap on rows kept per keyword. This is NOT an API parameter:
        # one HasData request returns the whole shopping grid for a query
        # (~65 rows observed live 2026-10-07) and costs 10 credits whatever
        # the slice. The cap therefore bounds downstream LLM curation cost,
        # never HasData spend. 20 keeps the curation budget tight under the
        # Morning Anthem credit envelope.
        self.HASDATA_GS_MAX_RESULTS_PER_KEYWORD: int = _parse_int(
            os.getenv("HASDATA_GS_MAX_RESULTS_PER_KEYWORD"), default=20
        )
        # Demand geography for the Step-2 Trends research (agent-side) and
        # the Step-3 Google Shopping scrape. The Morning Anthem launches
        # Australia-first, so both default to the AU market — demand signals
        # and price benchmarks reflect Australian shopper behaviour and AUD
        # retail pricing. Re-point them when the store expands globally.
        self.TRENDS_GEO: str = os.getenv("TRENDS_GEO", "AU")
        self.SHOPPING_GL: str = os.getenv("SHOPPING_GL", "au")
        self.SHOPPING_DOMAIN: str = (
            os.getenv("SHOPPING_DOMAIN") or "google.com.au"
        )
        # HasData free-tier budget guardrails (credits/month). A full
        # production run must stay within HASDATA_PRODUCTION_CAP of the
        # monthly budget; the Step-3 runner refuses to start a scrape whose
        # planned keyword count would exceed HASDATA_GS_MAX_KEYWORDS or push
        # the run past the production cap, before any credit moves.
        self.HASDATA_MONTHLY_BUDGET: int = _parse_int(
            os.getenv("HASDATA_MONTHLY_BUDGET"), default=1000
        )
        # The launch-run profile (operator decision 2026-10-12): a one-off
        # full run that front-loads the month's research into one gold list,
        # because the CJ side of the pipeline costs no credits. At the rates
        # below the two halves sum to the cap exactly:
        #   Step 2  TRENDS_MAX_CALLS (10) x 10 = 100
        #   Step 3  30 keywords x 10            = 300
        #                                       = 400 = HASDATA_PRODUCTION_CAP
        # ~600 credits of the monthly tier stay in reserve for the per-step
        # pilots and any re-run.
        self.HASDATA_PRODUCTION_CAP: int = _parse_int(
            os.getenv("HASDATA_PRODUCTION_CAP"), default=400
        )
        self.HASDATA_GS_MAX_KEYWORDS: int = _parse_int(
            os.getenv("HASDATA_GS_MAX_KEYWORDS"), default=30
        )
        # The rates the guardrail arithmetic is computed from, so a confirmed
        # figure is a one-line change rather than a hunt through the runners.
        # Trends is 10 credits per request, CONFIRMED by the operator on the
        # HasData dashboard 2026-10-12 (stated on the endpoint's Run Request
        # button; documented at docs.hasdata.com/apis/google-trends/search).
        # The repo's older "5 credits" note was stale.
        self.HASDATA_TRENDS_CREDITS_PER_CALL: int = _parse_int(
            os.getenv("HASDATA_TRENDS_CREDITS_PER_CALL"), default=10
        )
        self.HASDATA_SHOPPING_CREDITS_PER_REQUEST: int = _parse_int(
            os.getenv("HASDATA_SHOPPING_CREDITS_PER_REQUEST"), default=10
        )
        # Step-3 deliverable path (untracked outputs/ tree — all production
        # deliverables of the Morning Anthem pipeline live there, never a
        # repo artefact); Steps 4 and 6 read the gold product list from here.
        self.MORNING_ANTHEM_GOLD_PRODUCTS_PATH: str = (
            os.getenv("MORNING_ANTHEM_GOLD_PRODUCTS_PATH")
            or "outputs/json/morning-anthem-step-3-gold-products.json"
        )
        # Step-4 deliverable path — the Step-5 "keyword bank": the supplier
        # search keywords Step 4 generated from the gold products, which
        # Step 5 ingests through the CJdropshipping MCP pipeline and Step 6
        # ranks against the gold list. Same untracked outputs/ tree as every
        # other production deliverable.
        self.MORNING_ANTHEM_KEYWORD_BANK_PATH: str = (
            os.getenv("MORNING_ANTHEM_KEYWORD_BANK_PATH")
            or "outputs/json/morning-anthem-step-4-keyword-bank.json"
        )
        # Step-2 deliverable path — the Jev-gated trend keyword list that
        # feeds Step 3's Google Shopping scrape.
        self.MORNING_ANTHEM_TREND_KEYWORDS_PATH: str = (
            os.getenv("MORNING_ANTHEM_TREND_KEYWORDS_PATH")
            or "outputs/json/morning-anthem-step-2-trend-keywords.json"
        )
        # Step-5 export target PER (PRODUCT, ENGINE): the number of ACCEPTed
        # packages one product may contribute through one engine. The PRODUCT is
        # the unit of ingestion — its exact-title keyword runs first and its
        # fallback only fills whatever slots the exact keyword left open, so a
        # fallback is never paid for when the exact title already worked.
        self.BANK_TARGET_PER_PRODUCT: int = _parse_int(
            os.getenv("BANK_TARGET_PER_PRODUCT"), default=2
        )
        # Step-6 ranking report — Step 7's cleanup intake, and the record of
        # what was ranked (including what cleanup later deleted).
        self.MORNING_ANTHEM_RANKED_CANDIDATES_PATH: str = (
            os.getenv("MORNING_ANTHEM_RANKED_CANDIDATES_PATH")
            or "outputs/json/morning-anthem-step-6-ranked-candidates.json"
        )
        # Step-7 cleanup: how many `review` packages survive the cut. Shortlist
        # packages are never deleted and `disregard` packages always are, so
        # this is the only number that decides a borderline case — and deleting
        # wrongly costs the HasData credits, CJ round trips and LLM calls that
        # bought the package, which is why the runner is a dry run by default.
        self.CLEANUP_REVIEW_KEEP: int = _parse_int(
            os.getenv("CLEANUP_REVIEW_KEEP"), default=10
        )
        # Step-7 cleanup: the GLOBAL ceiling on one product family, so a
        # saturated family cannot flood the catalogue however well it scores
        # (operator directive 2026-10-16, after 22 of 23 kept `sunrise_move`
        # packages turned out to be dog gear). Families are derived from the
        # package title (`cleanup.FAMILY_RULES`); a package in no capped family
        # is unconstrained. This cap OVERRIDES the shortlist keep.
        self.CLEANUP_MAX_PER_FAMILY: int = _parse_int(
            os.getenv("CLEANUP_MAX_PER_FAMILY"), default=5
        )
        # Step-5 Morning Anthem gold-kernel intake root: the supplier
        # subfolder is created under it by the runner, numbered independently
        # (`cjdropshipping/product-NN`).
        self.MORNING_ANTHEM_EXPORT_DIR: str = (
            os.getenv("MORNING_ANTHEM_EXPORT_DIR")
            or DEFAULT_MORNING_ANTHEM_EXPORT_DIR
        )

        # --- Step 6: Jev product ranking (TypeSafe System One via OpenRouter;
        # updated multi-step pipeline) ---
        # OpenRouter API key (`sk-or-v1-…`), the operator's credential for the
        # Jev-hosted System One endpoint. Optional at import (the supplier
        # core never needs it); the Step-6 ranker raises `JevConfigError` when
        # it is absent, which the runner renders as an intervention block —
        # that is how the step gates on the operator's credential cadence.
        self.OPENROUTER_API_KEY: Optional[str] = (
            os.getenv("OPENROUTER_API_KEY") or None
        )
        # OpenRouter API base; the System One path (`/systemone`) is appended
        # by the client. A blank value falls back to the public endpoint.
        self.OPENROUTER_BASE_URL: str = (
            os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
        )
        # The System One model id (OpenRouter form). `typesafe/jev-1.13` is the
        # pinned version; the `~typesafe/jev-latest` alias tracks the moving
        # latest and is deliberately not the default (a pinned model keeps a
        # ranking reproducible).
        self.JEV_MODEL: str = (
            os.getenv("JEV_MODEL") or "perplexity/pplx-decider-v1.1-27b"
        )
        # Products per System One call. The native batch is one shared state +
        # many independent questions, and TypeSafe cites ~13 questions per
        # call as ~11.5x cheaper / 9.6x faster than separate calls; at three
        # questions per product, 4 products = 12 questions sits right at that
        # envelope. Cost control, not a hard ceiling.
        self.JEV_BATCH_SIZE: int = _parse_int(
            os.getenv("JEV_BATCH_SIZE"), default=4
        )
        # Tier boundaries on `rank_score` (0.6*similarity + 0.4*value). Jev
        # reports a score POSITION along an ordered level list; the ranker
        # shifts Jev's 0-based position onto a 1-5 scale (§8.0 semantics), so
        # both floors read on 1-5: >= 3.5 -> shortlist, >= 2.5 -> review,
        # else disregard. The shortlist floor was widened from 4.0 with the
        # operator's 2026-09-29 calibration (the old 4.0 needed near-perfect
        # similarity and left the viable review band unharvested). The two
        # score questions and the choice question live in
        # `src/ranking/jev_client.py`, so all judgement edits are in one
        # place.
        self.JEV_SHORTLIST_MIN_SCORE: float = _parse_float(
            os.getenv("JEV_SHORTLIST_MIN_SCORE"), default=3.5
        )
        self.JEV_REVIEW_MIN_SCORE: float = _parse_float(
            os.getenv("JEV_REVIEW_MIN_SCORE"), default=2.5
        )
        # Send the package's own supplier photo with the decision.
        #
        # OFF by default, on measurement (2026-10-16). The Step-6 decision
        # endpoint takes text state only: three solid-colour test images (red,
        # blue, white) all came back "white" with identical probability
        # distributions, so the model never reads the pixels. A full-size data
        # URL is also counted as text and costs ~177k tokens, which pushes a
        # four-package batch past the 262,144-token ceiling and fails the call.
        # Leave this off unless the transport is replaced with a real vision
        # model.
        self.JEV_SEND_IMAGES: bool = _parse_bool(
            os.getenv("JEV_SEND_IMAGES"), default=False
        )
        self.JEV_IMAGES_PER_PACKAGE: int = _parse_int(
            os.getenv("JEV_IMAGES_PER_PACKAGE"), default=1
        )

        # --- Margin floor (the deterministic half of gate 2) ---
        # An ACCEPT must clear markup_multiplier >= MIN_MARKUP_MULTIPLIER OR
        # estimated_margin_aud > MIN_MARGIN_AUD against the real landed cost
        # (CJ's USD list price + freight, converted to AUD); anything else is
        # downgraded to REJECT. AUD 10.00 is the operator's target gross
        # margin per unit, supporting the >= 20% margin threshold at launch.
        self.MIN_MARKUP_MULTIPLIER: float = _parse_float(
            os.getenv("MIN_MARKUP_MULTIPLIER"), default=2.5
        )
        self.MIN_MARGIN_AUD: float = _parse_float(
            os.getenv("MIN_MARGIN_AUD"), default=10.0
        )

        # --- Step 2: Jev product-intent gate (Morning Anthem pipeline) ---
        # Candidate trend strings per System One call. One choice question
        # rides each candidate (`<i>__product_intent`); 8 strings = 8
        # questions per call, inside the docs' efficient batch envelope.
        self.TREND_GATE_BATCH_SIZE: int = _parse_int(
            os.getenv("TREND_GATE_BATCH_SIZE"), default=8
        )
        # Step-2 Trends research hard cap: at most this many Trends calls
        # (5 credits each) per production research pass, protecting the
        # 250-credit production envelope (50 of the 250 go to Step 2).
        self.TRENDS_MAX_CALLS: int = _parse_int(
            os.getenv("TRENDS_MAX_CALLS"), default=10
        )
        # Step-2 Jev SELECTION pass: the gated pool is wider than the Step-3
        # scrape can afford (one Google Shopping request per keyword), so Jev
        # scores every survivor and code cuts the best TREND_SELECT_TARGET of
        # them, with a per-pillar floor so a strong pillar cannot crowd a
        # weaker one out of the launch. The target IS the Step-3 budget.
        self.TREND_SELECT_BATCH_SIZE: int = _parse_int(
            os.getenv("TREND_SELECT_BATCH_SIZE"), default=12
        )
        self.TREND_SELECT_TARGET: int = _parse_int(
            os.getenv("TREND_SELECT_TARGET"), default=30
        )
        self.TREND_SELECT_MIN_PER_PILLAR: int = _parse_int(
            os.getenv("TREND_SELECT_MIN_PER_PILLAR"), default=2
        )
        # --- Step 3: Jev pillar gate (Morning Anthem pipeline) ---
        # Candidate products per System One call. One choice question rides
        # each product (`<i>__pillar`); 4 products = 4 questions per call.
        self.PILLAR_GATE_BATCH_SIZE: int = _parse_int(
            os.getenv("PILLAR_GATE_BATCH_SIZE"), default=4
        )

    @staticmethod
    def _parse_supplier_order() -> List[str]:
        raw = os.getenv("SUPPLIER_PRIORITY_ORDER")
        if not raw or not raw.strip():
            # CJ's official MCP server — The Morning Anthem's sole supplier
            # (no anti-bot surface). A stale key that names no registered
            # extractor is skipped with a warning by the chain builder
            # rather than failing the run.
            return ["cjdropshipping"]
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