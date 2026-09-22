"""AliExpress ingestion from the Dropshipping Center itself (native).

Replaces the retired Apify actor path (plans/AliExpress Dropshipping
Center Gating.md, phases 1-4). Instead of scraping unauthenticated
consumer search pages — which quote loss-leader "welcome deal" prices,
ignore the AU ship-to context, and bill per result — this extractor reads
the DS Center's own MTOP search stream and its per-item record:

    selection.search          -> the DS Center's ranked catalogue search
    selection.queryByItemUrl  -> one item's record (price, orders, rating)

Both are plain MTOP H5 calls made through the Playwright context's request
jar, so the same browser context that would harvest a PDP also carries the
ship-to cookie that decides which market the prices are quoted for. That
market pin is load-bearing: the same item is quoted `US $5.23` for the US
default and `US $7.22` for AU, and it is what makes `price_aud` a real
landed dropshipping cost rather than a consumer welcome price.

Winning-product gate (phase 3): every search hit is expanded through its
item record, then dropped unless it clears BOTH quantitative floors —
`MIN_DS_ORDER_COUNT` historical orders and `MIN_DS_RATING` out of 5.
Orders arrive as a display string ("148 sold", "10000+ sold") and rating
as a string ("4.2"); a metric the DS Center does not report is treated as
unproven and the item is dropped, matching the inverted tolerance the CJ
liveness gate uses. Survivors are sorted by order volume descending so the
target count fills with the strongest sellers first.

Imagery: the DS Center record carries a single `itemMainPic`, so each
survivor's own PDP is harvested for its full gallery and meta description
through the same headless Chromium pass the retired path used.

Zero LLM involvement. Prices are USD-quoted by the DS Center for AU
(`currencyCode` is checked; anything that is neither USD nor AUD is
skipped rather than mispriced), and become AUD via `settings.USD_TO_AUD`.
No shipping cost is quoted here, so `shipping_cost_aud` is 0.0 (the
unquoted-shipping caveat is stated in the COGS basis downstream).

The saved session is optional: the DS Center answers these calls
anonymously, so `ALI_DS_STATE_PATH` is injected only when the file exists
and its absence is never an error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

from src.config import settings
from src.extractors.base import BaseSupplierExtractor, ExtractorBlockedException
from src.models import RawSupplierProduct
from src.pipeline.image_sourcing import strip_size_suffix

logger = logging.getLogger(__name__)

_SUPPLIER_NAME = "AliExpress"

# AliExpress direct product page shape — every emitted supplier_retail_url
# must match (mirrors models._SUPPLIER_PDP_PATTERNS; defense in depth).
_PDP_PATTERN = re.compile(r"/item/\d+\.html")
_PDP_ID_PATTERN = re.compile(r"/item/(\d+)\.html")
_PRICE_NUMBER_RE = re.compile(r"[\d.]+")
# "148 sold" / "10000+ sold" / "1,234 sold" -> the leading order count.
_ORDERS_RE = re.compile(r"([\d,]+)\s*\+?\s*sold", re.IGNORECASE)
_RATING_RE = re.compile(r"(\d+(?:\.\d+)?)")

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

# --- DS Center MTOP endpoints -------------------------------------------
# The DS Center is a React SPA over Alibaba's MTOP gateway; these are the
# same H5 APIs its own UI calls, reached with the token-then-sign handshake.
_DS_ORIGIN = "https://acs.aliexpress.com"
_DS_SEARCH_API = "mtop.aidc.ds.center.selection.search"
_DS_SEARCH_VERSION = "2.0"
_DS_ITEM_API = "mtop.aidc.ds.center.selection.queryByItemUrl"
_DS_ITEM_VERSION = "1.0"
# Extra params the SPA sends with the search call (the catalogue search is
# served by a newer gateway revision than the item record).
_DS_SEARCH_EXTRA = {"SV": "5.0", "preventFallback": "true"}
_DS_APP_KEY = "12574478"
_DS_JSV = "2.7.2"
# Server-side ranking: the directive's "sort by performance" needs no
# client-side reordering of the search page, only the gate's own ordering.
_DS_SEARCH_SORT = "ORDERS_DESC"
_DS_COUNTRY_COOKIE = "aep_usuc_f"
# MTOP answered "token expired" (priming did not take) — worth one retry.
_DS_TOKEN_FLAGS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY")
# The DS Center is refusing this client outright (or the saved session is
# gone): surface it rather than silently returning an empty funnel.
_DS_SESSION_FAILURES = (
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_ILLEGAL_ACCESS",
)
_LOGIN_URL_MARKERS = ("login.aliexpress.com", "/login.")
# MTOP answers `type=jsonp` by wrapping the JSON in `mtopjsonpN(...)`.
_JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*$", re.S)

def _session_expired_message() -> str:
    """Operator-facing cause + recovery, with the configured state path."""
    return (
        "The AliExpress Dropshipping Center refused the request (session "
        "expired, challenged, or blocked). The saved session is optional but "
        "was used for this run: refresh it with "
        "`python scripts/generate_ali_session.py`, or remove "
        f"{settings.ALI_DS_STATE_PATH!r} to run anonymously."
    )


class DsCenterSessionExpiredError(RuntimeError):
    """The DS Center refused the client: session gone, challenged, or blocked."""


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


def _ds_item_id(product_url: str) -> Optional[str]:
    """The numeric item id from an AliExpress PDP URL, or None."""
    match = _PDP_ID_PATTERN.search(product_url or "")
    return match.group(1) if match else None


def _canonical_product_url(url: str) -> str:
    """Reduce a DS Center `itemUrl` to the bare canonical PDP link.

    The payload quotes `https://aliexpress.com/item/<id>.html`; the
    exported fulfilment link is the canonical `www.` form.
    """
    match = _PDP_PATTERN.search(url or "")
    if not match:
        return url
    return f"https://www.aliexpress.com{match.group(0)}"


def _parse_orders(trade_desc) -> Optional[int]:
    """Historical order count from the DS Center's `tradeDesc`, or None.

    The DS Center reports sales as display text ("148 sold", "10000+
    sold", "1,234 sold") and as an empty string when it has no figure for
    the item. A floored figure ("10000+ sold") is read at its floor — the
    gate only ever compares it against a minimum.
    """
    if not trade_desc:
        return None
    match = _ORDERS_RE.search(str(trade_desc))
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _parse_rating(score) -> Optional[float]:
    """The item's star rating from the DS Center's `score`, or None.

    `score` is empty for items the DS Center has no rating for.
    """
    if score is None or str(score).strip() == "":
        return None
    match = _RATING_RE.search(str(score))
    if not match:
        return None
    value = float(match.group(1))
    return value if value > 0 else None


def _decode_payload(text: Optional[str]) -> Optional[dict]:
    """Decode an MTOP response body, JSONP-wrapped or not."""
    if not text:
        return None
    body = text.strip()
    match = _JSONP_RE.match(body)
    if match:
        body = match.group(1)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        logger.debug("MTOP response was not JSON: %r", text[:200])
        return None
    return payload if isinstance(payload, dict) else None


def _session_failure(payload: Optional[dict]) -> Optional[str]:
    """The session/auth `ret` flag MTOP reported, if any."""
    for flag in (payload or {}).get("ret") or []:
        for failure in _DS_SESSION_FAILURES:
            if failure in str(flag):
                return failure
    return None


def _token_priming_failure(payload: Optional[dict]) -> bool:
    """Whether the exchange failed only because the token was not yet primed."""
    for flag in (payload or {}).get("ret") or []:
        if any(marker in str(flag) for marker in _DS_TOKEN_FLAGS):
            return True
    return False


def _coerce_price(raw) -> Optional[float]:
    """A price field that may be a number or a "US $5.32" string."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    match = _PRICE_NUMBER_RE.search(str(raw).replace(",", ""))
    if not match:
        return None
    value = float(match.group(0))
    return value if value > 0 else None


def _price_aud(record: dict) -> Optional[float]:
    """The item's listed dropshipping price in AUD, or None when unusable.

    The record carries the exact figure in `originMinPriceFormatJson`
    (minor units, plus the currency it was quoted in) and the same figure
    formatted for display. The currency guard mirrors the retired path: the
    AUD conversion assumes USD, so anything else is skipped rather than
    mispriced — an AUD quote is taken as-is.
    """
    structure = (record.get("originMinPriceFormatJson") or {}).get("structure")
    currency = ""
    amount: Optional[float] = None
    if isinstance(structure, dict):
        currency = str(structure.get("currencyCode") or "").strip().upper()
        cent = structure.get("cent")
        if cent is not None:
            try:
                amount = float(cent) / 100.0
            except (TypeError, ValueError):
                amount = None
    if not amount or amount <= 0:
        amount = _coerce_price(
            record.get("discountMinPriceFormat") or record.get("rangePriceFormat")
        )
    if amount is None:
        return None
    # The DS Center quotes USD for the AU market; an unlabelled figure is
    # taken as USD (its documented behaviour), anything else is skipped.
    if currency in ("", "USD"):
        return round(amount * settings.USD_TO_AUD, 2)
    if currency == "AUD":
        return round(amount, 2)
    logger.debug("DS Center item skipped (currency=%s)", currency)
    return None


# ----------------------------------------------------------------------
# MTOP transport
# ----------------------------------------------------------------------


def _h5_sign(token: str, timestamp: str, data: str) -> str:
    """MTOP H5 request signature: md5(token&t&appKey&data)."""
    digest = hashlib.md5(
        f"{token}&{timestamp}&{_DS_APP_KEY}&{data}".encode("utf-8")
    )
    return digest.hexdigest()


async def _h5_token(context) -> Optional[str]:
    """The `_m_h5_tk` token (pre-underscore half) from the context's cookies."""
    for cookie in await context.cookies(_DS_ORIGIN):
        if cookie.get("name") == "_m_h5_tk":
            token = str(cookie.get("value") or "").split("_")[0]
            return token or None
    return None


async def _mtop_call(
    context, api: str, version: str, data: dict, extra: Optional[dict] = None
) -> Optional[dict]:
    """One MTOP H5 call: prime the token, then issue the signed request.

    MTOP rejects a signed request made without a primed `_m_h5_tk` cookie
    (it answers `TOKEN_EMPTY`), so the first call is sent unsigned purely to
    establish it. Returns the decoded payload, or None when the exchange
    yielded nothing readable.
    """
    url = f"{_DS_ORIGIN}/h5/{api}/{version}/"
    payload = json.dumps(data, separators=(",", ":"))
    params = {
        "jsv": _DS_JSV,
        "appKey": _DS_APP_KEY,
        "api": api,
        "v": version,
        "type": "originaljson",
        "dataType": "json",
        "data": payload,
    }
    if extra:
        params.update(extra)
    try:
        priming = await context.request.get(
            url, params={**params, "t": str(int(time.time() * 1000)), "sign": ""}
        )
        token = await _h5_token(context)
        if not token:
            if _token_priming_failure(_decode_payload(await priming.text())):
                logger.debug("MTOP token was never issued for %s", api)
                return None
        for attempt in (1, 2):
            if not token:
                token = await _h5_token(context)
                if not token:
                    return None
            timestamp = str(int(time.time() * 1000))
            response = await context.request.get(
                url,
                params={
                    **params,
                    "t": timestamp,
                    "sign": _h5_sign(token, timestamp, payload),
                },
            )
            decoded = _decode_payload(await response.text())
            if attempt == 1 and _token_priming_failure(decoded):
                # The token rotated between priming and signing; retry once.
                token = None
                continue
            return decoded
        return None
    except Exception:
        logger.debug("MTOP call failed: %s", api, exc_info=True)
        return None


async def _ds_search(context, keyword: str, page_size: int) -> List[dict]:
    """The DS Center's catalogue search for one keyword, best sellers first."""
    payload = await _mtop_call(
        context,
        _DS_SEARCH_API,
        _DS_SEARCH_VERSION,
        {
            "searchText": keyword,
            "categoryId": "",
            "shipFrom": "",
            "isCouponCodeRequired": False,
            "isFreeShippingRequired": False,
            "isOverseasWarehouseRequired": False,
            "isVideoRequired": False,
            "sort": _DS_SEARCH_SORT,
            "specialOffer": "",
            "minPrice": "",
            "maxPrice": "",
            "searchedImage": "",
            "imageUrl": "",
            "pageSize": page_size,
            "pageNum": 1,
        },
        extra=_DS_SEARCH_EXTRA,
    )
    failure = _session_failure(payload)
    if failure:
        raise DsCenterSessionExpiredError(
            f"{_session_expired_message()} ({failure})"
        )
    if payload is None or not str((payload.get("ret") or [""])[0]).startswith(
        "SUCCESS"
    ):
        # Not an empty result set — the exchange itself failed, so the
        # chain should fall through rather than report an empty funnel.
        raise ExtractorBlockedException(
            f"DS Center search failed for keyword={keyword!r}: "
            f"{(payload or {}).get('ret')}"
        )
    body = (payload.get("data") or {}).get("data")
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        logger.warning("DS Center search payload had no item list for %r", keyword)
        return []
    return [item for item in items if isinstance(item, dict)]


async def _ds_item_record(context, item_url: str) -> Optional[dict]:
    """One item's DS Center record (price, orders, rating), or None."""
    payload = await _mtop_call(
        context, _DS_ITEM_API, _DS_ITEM_VERSION, {"itemUrl": item_url}
    )
    failure = _session_failure(payload)
    if failure:
        raise DsCenterSessionExpiredError(
            f"{_session_expired_message()} ({failure})"
        )
    record = ((payload or {}).get("data") or {}).get("data")
    return record if isinstance(record, dict) else None


def _ds_country_cookie(country: str) -> dict:
    """AliExpress ship-to cookie: the market the DS Center quotes for.

    Load-bearing, not cosmetic: without it the DS Center answers for its
    own default market and quotes a different (cheaper, wrong) price and a
    different catalogue — the same item is `US $5.23` for the US default
    and `US $7.22` for AU.
    """
    return {
        "name": _DS_COUNTRY_COOKIE,
        "value": f"site=glo&region={country}&b_locale=en_US",
        "domain": ".aliexpress.com",
        "path": "/",
    }


def _is_login_wall(url: str) -> bool:
    """Whether a DS Center navigation landed on the login page."""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _LOGIN_URL_MARKERS)


class AliExpressDsCenterExtractor(BaseSupplierExtractor):
    """Ingests AliExpress candidates from the Dropshipping Center search."""

    engine_name = "aliexpress_ds_center"
    supplier_name = _SUPPLIER_NAME

    def __init__(
        self,
        max_products: Optional[int] = None,
        headless: Optional[bool] = None,
    ) -> None:
        self._max_products = max_products or settings.ALI_DS_MAX_PRODUCTS
        self._headless = True if headless is None else headless
        # Overridden per run by `fetch_products`; the default keeps the
        # market pin correct if a context is opened outside a run.
        self._country = settings.TARGET_COUNTRY

    async def fetch_products(
        self, keywords: List[str], country: str = "AU"
    ) -> List[RawSupplierProduct]:
        """Search the DS Center per keyword, gate, then harvest galleries.

        Returns only candidates that cleared the winning-product gate and
        whose PDP yielded a >= 3-image gallery — anything else is skipped,
        never fabricated.
        """
        self._country = country
        candidates: List[dict] = []
        seen_ids: set = set()

        async with self._browser_context() as (context, _landing_page):
            for keyword in keywords:
                for hit in await _ds_search(context, keyword, self._max_products):
                    item_id = str(hit.get("itemId") or "").strip()
                    item_url = _canonical_product_url(str(hit.get("itemUrl") or ""))
                    if not item_id or not _PDP_PATTERN.search(item_url):
                        continue
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    record = await _ds_item_record(context, item_url)
                    if record is None:
                        logger.warning(
                            "Skipping %s: no DS Center record available", item_id
                        )
                        continue
                    if not self._passes_winner_gate(item_id, record):
                        continue
                    price_aud = _price_aud(record)
                    if price_aud is None:
                        logger.warning(
                            "Skipping %s: no usable price in the DS Center record",
                            item_id,
                        )
                        continue
                    title = str(
                        record.get("itemName") or hit.get("itemName") or ""
                    ).strip()
                    if not title:
                        continue
                    main_pic = str(
                        record.get("itemMainPic") or hit.get("itemMainPic") or ""
                    ).strip()
                    candidates.append(
                        {
                            "url": item_url,
                            "title": title,
                            "price_aud": price_aud,
                            "orders": _parse_orders(record.get("tradeDesc")) or 0,
                            "rating": _parse_rating(record.get("score")),
                            "images": [main_pic] if main_pic else [],
                        }
                    )

            if not candidates:
                logger.warning("DS Center ingestion yielded no viable candidates")
                return []

            # Strongest sellers first, so the target count fills with the best
            # proven products rather than whatever the search page happened to
            # rank first.
            candidates.sort(key=lambda c: c["orders"], reverse=True)
            logger.info(
                "DS Center gate passed %d candidate(s) for %s (orders %s)",
                len(candidates),
                country,
                [c["orders"] for c in candidates[:10]],
            )
            await self._harvest_galleries(context, candidates)

        products: List[RawSupplierProduct] = []
        for candidate in candidates:
            try:
                products.append(self._build_product(candidate))
            except Exception:
                logger.debug(
                    "aliexpress candidate skipped: %r",
                    candidate["url"],
                    exc_info=True,
                )
        return products

    # --- internals -----------------------------------------------------

    def _passes_winner_gate(self, item_id: str, record: dict) -> bool:
        """The quantitative "winning product" gate (directive phase 3).

        Both floors must be cleared. A metric the DS Center does not report
        is unproven, so the item is dropped — the same inverted tolerance
        the CJ liveness gate applies to unverifiable stock.
        """
        orders = _parse_orders(record.get("tradeDesc"))
        rating = _parse_rating(record.get("score"))
        if orders is None:
            logger.warning(
                "Skipping %s: Insufficient order volume (unavailable)", item_id
            )
            return False
        if orders < settings.MIN_DS_ORDER_COUNT:
            logger.warning(
                "Skipping %s: Insufficient order volume (%d)", item_id, orders
            )
            return False
        if rating is None:
            logger.warning("Skipping %s: Rating unavailable", item_id)
            return False
        if rating < settings.MIN_DS_RATING:
            logger.warning(
                "Skipping %s: Rating too low (%s)", item_id, rating
            )
            return False
        return True

    def _browser_context(self):
        """A stealth Chromium context carrying the target market's cookie.

        The saved session is injected only when it exists: the DS Center
        answers these calls anonymously, so a missing state file is not an
        error and the run simply continues without it.
        """
        return _DsBrowserContext(self._headless, self._country)

    async def _harvest_galleries(
        self, context, candidates: List[dict]
    ) -> None:
        """Upgrade each candidate in place from its own PDP.

        The DS Center record carries a single image, so every candidate
        needs the PDP pass for its gallery; the page's meta description is
        read while the probe runs. One page is used at a time and closed
        immediately.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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
                    logger.debug("PDP load timed out: %s", candidate["url"])
                    continue
                result = await page.evaluate(_GALLERY_JS)
                urls = (
                    [u for u in result if isinstance(u, str) and u]
                    if isinstance(result, list)
                    else []
                )
                # Upgrade suffixed assets: original first, as-served URL as
                # fallback ordering (same rule the strict image protocol
                # used for alicdn).
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
            price_aud=candidate["price_aud"],
            shipping_cost_aud=0.0,
            image_urls=images,
        )


class _DsBrowserContext:
    """Async context manager yielding one stealth Chromium (context, page).

    Opening the DS Center landing page first is what establishes the
    browser-session cookies the MTOP calls ride on, and it is also how a
    login wall on a supplied session is detected.
    """

    def __init__(self, headless: bool, country: str) -> None:
        self._headless = headless
        self._country = country

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        self._playwright = await async_playwright().start()
        browser = None
        try:
            browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context_kwargs = {
                "user_agent": settings.USER_AGENT,
                "locale": "en-AU",
                "viewport": {"width": 1366, "height": 900},
            }
            state_path = Path(settings.ALI_DS_STATE_PATH)
            if state_path.exists():
                context_kwargs["storage_state"] = str(state_path)
            context = await browser.new_context(**context_kwargs)
            await Stealth(
                navigator_user_agent_override=settings.USER_AGENT
            ).apply_stealth_async(context)
            await context.add_cookies([_ds_country_cookie(self._country)])
            page = await context.new_page()
            try:
                await page.goto(
                    "https://ds.aliexpress.com/find-products",
                    timeout=_NAV_TIMEOUT_MS,
                    wait_until="domcontentloaded",
                )
            except Exception:
                logger.debug("DS Center landing page did not load", exc_info=True)
            if _is_login_wall(page.url):
                raise DsCenterSessionExpiredError(_session_expired_message())
        except Exception:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    logger.debug("browser close failed", exc_info=True)
            await self._playwright.stop()
            raise
        self._browser = browser
        self._context = context
        self._page = page
        return context, page

    async def __aexit__(self, *exc_info):
        try:
            await self._browser.close()
        except Exception:
            logger.debug("browser close failed", exc_info=True)
        await self._playwright.stop()
        return False
