"""Step 3 (updated multi-step pipeline): Apify Google Shopping scraper.

Wraps `damilo/google-shopping-apify` (pay-per-result, ~$3.50/1,000 results,
no per-run fee) behind the repo's extractor idiom: one class, one fetch-style
method, the same not-configured/run-failed taxonomy. All keywords ride ONE
actor run (`queries` input takes priority over `query`) — spend is
per-result, so batching costs nothing extra and keeps the run single.

Extractor-shaped, deliberately NOT a `BaseSupplierExtractor`: these rows are
Google Shopping marketplace listings (merchant `link`, retail `price`,
`rating`/`ratingCount`), not supplier dropship listings. There is no supplier
PDP, no freight quote and no gallery to carry, so they can never become
`RawSupplierProduct` without fabricating sourcing fields — the
anti-hallucination contract forbids that. The module is never registered in
`_EXTRACTOR_REGISTRY` / `SUPPLIER_PRIORITY_ORDER`, and the retired
AliExpress Apify path (`extractors/aliexpress_apify.py`) stays deleted.

Schema source: the actor's own marketplace README example item (verified
2026-09-28): title, source, link, price ("$30.11"), delivery, imageUrl,
rating, ratingCount, offers, productId, position, query. Live input
validation (same date) proved `num` accepts ONLY 10/20/30/40/50/100 —
there is no smaller page, so a pilot's floor is 10 results per keyword.
Live output finding (same date, 80-row AU pilot): the `link` field carries
Google Shopping SEARCH urls (`google.com/search?ibp=oshop&...` with the
offer annotation), not the merchant's own PDP url — the README's
merchant-style example link is misleading. The url therefore serves as a
stable row identity for the anti-hallucination join and resolves to the
listing on Google itself; it is not a merchant product page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import List, Optional

from src.config import settings

logger = logging.getLogger(__name__)

#: The actor's `num` input is closed-set (live validation error 2026-09-28:
#: "Field input.num must be equal to one of the allowed values").
ALLOWED_RESULTS_PER_KEYWORD = (10, 20, 30, 40, 50, 100)


class GoogleShoppingError(Exception):
    """The actor run failed or returned nothing usable."""


class ApifyNotConfiguredError(GoogleShoppingError):
    """APIFY_TOKEN is missing — the actor cannot be run without it."""


class GoogleShoppingRunFailedError(GoogleShoppingError):
    """The actor run itself failed, aborted, or timed out."""


@dataclass
class ShoppingRow:
    """One Google Shopping AU listing, parsed defensively from the actor row."""

    title: str
    url: str
    merchant: Optional[str]
    price_text: Optional[str]
    delivery_text: Optional[str]
    image_url: Optional[str]
    description: Optional[str]
    rating: Optional[float]
    rating_count: Optional[int]
    source_keyword: str


def _parse_row(item: dict, source_keyword: str = "") -> Optional[ShoppingRow]:
    """Map one actor dataset item. Rows without a title or link are unusable."""
    title = str(item.get("title") or "").strip()
    url = str(item.get("link") or item.get("url") or "").strip()
    if not title or not url:
        logger.warning("Google Shopping row without title/link skipped: %r", item)
        return None
    rating = item.get("rating")
    rating_count = item.get("ratingCount")
    query = str(item.get("query") or source_keyword or "").strip()
    price = item.get("price")
    merchant = item.get("source")
    return ShoppingRow(
        title=title,
        url=url,
        merchant=str(merchant).strip() if merchant else None,
        price_text=str(price).strip() if price is not None else None,
        delivery_text=str(item["delivery"]).strip() if item.get("delivery") else None,
        image_url=str(item["imageUrl"]).strip() if item.get("imageUrl") else None,
        description=(
            str(item["description"]).strip() if item.get("description") else None
        ),
        rating=float(rating) if isinstance(rating, (int, float)) else None,
        rating_count=(
            int(rating_count) if isinstance(rating_count, (int, float)) else None
        ),
        source_keyword=query,
    )


class GoogleShoppingScraper:
    """Runs the Google Shopping actor once for a keyword list."""

    #: The actor README's `query` output field attributes each row back to
    #: its keyword; a row whose query names none of the requested keywords
    #: is logged (the row is still kept — the join verification downstream
    #: keys on url, not on this attribution).
    def __init__(
        self,
        apify_client=None,
        token: Optional[str] = None,
        actor: Optional[str] = None,
        results_per_keyword: Optional[int] = None,
        max_charge_usd: Optional[float] = None,
        timeout_seconds: int = 240,
    ) -> None:
        self._client = apify_client
        self.token = token or settings.APIFY_TOKEN
        self.actor = actor or settings.APIFY_GS_ACTOR
        self.results_per_keyword = (
            results_per_keyword or settings.APIFY_GS_MAX_RESULTS_PER_KEYWORD
        )
        if self.results_per_keyword not in ALLOWED_RESULTS_PER_KEYWORD:
            # Fail before spending money: the actor rejects any other value.
            raise GoogleShoppingError(
                f"results_per_keyword={self.results_per_keyword} is not in the "
                f"actor's allowed num values {ALLOWED_RESULTS_PER_KEYWORD}"
            )
        # Hard spend ceiling enforced by Apify itself (`max_total_charge_usd`
        # run option): the run aborts rather than exceed it.
        self.max_charge_usd = (
            max_charge_usd
            if max_charge_usd is not None
            else settings.APIFY_GS_MAX_CHARGE_USD
        )
        self.timeout_seconds = timeout_seconds
        # Run telemetry for the caller's spend line; populated per run.
        self.last_run: Optional[dict] = None
        if self._client is None and not self.token:
            raise ApifyNotConfiguredError(
                "APIFY_TOKEN is not configured — set it in .env to run the "
                "Google Shopping gold-product scrape (Step 3)"
            )

    def scrape_keywords(self, keywords: List[str]) -> List[ShoppingRow]:
        """One actor run over every keyword; returns the parsed listings."""
        if not keywords:
            raise GoogleShoppingError("no keywords provided to scrape")
        client = self._client
        if client is None:
            from apify_client import ApifyClient

            client = ApifyClient(self.token)
        run_input = {
            "queries": list(keywords),
            "country": "au",
            "language": "en",
            # The actor's input schema types `num` as a string.
            "num": str(self.results_per_keyword),
            "max_pages": 1,
        }
        logger.info(
            "Google Shopping run: %d keyword(s) x %s result(s) via %s",
            len(keywords), run_input["num"], self.actor,
        )
        # apify-client 3.x: run_timeout/wait_duration are timedeltas, and
        # call() returns a Run model (attributes, not a dict) or None when
        # the wait window elapses.
        try:
            run = client.actor(self.actor).call(
                run_input=run_input,
                run_timeout=timedelta(seconds=self.timeout_seconds),
                wait_duration=timedelta(seconds=self.timeout_seconds),
                max_total_charge_usd=Decimal(str(self.max_charge_usd)),
            )
        except Exception as exc:
            raise GoogleShoppingRunFailedError(
                f"Google Shopping actor call failed: {exc}"
            ) from exc
        if run is None:
            raise GoogleShoppingRunFailedError(
                "Google Shopping actor run did not finish within "
                f"{self.timeout_seconds}s"
            )
        status = str(run.status)
        usage = run.usage_total_usd
        self.last_run = {
            "run_id": run.id,
            "status": status,
            "usage_total_usd": float(usage) if usage is not None else None,
        }
        if status != "SUCCEEDED":
            raise GoogleShoppingRunFailedError(
                f"Google Shopping actor run {run.id} ended with "
                f"status {status!r}"
            )
        try:
            items = list(
                client.dataset(run.default_dataset_id).iterate_items()
            )
        except Exception as exc:
            raise GoogleShoppingError(
                f"Google Shopping dataset read failed: {exc}"
            ) from exc
        if not items:
            raise GoogleShoppingError(
                f"Google Shopping run {run.id} produced no rows"
            )
        requested = {k.strip().lower() for k in keywords}
        rows = []
        for item in items:
            row = _parse_row(item)
            if row is None:
                continue
            if row.source_keyword and row.source_keyword.lower() not in requested:
                logger.warning(
                    "Row query attribution %r is not a requested keyword; "
                    "keeping the row (join downstream keys on url)",
                    row.source_keyword,
                )
            rows.append(row)
        if not rows:
            raise GoogleShoppingError(
                f"Google Shopping run {run.id} produced "
                f"{len(items)} item(s) but none with both a title and a link"
            )
        self.last_run["result_count"] = len(rows)
        logger.info(
            "Google Shopping run %s SUCCEEDED: %d usable row(s) from %d item(s)",
            run.id, len(rows), len(items),
        )
        return rows