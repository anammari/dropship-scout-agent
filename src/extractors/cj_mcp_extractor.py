"""CJdropshipping MCP extractor (plan §4.2) — the primary engine.

Supplier-First ingestion over the official CJdropshipping **MCP server**
instead of the retired REST API. The MCP path bypasses the Cloudflare
Turnstile wall that blocks every automated frontend read of a CJ product
page, so no HTML is ever fetched and no bot challenge is ever answered —
the structured tool output is the ground truth.

Per keyword the extractor:

1. runs the discovered product-search tool with the target keyword, the
   China warehouse filter, and an inventory-available filter (result cap:
   `settings.CJ_MAX_PRODUCTS_PER_KEYWORD`);
2. expands each shortlisted hit through the product-detail tool to obtain
   pricing, variants, the full `productImageSet` gallery, and the
   description;
3. puts the merged payload through the **MCP Payload Liveness Gate**
   (`is_mcp_payload_live`) — a delisted, out-of-stock, or
   ambiguous-liveness payload is dropped immediately and logged at INFO;
4. maps the survivors to `RawSupplierProduct` with the canonical
   `https://cjdropshipping.com/product/{pid}.html` PDP URL, the listed USD
   price converted by `settings.USD_TO_AUD`, an HTML-stripped description,
   and >= 3 distinct CDN gallery URLs.

Hits that cannot yield a fully-formed product are skipped, never
fabricated — the same skip-don't-invent rule every extractor follows.

Chain exception mapping (plan §4.2): `CjMcpNotConfiguredError` ->
`ExtractorNotConfiguredError("CJ_MCP_TOKEN")`; `CjMcpConnectionError` /
`CjMcpToolError` -> `ExtractorBlockedException`, so the orchestrator's
chain falls through to AliExpress/Etsy rather than retrying a dead
endpoint.

Zero LLM involvement — every field is tool-returned data for the exact SKU
listing.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from src.config import settings
from src.extractors.base import (
    BaseSupplierExtractor,
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
)
from src.models import RawSupplierProduct
from src.pipeline.cj_mcp_client import (
    DEFAULT_WAREHOUSE_COUNTRY,
    CjMcpClient,
    CjMcpConnectionError,
    CjMcpNotConfiguredError,
    CjMcpToolError,
    extract_description,
    extract_gallery,
    extract_pid,
    extract_price_usd,
    extract_product_url,
    extract_title,
    is_mcp_payload_live,
    merge_product_payloads,
    product_page_url,
)

logger = logging.getLogger(__name__)

_SUPPLIER_NAME = "CJdropshipping"

# The pipeline's hard gallery floor; a thinner product is dropped here so it
# never reaches the image-sourcing stage.
_MIN_GALLERY_IMAGES = 3

_CJ_MCP_CLIENT: Optional[CjMcpClient] = None


def get_cj_mcp_client() -> CjMcpClient:
    """Lazily build the run-scoped CJ MCP client from settings."""
    global _CJ_MCP_CLIENT
    if _CJ_MCP_CLIENT is None:
        _CJ_MCP_CLIENT = CjMcpClient()
    return _CJ_MCP_CLIENT


def reset_cj_mcp_client() -> None:
    """Drop the process-global client singleton (test isolation)."""
    global _CJ_MCP_CLIENT
    _CJ_MCP_CLIENT = None


class CjMcpExtractor(BaseSupplierExtractor):
    """Fetches active CJdropshipping listings through the official MCP server."""

    engine_name = "cjdropshipping"
    supplier_name = _SUPPLIER_NAME

    def __init__(
        self,
        client: Optional[Any] = None,
        max_products: Optional[int] = None,
    ) -> None:
        # An injected client (tests, alternate wiring) skips the singleton.
        self._client = client
        self._max_products = max_products or settings.CJ_MAX_PRODUCTS_PER_KEYWORD

    async def fetch_products(
        self, keywords: List[str], country: str = "AU"
    ) -> List[RawSupplierProduct]:
        client = self._client if self._client is not None else get_cj_mcp_client()
        if not client.configured:
            raise ExtractorNotConfiguredError("CJ_MCP_TOKEN")

        products: List[RawSupplierProduct] = []
        try:
            # One MCP session for the whole run: connect once, run every
            # keyword/tool call over it, close on exit.
            async with client:
                for keyword in keywords:
                    hits = await self._search(client, keyword, country)
                    for hit in hits:
                        try:
                            products.append(
                                await self._build_product(hit, client, country)
                            )
                        except Exception as exc:
                            # Skip, never fabricate — and say why at INFO so
                            # a run's drops are diagnosable without a debug
                            # build (plan §5.2: dropped strictly, logged with
                            # pid + reason, never emitted).
                            logger.info(
                                "cj_mcp hit DROPPED: pid=%s title=%r reason=%s",
                                extract_pid(hit) if isinstance(hit, dict) else None,
                                extract_title(hit) if isinstance(hit, dict) else "",
                                exc,
                            )
                            logger.debug(
                                "cj_mcp hit drop detail", exc_info=True
                            )
        except CjMcpNotConfiguredError as exc:
            raise ExtractorNotConfiguredError("CJ_MCP_TOKEN") from exc
        except (CjMcpConnectionError, CjMcpToolError) as exc:
            raise ExtractorBlockedException(
                f"CJ MCP endpoint unavailable: {exc}"
            ) from exc
        return products

    async def _search(self, client: Any, keyword: str, country: str) -> List[dict]:
        """One product-search tool call for a keyword.

        Parameters are pinned to CJ's live search schema: `countryCode` on
        that tool selects the WAREHOUSE country backing the listing (not a
        shipping destination — passing the AU destination returns almost
        nothing), `isWarehouse=true` is the documented "China warehouse"
        intent mapping, and `startWarehouseInventory=1` is the
        inventory-available filter, so only stocked listings come back.
        """
        return await client.search_products(
            keyword,
            limit=self._max_products,
            warehouse_country=DEFAULT_WAREHOUSE_COUNTRY,
            global_warehouse=True,
            inventory_available=True,
        )

    async def _build_product(
        self, hit: dict, client: Any, country: str
    ) -> RawSupplierProduct:
        """Expand one search hit into a fully-formed RawSupplierProduct.

        The search hit alone carries status and warehouse stock but only a
        single thumbnail, so the detail call is mandatory: it supplies
        pricing, variants, the full gallery, and the description. The
        merged payload is what the liveness gate finally judges. An empty
        or failed detail call drops the candidate on the spot — the MCP
        stream is the source of truth, so "we couldn't tell" is not an
        acceptable answer from it (plan §5.2).
        """
        pid = extract_pid(hit)
        if not pid:
            raise ValueError("cj_mcp search hit carried no product id")

        # Cheap pre-gate: the search hit already carries status and
        # warehouse stock, so an obviously dead listing is dropped here
        # rather than costing a detail round-trip. The merged payload is
        # still gated below — this only avoids the call.
        hit_alive, hit_reason = is_mcp_payload_live(hit, country)
        if not hit_alive:
            raise ValueError(
                f"failed the liveness gate on the search hit: {hit_reason}"
            )

        try:
            detail = await client.query_sku_details(pid)
        except CjMcpToolError as exc:
            raise ValueError(f"product-detail call failed: {exc}") from exc

        if not detail:
            raise ValueError("product-detail payload was empty")

        merged = merge_product_payloads(hit, detail, country)

        alive, reason = is_mcp_payload_live(merged, country)
        if not alive:
            raise ValueError(f"failed the MCP payload liveness gate: {reason}")

        title = extract_title(merged)
        if not title:
            raise ValueError(f"cj_mcp pid={pid} has no product title")

        price_usd = extract_price_usd(merged)
        if price_usd is None:
            raise ValueError(f"cj_mcp pid={pid} has no usable listed price")

        gallery = extract_gallery(merged)
        if len(gallery) < _MIN_GALLERY_IMAGES:
            raise ValueError(
                f"cj_mcp pid={pid} gallery has only {len(gallery)} image(s)"
            )

        return RawSupplierProduct(
            supplier_name=_SUPPLIER_NAME,
            # CJ's own PDP link when it publishes one (authoritative, and
            # the only known-good form for UUID-pid listings); the canonical
            # plain-pid URL otherwise.
            supplier_retail_url=extract_product_url(merged) or product_page_url(pid),
            product_title=title,
            product_description=extract_description(merged) or title,
            price_aud=round(price_usd * settings.USD_TO_AUD, 2),
            shipping_cost_aud=0.0,
            image_urls=gallery,
        )
