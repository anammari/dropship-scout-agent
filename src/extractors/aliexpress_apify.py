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

With `ENABLE_DS_CENTER_GATE` on, every candidate is additionally checked
against the AliExpress Dropshipping Center before its PDP image extraction
and dropped when the DS Center does not list it — see the gate section below.

Zero LLM involvement. The `currency` input is pinned to USD and items
quoting any other currency are skipped, so the AUD conversion below is
always valid; prices become AUD via `settings.USD_TO_AUD`. AliExpress does
not return a shipping cost here, so `shipping_cost_aud` is 0.0 (the
unquoted-shipping caveat is stated in the COGS basis downstream).

Credentials: `APIFY_API_TOKEN` in `.env`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import timedelta
from pathlib import Path
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
_PDP_ID_PATTERN = re.compile(r"/item/(\d+)\.html")
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


# --- AliExpress Dropshipping Center gate --------------------------------
# (plans/AliExpress Dropshipping Center Gating.md, steps 3.2-3.3.)
#
# The DS Center Product Analysis page (`/product-analysis?itemId=<id>`)
# analyzes the item itself; the item verdict comes from the same internal
# XHR the DS Center UI issues — `selection.queryByItemUrl`, reached through
# MTOP's token-then-sign handshake on the authenticated context. The DOM
# probe below is the fallback: it reads the page the analysis was rendered
# into, so a rejection stays detectable if the internal API changes shape.
_DS_ANALYSIS_URL = "https://ds.aliexpress.com/product-analysis"
_DS_ITEM_API = "mtop.aidc.ds.center.selection.queryByItemUrl"
_DS_ITEM_API_ORIGIN = "https://acs.aliexpress.com"
_DS_APP_KEY = "12574478"
_DS_JSV = "2.7.2"
# `code`/`message` the DS Center returns when it holds no record of the item.
_DS_ABSENT_CODES = {"-1"}
_DS_ABSENT_MESSAGES = {"none_of_item"}
# MTOP `ret` flags meaning the saved session can no longer be used.
_DS_SESSION_FAILURES = (
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_ILLEGAL_ACCESS",
)
_LOGIN_URL_MARKERS = ("login.aliexpress.com", "/login.")
_DS_COUNTRY_COOKIE = "aep_usuc_f"

_DS_DOM_PROBE_JS = """async () => {
    const bodyText = () => (document.body ? document.body.innerText : '') || '';
    const rows = () => document.querySelectorAll('tbody tr').length;
    const loginWall = () => !!document.querySelector('input[type="password"]');
    const marker = () => {
        const found = bodyText().match(
            /(no data|not supported|unsupported|not available|no result|暂不支持|暂无数据|不支持)/i);
        return found ? found[0] : null;
    };
    // The analysis is rendered client-side over a few seconds, and the table
    // is briefly empty before the response lands — so a marker only counts as
    // a verdict if it is still there once the page has settled.
    const deadline = Date.now() + 10000;
    let reject = null;
    while (Date.now() < deadline) {
        if (loginWall()) return {loginWall: true, reject: null, rows: rows()};
        if (rows() > 0) return {loginWall: false, reject: null, rows: rows()};
        reject = marker();
        await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return {loginWall: loginWall(), reject: reject, rows: rows()};
}"""

_SESSION_EXPIRED_MESSAGE = (
    "The saved AliExpress Dropshipping Center session is no longer valid. "
    "Re-run `python scripts/generate_ali_session.py`, log in, and press "
    "Enter to refresh the stored state."
)
_MISSING_STATE_MESSAGE = (
    "ENABLE_DS_CENTER_GATE is on but no saved AliExpress session was found. "
    "Run `python scripts/generate_ali_session.py`, log in, and press Enter "
    "to create the storage state."
)


class DsCenterSessionExpiredError(RuntimeError):
    """The saved AliExpress session is missing, expired, or challenged."""


def _ds_item_id(product_url: str) -> Optional[str]:
    """The numeric item id from an AliExpress PDP URL, or None."""
    match = _PDP_ID_PATTERN.search(product_url)
    return match.group(1) if match else None


def _h5_sign(token: str, timestamp: str, data: str) -> str:
    """MTOP H5 request signature: md5(token&t&appKey&data)."""
    digest = hashlib.md5(
        f"{token}&{timestamp}&{_DS_APP_KEY}&{data}".encode("utf-8")
    )
    return digest.hexdigest()


async def _h5_token(context) -> Optional[str]:
    """The `_m_h5_tk` token (pre-underscore half) from the context's cookies."""
    for cookie in await context.cookies(_DS_ITEM_API_ORIGIN):
        if cookie.get("name") == "_m_h5_tk":
            token = str(cookie.get("value") or "").split("_")[0]
            return token or None
    return None


async def _ds_item_lookup(context, item_id: str) -> Optional[dict]:
    """Ask the DS Center what it knows about one item (authenticated context).

    Mirrors the XHR the DS Center UI issues: a token-priming call, then the
    signed call. Returns the decoded payload, or None when no verdict could
    be read from the exchange.
    """
    payload_data = json.dumps(
        {"itemUrl": f"https://www.aliexpress.com/item/{item_id}.html"},
        separators=(",", ":"),
    )
    url = f"{_DS_ITEM_API_ORIGIN}/h5/{_DS_ITEM_API}/1.0/"
    params = {
        "jsv": _DS_JSV,
        "appKey": _DS_APP_KEY,
        "api": _DS_ITEM_API,
        "v": "1.0",
        "type": "originaljson",
        "dataType": "json",
        "data": payload_data,
    }
    try:
        # First call primes the `_m_h5_tk` cookie (MTOP answers TOKEN_EMPTY).
        await context.request.get(
            url, params={**params, "t": str(int(time.time() * 1000)), "sign": ""}
        )
        token = await _h5_token(context)
        if not token:
            return None
        timestamp = str(int(time.time() * 1000))
        response = await context.request.get(
            url,
            params={
                **params,
                "t": timestamp,
                "sign": _h5_sign(token, timestamp, payload_data),
            },
        )
        return await response.json()
    except Exception:
        logger.debug("DS Center item lookup failed for %s", item_id, exc_info=True)
        return None


def _classify_ds_item(payload: Optional[dict]) -> Optional[bool]:
    """True = the DS Center lists the item, False = it does not, None = unknown.

    Raises `DsCenterSessionExpiredError` when MTOP reports the session is
    expired, challenged, or unauthorized (the operator must log in again).
    """
    if not isinstance(payload, dict):
        return None
    for flag in payload.get("ret") or []:
        if any(failure in str(flag) for failure in _DS_SESSION_FAILURES):
            raise DsCenterSessionExpiredError(_SESSION_EXPIRED_MESSAGE)
    body = payload.get("data")
    if not isinstance(body, dict):
        return None
    if str(body.get("code") or "") in _DS_ABSENT_CODES:
        return False
    if str(body.get("message") or "") in _DS_ABSENT_MESSAGES:
        return False
    record = body.get("data")
    if isinstance(record, dict) and record.get("itemId"):
        return True
    return None


def _is_login_wall(url: str) -> bool:
    """Whether a DS Center navigation landed on the login page."""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _LOGIN_URL_MARKERS)


def _ds_country_cookie(country: str) -> dict:
    """AliExpress ship-to cookie: the DS Center judges the item for this market.

    The DS Center resolves the item for the market in this cookie, and its
    verdict is market-specific — an item it lists for AU can be
    `none_of_item` for another region — so the gate must ask for the run's
    target country rather than the DS Center's default.
    """
    return {
        "name": _DS_COUNTRY_COOKIE,
        "value": f"site=glo&region={country}&b_locale=en_US",
        "domain": ".aliexpress.com",
        "path": "/",
    }


async def _ds_center_verdict(context, page, product_url: str) -> Optional[bool]:
    """DS Center verdict for one candidate: True, False, or None (unknown).

    Navigates the authenticated page to the DS Center Product Analysis entry
    for the item, then resolves the item's DS Center record. A login wall —
    an automatic redirect, or a password field where the analysis should
    be — means the saved session is gone and is surfaced as
    `DsCenterSessionExpiredError`.
    """
    item_id = _ds_item_id(product_url)
    if not item_id:
        return None
    await page.goto(
        f"{_DS_ANALYSIS_URL}?itemId={item_id}",
        timeout=_NAV_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )
    if _is_login_wall(page.url):
        raise DsCenterSessionExpiredError(_SESSION_EXPIRED_MESSAGE)
    verdict = _classify_ds_item(await _ds_item_lookup(context, item_id))
    if verdict is not None:
        return verdict
    # The internal XHR gave no verdict — read the page the DS Center rendered
    # the analysis into instead.
    try:
        dom = await page.evaluate(_DS_DOM_PROBE_JS)
    except Exception:
        logger.debug("DS Center DOM probe failed for %s", item_id, exc_info=True)
        return None
    if not isinstance(dom, dict):
        return None
    if dom.get("loginWall"):
        raise DsCenterSessionExpiredError(_SESSION_EXPIRED_MESSAGE)
    if dom.get("reject"):
        return False
    if int(dom.get("rows") or 0) > 0:
        return True
    return None


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
        # The DS Center gate judges each item for the run's target market.
        self._country = country

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
        # Thin candidates need the PDP pass for their gallery; with the DS
        # Center gate on, every candidate must pass the gate first, so the
        # whole batch goes through it.
        verified = candidates if self._ds_gate_enabled() else thin
        if verified:
            try:
                await self._harvest_galleries(verified)
            except DsCenterSessionExpiredError:
                # Session gone: the operator has to re-run the session
                # script, so surface it instead of continuing unverified.
                raise
            except Exception:
                logger.exception(
                    "aliexpress PDP gallery harvest crashed; keeping any "
                    "galleries captured so far"
                )

        rejected = sum(1 for c in candidates if c.get("_ds_center_rejected"))
        if rejected:
            candidates = [
                c for c in candidates if not c.get("_ds_center_rejected")
            ]
            logger.info(
                "DS Center gate dropped %d of %d verified candidate(s)",
                rejected, len(verified),
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

    def _ds_gate_enabled(self) -> bool:
        """Whether the DS Center gate applies to this run (config, not the class)."""
        return bool(settings.ENABLE_DS_CENTER_GATE)

    async def _harvest_galleries(self, candidates: List[dict]) -> None:
        """Verify candidates in the DS Center (when enabled) and upgrade the
        thin ones in place from their own PDPs.

        One headless Chromium session for the batch; each page is closed
        immediately after harvesting. Description is read from the page's
        meta description while the gallery probe runs.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        gate = self._ds_gate_enabled()
        context_kwargs = {
            "user_agent": settings.USER_AGENT,
            "locale": "en-AU",
            "viewport": {"width": 1366, "height": 900},
        }
        if gate:
            state_path = Path(settings.ALI_DS_STATE_PATH)
            if not state_path.exists():
                raise DsCenterSessionExpiredError(_MISSING_STATE_MESSAGE)
            context_kwargs["storage_state"] = str(state_path)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(**context_kwargs)
            await Stealth(
                navigator_user_agent_override=settings.USER_AGENT
            ).apply_stealth_async(context)
            if gate:
                await context.add_cookies(
                    [
                        _ds_country_cookie(
                            getattr(self, "_country", None) or settings.TARGET_COUNTRY
                        )
                    ]
                )
            try:
                for candidate in candidates:
                    page = await context.new_page()
                    try:
                        if gate:
                            item_id = _ds_item_id(candidate["url"])
                            verdict = await _ds_center_verdict(
                                context, page, candidate["url"]
                            )
                            if verdict is not True:
                                candidate["_ds_center_rejected"] = True
                                logger.warning(
                                    "Skipping %s: %s",
                                    item_id,
                                    "Not supported in DS Center"
                                    if verdict is False
                                    else "DS Center gate inconclusive",
                                )
                                continue
                            logger.info(
                                "DS Center gate: kept %s (listed for %s)",
                                item_id,
                                getattr(self, "_country", None)
                                or settings.TARGET_COUNTRY,
                            )
                            if len(candidate["images"]) >= _MIN_GALLERY:
                                # Already gallery-complete: nothing to harvest.
                                continue
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