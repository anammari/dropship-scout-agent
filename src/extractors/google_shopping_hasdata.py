"""Step 3 (updated multi-step pipeline): HasData Google Shopping scraper.

The OPTIONAL alternative to the Apify actor in `google_shopping.py`, for
operators who would rather spend HasData credits than Apify credit. It is a
drop-in peer of `GoogleShoppingScraper`: same `scrape_keywords(keywords) ->
List[ShoppingRow]` contract, the same `ShoppingRow` dataclass, and therefore
the same downstream curation (`src.evaluators.gold_curator`) and the same
`--dump-raw` / `--from-raw` replay. Only the transport differs.

Why an HTTP client and not the MCP server (finding, 2026-10-07): the HasData
MCP gateway (`https://mcp.hasdata.com/mcp?apis=...`) enumerates 27 API groups
and `google_shopping` is NOT one of them — a tools/list probe against
`?apis=google_shopping` answers `{"error": "No HasData tools match ...",
"available": [airbnb, amazon, bing, ...]}` (google_serp, google_images,
google_travel, walmart and amazon are exposed; shopping is not). The official
HasData agent skill covers the same surface and likewise has no Google
Shopping reference. Neither agent-side route can serve a headless pipeline
runner in any case, so the reference guide's own fallback applies: "If
unsupported, standard HTTP client integration is preferred."

Cost: one request per keyword = **10 HasData credits** (published rate for
the Google Shopping API), and a request returns the WHOLE shopping grid for
that query (~65 rows observed live on 2026-10-07 for `french press`, gl=au),
not a fixed page size. `results_per_keyword` is therefore a LOCAL slice of
that grid, not an API parameter — it bounds curation cost, never API spend.
One keyword = one request regardless of the slice.

Schema source: the live payload (2026-10-07 probe, `/scrape/google/shopping`,
`q=french press&gl=au&domain=google.com.au`). The reference guide's example
row lists `link` / `productLink` / `seller` / `ratingCount`; NONE of those
four appear in the live payload. The fields actually returned are title,
category, productId, price, extractedPrice, originalPrice,
extractedOriginalPrice, rating, reviews, source, thumbnail, delivery,
extensions, hasdataLink, immersiveProductPageToken — so `source` is the
seller and `reviews` is the review count, and the parser reads both the
documented and the live spellings rather than trusting either alone. That is
the same posture as the Apify module's live-vs-README findings.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from src.config import settings
from src.extractors.google_shopping import ShoppingRow

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.hasdata.com"
REQUEST_TIMEOUT_SECONDS = 60.0

#: Google's regional domain per pipeline country. The DS Center/Step-2 work
#: is AU-pinned, so the AU entry is the one that matters; the rest keep the
#: module honest if TARGET_COUNTRY is changed.
GOOGLE_DOMAINS = {
    "AU": "google.com.au",
    "US": "google.com",
    "GB": "google.co.uk",
    "UK": "google.co.uk",
    "NZ": "google.co.nz",
    "CA": "google.ca",
    "IE": "google.ie",
    "IN": "google.co.in",
}
DEFAULT_GOOGLE_DOMAIN = "google.com"


class HasDataShoppingError(Exception):
    """The HasData shopping call failed or returned nothing usable."""


class HasDataNotConfiguredError(HasDataShoppingError):
    """HASDATA_API_KEY is missing — the API cannot be called without it."""


class HasDataShoppingRunFailedError(HasDataShoppingError):
    """The HTTP call failed, or the API answered with an error status."""


def parse_response(
    payload: Dict[str, Any],
    source_keyword: str,
    country: str = "AU",
) -> List[ShoppingRow]:
    """Map one HasData shopping response onto the shared ShoppingRow shape.

    Defensive on purpose: the documented and the live field names differ (see
    the module docstring), so every field is read from a small candidate list
    and a row is dropped only when it carries no usable title or identity.
    Rows without a product identity cannot serve as the anti-hallucination
    join key, so they are unusable rather than merely thin.
    """
    items = []
    for key in ("shoppingResults", "inlineShoppingResults"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(value)
    domain = GOOGLE_DOMAINS.get(country.upper(), DEFAULT_GOOGLE_DOMAIN)

    rows: List[ShoppingRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        product_id = str(item.get("productId") or "").strip()
        # Prefer a real listing link when the API supplies one (the reference
        # guide documents `link`/`productLink`); the live payload supplies
        # neither, so the row falls back to the canonical Google Shopping
        # product identity derived from its own `productId`. That is the same
        # documented-fallback shape as the CJ extractor's
        # `/product/{pid}.html` canonical link: derived, never invented.
        url = str(
            item.get("productLink") or item.get("link") or ""
        ).strip()
        if not url and product_id:
            url = f"https://www.{domain}/shopping/product/{product_id}"
        if not title or not url:
            logger.warning(
                "HasData shopping row without title/identity skipped: %r",
                str(item)[:160],
            )
            continue

        price = item.get("price")
        merchant = item.get("source") or item.get("seller")
        # `reviews` is the live spelling; `ratingCount` is the reference
        # guide's. Either satisfies the pipeline's demand-evidence rule.
        review_count = item.get("reviews")
        if review_count is None:
            review_count = item.get("ratingCount")
        rating = item.get("rating")
        thumbnail = item.get("thumbnail") or item.get("imageUrl")

        rows.append(
            ShoppingRow(
                title=title,
                url=url,
                merchant=str(merchant).strip() if merchant else None,
                price_text=str(price).strip() if price is not None else None,
                delivery_text=(
                    str(item["delivery"]).strip() if item.get("delivery") else None
                ),
                image_url=str(thumbnail).strip() if thumbnail else None,
                description=(
                    str(item["description"]).strip()
                    if item.get("description")
                    else None
                ),
                rating=(
                    float(rating) if isinstance(rating, (int, float)) else None
                ),
                rating_count=(
                    int(review_count)
                    if isinstance(review_count, (int, float))
                    else None
                ),
                source_keyword=source_keyword,
            )
        )
    return rows


class HasDataShoppingScraper:
    """Runs one HasData shopping request per keyword (10 credits each)."""

    #: Published rate for the Google Shopping API; used for the runner's
    #: spend banner and the run telemetry. HasData returns no per-call usage
    #: figure, so this is a rate, not a measurement.
    CREDITS_PER_REQUEST = 10

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        results_per_keyword: Optional[int] = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self.api_key = api_key or settings.HASDATA_API_KEY
        self.base_url = (base_url or settings.HASDATA_GS_BASE_URL).rstrip("/")
        cap = (
            results_per_keyword
            if results_per_keyword is not None
            else settings.HASDATA_GS_MAX_RESULTS_PER_KEYWORD
        )
        if cap < 1:
            raise HasDataShoppingError(
                f"results_per_keyword={cap} would discard every row; the "
                "HasData shopping API returns one grid per request, so a "
                "positive local cap is required"
            )
        self.results_per_keyword = cap
        self.timeout_seconds = timeout_seconds
        # Run telemetry for the caller's spend line; populated per run.
        self.last_run: Optional[dict] = None
        if self._client is None and not self.api_key:
            raise HasDataNotConfiguredError(
                "HASDATA_API_KEY is not configured — set it in .env to run "
                "the Google Shopping gold-product scrape (Step 3) on the "
                "HasData source"
            )

    def _client_or_build(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self.timeout_seconds)

    def _fetch_keyword(
        self, client: httpx.Client, keyword: str, country: str
    ) -> List[ShoppingRow]:
        """One shopping request; returns the keyword's parsed rows."""
        params = {
            "q": keyword,
            "gl": country.lower(),
            "domain": GOOGLE_DOMAINS.get(country.upper(), DEFAULT_GOOGLE_DOMAIN),
            "hl": "en",
            "deviceType": "desktop",
            "start": 0,
        }
        try:
            response = client.get(
                f"{self.base_url}/scrape/google/shopping",
                params=params,
                headers={
                    "x-api-key": self.api_key or "",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise HasDataShoppingRunFailedError(
                f"HasData shopping call failed for {keyword!r}: {exc}"
            ) from exc
        if response.status_code != 200:
            # The key is never echoed: only the status and a body head, which
            # HasData's own error shape does not put the key in.
            raise HasDataShoppingRunFailedError(
                f"HasData shopping HTTP {response.status_code} for "
                f"{keyword!r}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HasDataShoppingRunFailedError(
                f"HasData shopping returned non-JSON for {keyword!r}: "
                f"{response.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise HasDataShoppingRunFailedError(
                f"HasData shopping returned {type(payload).__name__} for "
                f"{keyword!r}, expected an object"
            )
        status = (payload.get("requestMetadata") or {}).get("status")
        if status not in (None, "ok"):
            raise HasDataShoppingRunFailedError(
                f"HasData shopping request status {status!r} for {keyword!r}"
            )
        return parse_response(payload, keyword, country)

    def scrape_keywords(self, keywords: List[str]) -> List[ShoppingRow]:
        """One request per keyword; returns the parsed listings.

        Fails closed on the FIRST keyword that cannot be read, matching the
        Apify scraper's single-run posture: a half-scraped gold list would
        silently under-represent the Step-2 keywords it claims to cover.
        """
        if not keywords:
            raise HasDataShoppingError("no keywords provided to scrape")

        client = self._client_or_build()
        logger.info(
            "HasData Google Shopping run: %d keyword(s) at %d credit(s) each "
            "(local row cap %d)",
            len(keywords), self.CREDITS_PER_REQUEST, self.results_per_keyword,
        )
        rows: List[ShoppingRow] = []
        for keyword in keywords:
            keyword_rows = self._fetch_keyword(client, keyword, settings.TARGET_COUNTRY)
            if not keyword_rows:
                raise HasDataShoppingRunFailedError(
                    f"HasData shopping produced no usable rows for {keyword!r}"
                )
            kept = keyword_rows[: self.results_per_keyword]
            logger.info(
                "HasData shopping: %d row(s) for %r, keeping %d",
                len(keyword_rows), keyword, len(kept),
            )
            rows.extend(kept)

        self.last_run = {
            "source": "hasdata",
            "keywords": len(keywords),
            "requests": len(keywords),
            "credits": len(keywords) * self.CREDITS_PER_REQUEST,
            "result_count": len(rows),
        }
        logger.info(
            "HasData Google Shopping run complete: %d row(s) from %d "
            "keyword(s), ~%d credit(s)",
            len(rows), len(keywords), self.last_run["credits"],
        )
        return rows
