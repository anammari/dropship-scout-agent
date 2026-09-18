"""Etsy Open API v3 extractor (plan F.3.2).

Supplier-First ingestion: queries the official
`https://openapi.etsy.com/v3/application/listings/active` endpoint for
active listings matching the target keywords and maps them directly into
`RawSupplierProduct`s (listing URL, listed price, listing images). Only
fully-formed candidates (>= 3 distinct gallery URLs, USD-listed price)
are emitted; anything thinner is skipped, never fabricated.

Etsy prices are quoted in USD by default; they are converted to AUD with
`settings.USD_TO_AUD`. Etsy does not return shipping costs on this
endpoint, so `shipping_cost_aud` is 0.0 (the unquoted-shipping caveat is
stated in the COGS basis downstream).

Credentials: `ETSY_API_KEY` in `.env` — the Etsy Open API v3 personal
access token (`keystring:secret` form also works as a bearer token).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from src.config import settings
from src.extractors.base import (
    BaseSupplierExtractor,
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
    ExtractorTimeoutException,
)
from src.models import RawSupplierProduct

logger = logging.getLogger(__name__)

_ETSY_API_BASE = "https://openapi.etsy.com/v3"
_LISTINGS_ACTIVE_URL = f"{_ETSY_API_BASE}/application/listings/active"

_SUPPLIER_NAME = "Etsy"

# Gallery image fields on an Etsy listing image resource, best resolution
# first (url_fullxfull is the largest the API serves).
_IMAGE_URL_KEYS = ("url_fullxfull", "url_570xN", "url_570x100", "url_170x135")

_MAX_DESCRIPTION_CHARS = 4000


class EtsyApiExtractor(BaseSupplierExtractor):
    """Fetches active Etsy listings through the official Open API v3."""

    engine_name = "etsy_api"
    supplier_name = _SUPPLIER_NAME

    def __init__(
        self,
        api_key: Optional[str] = None,
        http: Optional[httpx.AsyncClient] = None,
        max_per_keyword: int = 20,
    ) -> None:
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None
        self._max_per_keyword = max_per_keyword

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def fetch_products(
        self, keywords: List[str], country: str = "AU"
    ) -> List[RawSupplierProduct]:
        api_key = self._api_key or settings.ETSY_API_KEY
        if not api_key:
            raise ExtractorNotConfiguredError("ETSY_API_KEY")

        products: List[RawSupplierProduct] = []
        for keyword in keywords:
            listings = await self._query_listings(api_key, keyword)
            for listing in listings:
                try:
                    products.append(self._build_product(listing))
                except Exception:
                    logger.debug(
                        "etsy listing skipped (did not satisfy "
                        "RawSupplierProduct): listing_id=%r",
                        listing.get("listing_id"),
                        exc_info=True,
                    )
        return products

    async def _query_listings(self, api_key: str, keyword: str) -> list:
        """One /listings/active call; hard failures raise, soft ones log."""
        try:
            response = await self._http.get(
                _LISTINGS_ACTIVE_URL,
                params={
                    "keywords": keyword,
                    "limit": str(self._max_per_keyword),
                    # Ask for the listing images inline so no per-listing
                    # follow-up request is needed for the gallery.
                    "includes": "Images",
                },
                headers={"x-api-key": api_key},
            )
        except httpx.TimeoutException as exc:
            raise ExtractorTimeoutException(f"Etsy API timed out: {exc}")
        except httpx.HTTPError as exc:
            raise ExtractorBlockedException(f"Etsy API transport failure: {exc}")
        if response.status_code in (401, 403):
            raise ExtractorBlockedException(
                f"Etsy API rejected credentials (HTTP {response.status_code})"
            )
        if response.status_code == 429:
            raise ExtractorBlockedException("Etsy API rate-limited (HTTP 429)")
        if response.status_code != 200:
            logger.warning(
                "etsy listings query failed keyword=%r: HTTP %d — skipping keyword",
                keyword, response.status_code,
            )
            return []
        try:
            body = response.json()
        except ValueError:
            logger.warning("Etsy API returned a non-JSON body — skipping keyword")
            return []
        results = body.get("results")
        return results if isinstance(results, list) else []

    @staticmethod
    def _coerce_price_usd(raw) -> Optional[float]:
        """Etsy `price` arrives as a string ("12.50"); None on junk."""
        if raw is None or raw == "":
            return None
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _build_product(self, listing: dict) -> RawSupplierProduct:
        listing_id = str(listing.get("listing_id") or "").strip()
        if not listing_id:
            raise ValueError("etsy listing has no listing_id")
        title = str(listing.get("title") or "").strip()
        if not title:
            raise ValueError(f"etsy listing {listing_id} has no title")
        currency = str(listing.get("currency_code") or "USD").upper()
        if currency != "USD":
            raise ValueError(
                f"etsy listing {listing_id} priced in {currency}; only USD "
                "listings are converted (currency mispricing guard)"
            )
        price_usd = self._coerce_price_usd(listing.get("price"))
        if price_usd is None:
            raise ValueError(f"etsy listing {listing_id} has no usable price")

        gallery: List[str] = []
        seen: set = set()
        images = listing.get("images")
        if isinstance(images, list):
            for image in images:
                if not isinstance(image, dict):
                    continue
                for key in _IMAGE_URL_KEYS:
                    url = image.get(key)
                    if isinstance(url, str) and url.startswith("http"):
                        if url not in seen:
                            seen.add(url)
                            gallery.append(url)
                        break
        if len(gallery) < 3:
            raise ValueError(
                f"etsy listing {listing_id} gallery has only {len(gallery)} image(s)"
            )
        description = str(listing.get("description") or "").strip()[:_MAX_DESCRIPTION_CHARS]
        return RawSupplierProduct(
            supplier_name=_SUPPLIER_NAME,
            supplier_retail_url=(
                listing.get("url") or f"https://www.etsy.com/listing/{listing_id}/"
            ),
            product_title=title,
            product_description=description or title,
            price_aud=round(price_usd * settings.USD_TO_AUD, 2),
            shipping_cost_aud=0.0,
            image_urls=gallery,
        )