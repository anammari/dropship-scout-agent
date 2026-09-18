"""AliExpress listing extractor via the Apify platform (plan F.3.1).

Supplier-First ingestion: runs the managed pay-per-result
`cryptosignals/aliexpress-scraper` Apify actor once per keyword
(`action: "search"`) and maps its active listings (title / USD price /
currency / absolute imageUrl / productUrl) into `RawSupplierProduct`s.

The actor returns only a single image per listing, so this extractor
upgrades each candidate in place: it opens the item's own PDP in headless
Chromium (same proven image-view probe the old gallery pipeline used) and
harvests the full carousel gallery plus the page's meta description.
Candidates that cannot yield >= 3 distinct gallery URLs are skipped —
never fabricated.

The actor bills per scraped product, so every run is bounded twice: the
per-run item cap (`APIFY_MAX_ITEMS_PER_RUN`) and a tight `wait_duration`
stop, and the run's estimated cost is logged before the first call. The
Apify account's own monthly spend limit is the authoritative ceiling.

Zero LLM involvement. The `currency` input is pinned to USD and items
quoting any other currency are skipped, so the AUD conversion below is
always valid; prices become AUD via `settings.USD_TO_AUD`. AliExpress does
not return a shipping cost here, so `shipping_cost_aud` is 0.0 (the
unquoted-shipping caveat is stated in the COGS basis downstream).

Credentials: `APIFY_API_TOKEN` in `.env`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import List, Optional

from src.config import settings
from src.extractors.base import (
    BaseSupplierExtractor,
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
    ExtractorTimeoutException,
)
from src.models import RawSupplierProduct
from src.pipeline.image_sourcing import strip_size_suffix

logger = logging.getLogger(__name__)

_SUPPLIER_NAME = "AliExpress"

# AliExpress direct product page shape — every emitted supplier_retail_url
# must match (mirrors models._SUPPLIER_PDP_PATTERNS; defense in depth).
_PDP_PATTERN = re.compile(r"/item/\d+\.html")
_PRICE_NUMBER_RE = re.compile(r"[\d.]+")
_META_DESCRIPTION_JS = (
    "() => { const m = document.querySelector('meta[name=\"description\"]') "
    "|| document.querySelector('meta[property=\"og:description\"]'); "
    "return m ? (m.content || '') : ''; }"
)
# Same proven image-view/slider probe the strict image protocol used
# (largest srcset winner first, then inline/data-src sources).
_GALLERY_JS = """() => {
    const out = []; const seen = new Set();
    const push = (u) => { if (u && !seen.has(u)) { seen.add(u); out.push(u); } };
    const scopes = document.querySelectorAll('div[class*="image-view"], div[class*="slider"]');
    const imgs = scopes.length
        ? Array.from(scopes).flatMap(s => Array.from(s.querySelectorAll('img')))
        : [];
    for (const img of imgs) {
        const srcset = img.getAttribute('srcset') || '';
        if (srcset) {
            let best = '', bestW = -1;
            for (const part of srcset.split(',')) {
                const bits = part.trim().split(/\\s+/);
                const w = parseInt(bits[1] || '', 10) || 0;
                if (w >= bestW) { bestW = w; best = bits[0]; }
            }
            push(best);
        }
        push(img.currentSrc || img.src || img.getAttribute('data-src') || '');
    }
    return out;
}"""

_NAV_TIMEOUT_MS = 25000
_MAX_GALLERY_CANDIDATES = 16
_MIN_GALLERY = 3

# The actor's own hard ceiling for `maxItems` on a single run.
_MAX_ITEMS_PER_ACTOR_RUN = 500


def build_run_input(keyword: str, max_items: int, country: str) -> dict:
    """`cryptosignals/aliexpress-scraper` search payload (one run per keyword)."""
    return {
        "action": "search",
        "query": keyword,
        "maxItems": max_items,
        # `country` is the shipping destination the actor prices against;
        # `currency` is pinned because the AUD conversion below assumes USD.
        "country": country,
        "currency": "USD",
        "sort": "default",
        "proxyConfiguration": {"useApifyProxy": True},
    }


def _canonical_product_url(url: str) -> str:
    """Reduce an actor `productUrl` to the bare PDP link.

    The actor returns the search-result URL with its tracking query string
    (`algo_pvid`, `pdp_npi`, `search_p4p_id`, ...). Those parameters are
    session-scoped noise in a fulfilment link, so the canonical
    `/item/<id>.html` form is what gets exported.
    """
    match = _PDP_PATTERN.search(url)
    if not match:
        return url
    return f"https://www.aliexpress.com{match.group(0)}"


def _run_dataset_id(run) -> Optional[str]:
    """The run's default dataset id, across apify-client return shapes.

    apify-client 3.x returns a pydantic `Run` model exposing
    `default_dataset_id`; a dict-shaped run uses the camelCase key.
    """
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    return getattr(run, "default_dataset_id", None)


def _coerce_price_usd(raw) -> Optional[float]:
    """Actor `price` may be a number or a "US $5.32" string; None on junk."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    match = _PRICE_NUMBER_RE.search(str(raw).replace(",", ""))
    if not match:
        return None
    value = float(match.group(0))
    return value if value > 0 else None


def _coerce_images(raw) -> List[str]:
    """Thumbnail/images field normalizer (str, list, or list of dicts)."""
    urls: List[str] = []
    if isinstance(raw, str) and raw.startswith("http"):
        urls.append(raw)
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry.startswith("http"):
                urls.append(entry)
            elif isinstance(entry, dict):
                for key in ("url", "image", "imageUrl", "thumbnail"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        urls.append(value)
                        break
    seen = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


class AliExpressApifyExtractor(BaseSupplierExtractor):
    """Fetches active AliExpress listings through an Apify-managed actor."""

    engine_name = "aliexpress_apify"
    supplier_name = _SUPPLIER_NAME

    def __init__(
        self,
        token: Optional[str] = None,
        actor_id: Optional[str] = None,
        max_items: Optional[int] = None,
        max_items_per_run: Optional[int] = None,
        headless: Optional[bool] = None,
    ) -> None:
        self._token = token
        self._actor_id = actor_id
        self._max_items = max_items or settings.APIFY_MAX_ITEMS_PER_KEYWORD
        self._max_items_per_run = (
            max_items_per_run or settings.APIFY_MAX_ITEMS_PER_RUN
        )
        self._headless = (
            True if headless is None
            else headless
        )

    async def fetch_products(
        self, keywords: List[str], country: str = "AU"
    ) -> List[RawSupplierProduct]:
        token = self._token or settings.APIFY_API_TOKEN
        if not token:
            raise ExtractorNotConfiguredError("APIFY_API_TOKEN")
        actor_id = self._actor_id or settings.APIFY_ALIEXPRESS_ACTOR

        # The actor bills per scraped product, so the work is bounded before
        # it starts: one capped run per keyword, stopping once the per-run
        # item ceiling is consumed. The Apify account's monthly spend limit
        # remains the authoritative ceiling on real spend.
        caps: List[int] = []
        budget = self._max_items_per_run
        for _ in keywords:
            if budget <= 0:
                break
            cap = max(1, min(self._max_items, budget, _MAX_ITEMS_PER_ACTOR_RUN))
            caps.append(cap)
            budget -= cap
        planned = sum(caps)
        logger.info(
            "aliexpress run plan: %d keyword(s), up to %d item(s), estimated "
            "cost $%.3f (at $%s/result)",
            len(caps), planned,
            planned * settings.APIFY_PRICE_PER_RESULT_USD,
            settings.APIFY_PRICE_PER_RESULT_USD,
        )
        if len(caps) < len(keywords):
            logger.warning(
                "aliexpress per-run item cap (%d) reached; %d keyword(s) "
                "not run", self._max_items_per_run, len(keywords) - len(caps),
            )

        items: List[dict] = []
        for keyword, cap in zip(keywords, caps):
            items.extend(
                await asyncio.to_thread(
                    self._run_actor, token, actor_id,
                    build_run_input(keyword, cap, country),
                )
            )

        # First pass: build candidates from actor output; keep the thin
        # ones needing a gallery upgrade for the PDP harvest below.
        candidates: List[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("productUrl") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not raw_url or not _PDP_PATTERN.search(raw_url) or not title:
                continue
            url = _canonical_product_url(raw_url)
            # The actor can quote other currencies; the AUD conversion
            # assumes USD, so anything else is skipped rather than mispriced.
            currency = str(item.get("currency") or "").strip().upper()
            if currency and currency != "USD":
                logger.debug(
                    "aliexpress item skipped (currency=%s): %r", currency, url
                )
                continue
            price_usd = _coerce_price_usd(item.get("price"))
            if price_usd is None:
                logger.debug("aliexpress item skipped (no usable price): %r", url)
                continue
            images = _coerce_images(item.get("imageUrl") or item.get("images"))
            candidates.append(
                {
                    "url": url,
                    "title": title,
                    "price_usd": price_usd,
                    "images": images,
                }
            )
        if not candidates:
            logger.warning("aliexpress actor returned no usable listings")
            return []

        thin = [c for c in candidates if len(c["images"]) < _MIN_GALLERY]
        if thin:
            try:
                await self._harvest_galleries(thin)
            except Exception:
                logger.exception(
                    "aliexpress PDP gallery harvest crashed; keeping any "
                    "galleries captured so far"
                )

        products: List[RawSupplierProduct] = []
        for candidate in candidates:
            try:
                products.append(self._build_product(candidate))
            except Exception:
                logger.debug(
                    "aliexpress candidate skipped: %r", candidate["url"],
                    exc_info=True,
                )
        return products

    def _run_actor(self, token: str, actor_id: str, run_input: dict) -> List[dict]:
        """Run the actor and collect its dataset (blocking; call via a thread)."""
        from apify_client import ApifyClient

        client = ApifyClient(token)
        try:
            run = client.actor(actor_id).call(
                run_input=run_input,
                # apify-client 3.x: bound how long .call() waits for the
                # actor to finish (timedelta, not the removed timeout_secs).
                wait_duration=timedelta(seconds=settings.APIFY_RUN_TIMEOUT_SECS),
            )
        except Exception as exc:
            message = str(exc)
            if "401" in message or "unauthorized" in message.lower():
                raise ExtractorBlockedException(
                    f"Apify rejected the API token: {exc}"
                ) from exc
            if "timeout" in message.lower():
                raise ExtractorTimeoutException(f"Apify actor run timed out: {exc}")
            raise ExtractorBlockedException(f"Apify actor run failed: {exc}") from exc
        dataset_id = _run_dataset_id(run)
        if not dataset_id:
            raise ExtractorBlockedException(
                "Apify actor run produced no default dataset"
            )
        try:
            return list(client.dataset(dataset_id).iterate_items())
        except Exception as exc:
            raise ExtractorBlockedException(
                f"Apify dataset read failed: {exc}"
            ) from exc

    async def _harvest_galleries(self, candidates: List[dict]) -> None:
        """Upgrade thin candidates in place from their own PDPs.

        One headless Chromium session for the batch; each page is closed
        immediately after harvesting. Description is read from the page's
        meta description while the gallery probe runs.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=settings.USER_AGENT,
                locale="en-AU",
                viewport={"width": 1366, "height": 900},
            )
            await Stealth(
                navigator_user_agent_override=settings.USER_AGENT
            ).apply_stealth_async(context)
            try:
                for candidate in candidates:
                    page = await context.new_page()
                    try:
                        try:
                            await page.goto(
                                candidate["url"],
                                timeout=_NAV_TIMEOUT_MS,
                                wait_until="domcontentloaded",
                            )
                        except PlaywrightTimeoutError:
                            logger.debug(
                                "PDP load timed out: %s", candidate["url"]
                            )
                            continue
                        result = await page.evaluate(_GALLERY_JS)
                        urls = (
                            [u for u in result if isinstance(u, str) and u]
                            if isinstance(result, list)
                            else []
                        )
                        if not urls:
                            og = await page.evaluate(_META_DESCRIPTION_JS)
                            urls = [og] if isinstance(og, str) and og else []
                        # Upgrade suffixed assets: original first, as-served
                        # URL as fallback ordering (same rule the strict
                        # image protocol used for alicdn).
                        upgraded: List[str] = []
                        for url in urls[:_MAX_GALLERY_CANDIDATES]:
                            stripped = strip_size_suffix(url)
                            if stripped != url:
                                upgraded.append(stripped)
                            upgraded.append(url)
                        seen = set()
                        ordered = [
                            u for u in upgraded if not (u in seen or seen.add(u))
                        ]
                        if len(ordered) >= _MIN_GALLERY:
                            candidate["images"] = ordered
                        try:
                            meta = await page.evaluate(_META_DESCRIPTION_JS)
                            if isinstance(meta, str) and meta.strip():
                                candidate["description"] = meta.strip()[:4000]
                        except Exception:
                            logger.debug("meta description probe failed", exc_info=True)
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            logger.debug("PDP page close failed", exc_info=True)
            finally:
                await browser.close()

    def _build_product(self, candidate: dict) -> RawSupplierProduct:
        images = candidate["images"]
        if len(images) < _MIN_GALLERY:
            raise ValueError(
                f"aliexpress candidate {candidate['url']} gallery has only "
                f"{len(images)} image(s)"
            )
        return RawSupplierProduct(
            supplier_name=_SUPPLIER_NAME,
            supplier_retail_url=candidate["url"],
            product_title=candidate["title"],
            product_description=(
                candidate.get("description") or candidate["title"]
            ),
            price_aud=round(candidate["price_usd"] * settings.USD_TO_AUD, 2),
            shipping_cost_aud=0.0,
            image_urls=images,
        )