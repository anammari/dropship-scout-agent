"""CJdropshipping MCP client — the zero-bot-surface supplier path (plan §5).

The retired REST path (`cj_client.py`) could not tell a live listing from a
delisted one: `/product/list` and `/product/query` returned byte-identical
shapes for both (`saleStatus=3`, variants, populated stock), and CJ fronts
every PDP with a Cloudflare Turnstile wall that no automated client clears,
so the tier-2 frontend check was permanently inconclusive from this
machine. The official CJdropshipping **MCP server** replaces the walled
frontend as the absolute source of truth: the agent speaks MCP over
StreamableHTTP to a remote endpoint and reads structured tool output
directly — no HTML, no Turnstile, no scraping surface.

Contract (plan §5.1–§5.3):

- **Endpoint:** ``{CJ_MCP_BASE_URL}/{CJ_MCP_TOKEN}`` — auth is the token
  embedded in the URL path (CJ's documented remote-connection format). The
  token is never logged; log `redacted_endpoint` instead.
- **Transport:** StreamableHTTP via the official ``mcp`` Python SDK
  (``mcp.client.streamable_http``). The SDK's SSE transport is deprecated
  and is not used.
- **Session:** one client session per pipeline run — connect once, execute
  every keyword/tool call over it, close on exit.
- **Discovery:** ``tools/list`` on first connect pins the real tool names
  and parameter schemas. Logical parameters (keyword, warehouse, inventory
  filter, country, limit) are mapped onto whatever the discovered schema
  actually declares; an expected tool that cannot be resolved raises
  `CjMcpToolError` rather than guessing.

Error taxonomy (plan §5.3) — none of these are swallowed silently:
`CjMcpNotConfiguredError` (no token), `CjMcpConnectionError` (endpoint
unreachable / auth rejected / protocol error), `CjMcpToolError` (tool
missing from the catalog, invalid parameters, or a tool-level error
result).

Zero LLM involvement — every field parsed here is tool-returned data for
the exact SKU listing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.config import DEFAULT_CJ_MCP_BASE_URL, settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool catalog expectations (plan §5.1)
# ---------------------------------------------------------------------------

# Canonical names this client binds. Resolution falls back to the alias
# lists below so a renamed/renumbered tool still binds deterministically
# instead of the client guessing.
SEARCH_TOOL_NAME = "search_products"
# The plan's contract calls this "query_sku_details", but the live catalog's
# `query_sku_details` returns an empty array for catalog product ids — the
# tool that actually carries variants, pricing, the gallery, and the
# description is `get_product_detail`. Pinned empirically; the alias list
# below keeps the plan's name as a fallback.
DETAIL_TOOL_NAME = "get_product_detail"

_SEARCH_TOOL_ALIASES: Tuple[str, ...] = (
    "search_products",
    "search_product",
    "searchproducts",
    "product_search",
    "products_search",
    "search",
)
# The detail tool is resolved by CONTRACT, not by the name the plan
# anticipated. Pinned against the live catalog: `query_sku_details` exists
# but returns an empty array for catalog product ids, whereas
# `get_product_detail` returns exactly what this pipeline needs — variants
# with per-variant pricing, the full `productImageSet` gallery, the HTML
# description, and the product status. The rest are ordered fallbacks.
_SKU_DETAIL_TOOL_ALIASES: Tuple[str, ...] = (
    "get_product_detail",
    "get_product_details",
    "product_details",
    "product_detail",
    "product_query",
    "query_product",
    "get_product",
    "query_sku_details",
    "query_sku_detail",
    "query_sku",
    "sku_details",
    "sku_detail",
)

# Logical parameter -> the field names a discovered tool schema might declare
# it under, most specific first. Only names the schema actually declares are
# sent, so an unknown tool shape degrades to "fewer filters", never to an
# invalid call.
_PARAM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "keyword": (
        "keyword", "keywords", "productName", "product_name", "productNameEn",
        "searchText", "search_text", "searchKeyword", "search_keyword",
        "query", "q", "name",
    ),
    "global_warehouse": (
        "isWarehouse", "is_warehouse", "globalWarehouse", "global_warehouse",
        "warehouseOnly", "warehouse_only",
    ),
    # NOTE: on the CJ MCP search tool `countryCode` selects the WAREHOUSE
    # country whose stock must back the listing — it is NOT a shipping
    # destination filter. Passing the AU destination here (as the retired
    # REST client did) filters down to CJ's Australian warehouse and returns
    # essentially nothing.
    "warehouse_country": (
        "countryCode", "country_code", "warehouseCountry", "warehouse_country",
        "country", "shipFromCountryCode", "ship_from_country_code",
    ),
    "inventory_available": (
        "startWarehouseInventory", "start_warehouse_inventory",
        "inventoryAvailable", "inventory_available", "hasInventory",
        "has_inventory", "inStock", "in_stock", "onlyAvailable",
        "only_available", "availableOnly", "available_only", "minInventory",
        "min_inventory",
    ),
    "inventory_max": (
        "endWarehouseInventory", "end_warehouse_inventory", "maxInventory",
        "max_inventory",
    ),
    "features": ("features", "feature", "extraFeatures", "extra_features"),
    "limit": (
        "pageSize", "page_size", "limit", "size", "count", "maxResults",
        "max_results", "num", "pageCount", "page_count",
    ),
    "page": ("pageNum", "page_num", "page", "pageIndex", "page_index", "pageNo", "page_no"),
    "price_min": (
        "priceMin", "price_min", "minPrice", "min_price", "startPrice",
        "start_price", "lowPrice", "low_price",
    ),
    "price_max": (
        "priceMax", "price_max", "maxPrice", "max_price", "endPrice",
        "end_price", "highPrice", "high_price",
    ),
    "pid": (
        "pid", "productId", "product_id", "productID", "id", "sku",
        "skuId", "sku_id", "productSku", "product_sku", "cjSku", "cj_sku",
        "variantSku", "variant_sku",
    ),
}

# The warehouse filter CJ's own docs describe for the remote MCP server:
# `isWarehouse=true` + `countryCode=CN` is the documented "China warehouse"
# intent mapping (852 of 6000 "kitchen gadgets" listings carry CN stock).
DEFAULT_WAREHOUSE = "China"
DEFAULT_WAREHOUSE_COUNTRY = "CN"

# The search tool's own prose preamble ("📋 Found N products total.") precedes
# the JSON body in the text content block, so JSON is decoded loosely from
# the first brace rather than assuming the block is pure JSON.
_JSON_START_CHARS = "{["

# A rate-limited tool call is a pacing signal, not a wall — one bounded
# retry before surfacing CjMcpToolError (plan §13).
_TOOL_RETRY_BACKOFF_SECONDS = 2.0
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate-limit", "ratelimit", "too many requests")

# CJ's remote MCP server is slow and bursty: a single product-detail call has
# been observed taking ~40s, and longer while rate-limited. A tight read
# timeout turns that into a spurious CjMcpToolError and silently costs a
# candidate, so the default is deliberately generous.
_DEFAULT_READ_TIMEOUT_SECONDS = 180.0

# Result-payload shapes: envelope keys that hold the actual product array,
# and the envelope keys unwrapped when a single product is expected.
_LIST_KEYS: Tuple[str, ...] = (
    "list", "products", "productList", "product_list", "items", "itemList",
    "item_list", "records", "rows", "content", "data", "result", "results",
)
_ENVELOPE_KEYS: Tuple[str, ...] = (
    "data", "result", "product", "productInfo", "product_info", "detail",
    "item",
)
_PID_KEYS: Tuple[str, ...] = (
    "pid", "productId", "product_id", "productID", "id", "productSku",
    "product_sku", "cjSku", "cj_sku",
)
_TITLE_KEYS: Tuple[str, ...] = (
    "productNameEn", "product_name_en", "nameEn", "name_en", "productName",
    "product_name", "productTitle", "product_title", "title", "name",
)
_IMAGE_SET_KEYS: Tuple[str, ...] = (
    "productImageSet", "product_image_set", "images", "imageList",
    "image_list", "imageUrls", "image_urls", "gallery", "galleryImages",
    "productImages", "product_images", "productImage", "product_image",
    "bigImage", "big_image", "variantImage", "variant_image",
    "variantImages", "variant_images", "mainImage", "main_image",
)
_DESCRIPTION_KEYS: Tuple[str, ...] = (
    "description", "productDescription", "product_description", "describe",
    "productDesc", "product_desc", "detail", "overview",
)
_PRICE_KEYS: Tuple[str, ...] = (
    "sellPrice", "sell_price", "price", "productPrice", "product_price",
    "priceUsd", "price_usd", "variantSellPrice", "variant_sell_price",
    "wholesalePrice", "wholesale_price", "minPrice", "min_price",
)
# Commercial demand vocabulary (plan §6.2). `listedNum` is how many
# dropshippers have imported the listing, and it is the *only* demand metric
# CJ's MCP surface reports: probed live across `search_products` and
# `get_product_detail`, no historical-sales field exists under any name
# (`sellNum`, `sales`, `soldNum`, `orderNum`, …), and none of the 62
# advertised tools carries one. Both records do carry `listedNum`. Note
# `variantVolume` is NOT sales — it is the variant's volumetric freight
# dimension in mm³.
_LISTED_COUNT_KEYS: Tuple[str, ...] = (
    "listedNum", "listed_num", "listNum", "list_num", "listedCount",
    "listed_count", "importNum", "import_num",
)

# ---------------------------------------------------------------------------
# MCP Payload Liveness Gate field vocabulary (plan §5.2)
# ---------------------------------------------------------------------------

# Explicit "this listing is gone" flags — any truthy value kills the
# candidate outright, whatever the status fields say.
_DELIST_FLAG_FIELDS: Tuple[str, ...] = (
    "isDeleted", "is_deleted", "deleted", "removed", "isRemoved",
    "is_removed", "delisted", "isDelisted", "is_delisted", "invalid",
    "isInvalid", "is_invalid", "offShelf", "off_shelf",
)

# Per-field status vocabularies. `saleStatus` and `entryStatus` are CJ's own
# REST field names and are carried over here as the most likely MCP shapes;
# the generic set covers `status` / `productStatus` / `listingStatus` / `state`.
_SALE_STATUS_ACTIVE = frozenset({"3", "onsale", "sale", "selling", "active"})
_SALE_STATUS_DEAD = frozenset({
    "0", "1", "2", "4", "5", "offsale", "notonsale", "removed", "delisted",
    "inactive", "disabled", "deleted",
})
_ENTRY_STATUS_ACTIVE = frozenset({"1", "active", "onsale", "normal", "listed"})
_ENTRY_STATUS_DEAD = frozenset({
    "0", "2", "3", "4", "removed", "delisted", "deleted", "inactive",
    "disabled",
})
_GENERIC_STATUS_ACTIVE = frozenset({
    "1", "3", "active", "enabled", "listed", "online", "onsale", "selling",
    "normal", "available", "valid", "success", "true", "yes",
})
_GENERIC_STATUS_DEAD = frozenset({
    "0", "2", "4", "5", "inactive", "disabled", "offline", "unlisted",
    "removed", "delisted", "deleted", "expired", "invalid", "banned",
    "closed", "false", "no",
})

_STATUS_FIELD_GROUPS: Tuple[Tuple[Tuple[str, ...], frozenset, frozenset], ...] = (
    (
        ("saleStatus", "sale_status"),
        _SALE_STATUS_ACTIVE,
        _SALE_STATUS_DEAD,
    ),
    (
        ("entryStatus", "entry_status"),
        _ENTRY_STATUS_ACTIVE,
        _ENTRY_STATUS_DEAD,
    ),
    (
        ("status", "productStatus", "product_status", "listingStatus",
         "listing_status", "state", "productState", "product_state"),
        _GENERIC_STATUS_ACTIVE,
        _GENERIC_STATUS_DEAD,
    ),
)

# Boolean availability/listing flags. A falsy value is a DROP; a truthy one
# is an active signal (stock is still required separately).
_BOOLEAN_ACTIVE_FIELDS: Tuple[str, ...] = (
    "isActive", "is_active", "active", "isListed", "is_listed", "listed",
    "enabled", "isEnabled", "is_enabled", "isAvailable", "is_available",
    "onShelf", "on_shelf", "selling", "inStock", "in_stock",
)

_STOCK_FIELDS: Tuple[str, ...] = (
    # CJ MCP search hits carry the warehouse-backed quantity here.
    "warehouseInventoryNum", "warehouse_inventory_num",
    # CJ MCP inventory responses use the ...Num suffix.
    "totalInventoryNum", "total_inventory_num", "cjInventoryNum",
    "cj_inventory_num", "factoryInventoryNum", "factory_inventory_num",
    "inventoryNum", "inventory_num", "inventory", "stock", "stockNum",
    "stock_num", "stockQuantity", "stock_quantity", "availableStock",
    "available_stock", "availableQuantity", "available_quantity",
    "availableNum", "available_num", "quantity", "qty", "totalInventory",
    "total_inventory", "cjInventory", "cj_inventory", "inventoryQuantity",
    "inventory_quantity", "inventoryTotal", "inventory_total", "skuInventory",
    "sku_inventory",
)
_STOCK_CONTAINER_FIELDS: Tuple[str, ...] = (
    "variants", "variantList", "variant_list", "skus", "skuList", "sku_list",
    "inventories", "inventoryList", "inventory_list", "warehouses",
    "warehouseList", "warehouse_list",
)
_COUNTRY_KEYS: Tuple[str, ...] = (
    "countryCode", "country_code", "country", "warehouseCountry",
    "warehouse_country", "region", "areaCode", "area_code",
)

# A gallery entry may be a bare URL string or a dict carrying the URL under
# one of these keys.
_IMAGE_ENTRY_KEYS: Tuple[str, ...] = (
    "url", "image", "imageUrl", "image_url", "imagePath", "image_path",
    "src", "original", "big", "path",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

_MAX_DESCRIPTION_CHARS = 4000


# ---------------------------------------------------------------------------
# Error taxonomy (plan §5.3)
# ---------------------------------------------------------------------------


class CjMcpError(RuntimeError):
    """Base failure for a CJ MCP interaction."""


class CjMcpNotConfiguredError(CjMcpError):
    """`CJ_MCP_TOKEN` is missing from `.env` — the MCP path is unavailable."""


class CjMcpConnectionError(CjMcpError):
    """The remote MCP endpoint could not be reached, or the session failed.

    Covers auth rejection, an unreachable host, and protocol/handshake
    errors — anything that prevents a usable session from being established.
    """


class CjMcpToolError(CjMcpError):
    """A tool call could not be completed.

    The tool is absent from the discovered catalog, the arguments were
    rejected, or the server returned an error result.
    """


# ---------------------------------------------------------------------------
# Small normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_token(value: Any) -> str:
    """Lowercase, strip everything but [a-z0-9] — 'ON_SALE'/'On Sale' -> 'onsale'."""
    return _NON_ALNUM_RE.sub("", str(value).strip().lower())


def _normalise_tool_name(name: Any) -> str:
    return _NON_ALNUM_RE.sub("", str(name or "").lower())


def _as_number(value: Any) -> Optional[float]:
    """A finite float for numeric-ish values; None for booleans/junk.

    `bool` is excluded explicitly — in Python `True` is an `int`, and a
    `available: true` flag must never be read as "1 unit in stock".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _truthy(value: Any) -> bool:
    """Tri-state-ish truthiness for flag fields: True / False (None -> False)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    token = _normalise_token(value)
    if token in {"true", "yes", "y", "1", "active", "enabled"}:
        return True
    if token in {"", "false", "no", "n", "0", "none", "null"}:
        return False
    return True


def _coerce_price(raw: Any) -> Optional[float]:
    """A listed price from a number or a range string.

    CJ quotes ranges as "4.56 -- 4.92"; the LOW end is taken as the
    conservative wholesale reference (same rule the retired REST client
    used). Values that are not strictly positive are rejected — a free or
    zero-cost listing cannot clear the margin floor downstream and is a
    data-quality signal, not a candidate.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if "--" in text:
            text = text.split("--")[0].strip()
        elif "-" in text and not text.startswith("-"):
            head = text.split("-")[0].strip()
            if head:
                text = head
        value = _as_number(text)
    else:
        value = _as_number(raw)
    if value is None or value <= 0:
        return None
    return value


def clean_description(raw: Any) -> str:
    """HTML-strip and collapse whitespace; '' when there is nothing usable."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = _WS_RE.sub(" ", text).strip()
    return text[:_MAX_DESCRIPTION_CHARS]


def _first_present(node: Mapping, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in node and node[key] not in (None, ""):
            return node[key]
    return None


# ---------------------------------------------------------------------------
# Discovered-schema parameter mapping (plan §4.2 / §5.1)
# ---------------------------------------------------------------------------


def _schema_properties(input_schema: Any) -> Dict[str, Any]:
    """The `properties` map of a tool's inputSchema, or {}."""
    if not isinstance(input_schema, Mapping):
        return {}
    properties = input_schema.get("properties")
    return dict(properties) if isinstance(properties, Mapping) else {}


def _declared_types(prop_schema: Any) -> frozenset:
    if not isinstance(prop_schema, Mapping):
        return frozenset()
    raw = prop_schema.get("type")
    if isinstance(raw, str):
        return frozenset({raw.lower()})
    if isinstance(raw, (list, tuple)):
        return frozenset(str(item).lower() for item in raw)
    return frozenset()


def _coerce_argument(value: Any, prop_schema: Any) -> Any:
    """Shape a logical value to the type the discovered schema declares.

    A boolean logical value aimed at a numeric field becomes 1/0 — that is
    what the CJ search schema wants for `startWarehouseInventory` ("minimum
    stock ≥ 1", i.e. in-stock only), where sending `true` would be rejected.
    """
    declared = _declared_types(prop_schema)
    if "boolean" in declared:
        return bool(value)
    if "integer" in declared or "number" in declared:
        if isinstance(value, bool):
            number = 1.0 if value else 0.0
        else:
            number = _as_number(value)
        if number is None:
            return value
        return int(number) if "integer" in declared else number
    if "string" in declared:
        return value if isinstance(value, str) else str(value)
    return value


def build_tool_arguments(
    input_schema: Any, logical_arguments: Mapping[str, Any]
) -> Dict[str, Any]:
    """Map logical parameters onto the tool's own declared parameter names.

    Only parameters the discovered schema actually declares are emitted, so
    an unfamiliar tool shape degrades to fewer filters rather than to an
    invalid call. Nothing is ever sent under a guessed name.
    """
    properties = _schema_properties(input_schema)
    if not properties:
        return {}
    lowered = {name.lower(): name for name in properties}

    arguments: Dict[str, Any] = {}
    for logical_name, value in logical_arguments.items():
        if value is None:
            continue
        aliases = _PARAM_ALIASES.get(logical_name, ())
        for alias in aliases:
            declared_name = alias if alias in properties else lowered.get(alias.lower())
            if declared_name is None:
                continue
            arguments[declared_name] = _coerce_argument(
                value, properties.get(declared_name)
            )
            break
    return arguments


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


def result_text(result: Any) -> str:
    """Concatenated text blocks of a CallToolResult ('' when there are none)."""
    blocks = getattr(result, "content", None) or []
    parts: List[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def decode_json_loosely(text: Any) -> Any:
    """Decode JSON from text that may be wrapped in prose.

    CJ's tools answer with a human-readable preamble before the payload
    ("📋 Found 852 products total." / "🔍 Product detail loaded."), so a
    bare `json.loads` on the text block fails. The first decodable value
    starting at a `{` or `[` wins; None when there is no JSON at all.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in _JSON_START_CHARS:
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except ValueError:
            continue
        return value
    return None


def extract_payload(result: Any) -> Any:
    """The structured payload behind a tool result.

    Prefers the protocol's `structuredContent`; falls back to the text
    blocks (the shape CJ's remote server actually uses), decoding the JSON
    loosely so a prose preamble does not defeat it. Returns the raw text
    when no JSON is present at all.
    """
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    text = result_text(result)
    if not text:
        return None
    decoded = decode_json_loosely(text)
    return text if decoded is None else decoded


def _looks_like_product(node: Mapping) -> bool:
    return any(key in node for key in _PID_KEYS)


def iter_product_dicts(payload: Any, _depth: int = 0) -> List[dict]:
    """Every product dict inside a tool payload, envelope or not.

    Walks the common CJ envelopes (`data` / `result` / `list` / `products`)
    and JSON-encoded strings, and ignores anything that is not shaped like a
    product record.
    """
    if payload is None or _depth > 6:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except ValueError:
            return []
        return iter_product_dicts(decoded, _depth + 1)
    if isinstance(payload, list):
        found: List[dict] = []
        for entry in payload:
            if isinstance(entry, Mapping):
                if _looks_like_product(entry):
                    found.append(dict(entry))
                else:
                    found.extend(iter_product_dicts(entry, _depth + 1))
            elif isinstance(entry, (list, str)):
                found.extend(iter_product_dicts(entry, _depth + 1))
        return found
    if isinstance(payload, Mapping):
        if _looks_like_product(payload):
            return [dict(payload)]
        for key in _LIST_KEYS:
            if key in payload:
                found = iter_product_dicts(payload[key], _depth + 1)
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (Mapping, list, str)):
                found = iter_product_dicts(value, _depth + 1)
                if found:
                    return found
    return []


def unwrap_envelope(payload: Any) -> Any:
    """Peel single-payload envelopes (`{code, result, data: {...}}`) off."""
    current = payload
    for _ in range(6):
        if not isinstance(current, Mapping):
            return current
        if _looks_like_product(current):
            return current
        candidate = None
        for key in _ENVELOPE_KEYS:
            inner = current.get(key)
            if isinstance(inner, Mapping):
                candidate = inner
                break
        if candidate is None:
            return current
        current = candidate
    return current


# ---------------------------------------------------------------------------
# MCP Payload Liveness Gate (plan §5.2) — the SOLE liveness arbiter
# ---------------------------------------------------------------------------


def _status_verdict(payload: Mapping) -> Tuple[Optional[bool], str]:
    """Classify the payload's active/listed status fields.

    Returns (verdict, reason) where verdict is True (active), False (dead),
    or None (no status field present at all). An unrecognised status value
    is treated as DEAD — from the MCP stream "we couldn't tell" is not an
    acceptable answer (inverted tolerance, plan §5.2).

    Every status field is examined before the verdict is returned: a single
    dead signal (e.g. `entryStatus=0`) outranks any number of active ones,
    so a payload that disagrees with itself is never emitted.
    """
    saw_field = False
    active_reason: Optional[str] = None

    for fields, active_values, dead_values in _STATUS_FIELD_GROUPS:
        for field in fields:
            if field not in payload:
                continue
            raw = payload[field]
            if raw in (None, ""):
                continue
            saw_field = True
            token = _normalise_token(raw)
            if token in dead_values:
                return False, f"{field}={raw!r} is not active"
            if token in active_values:
                if active_reason is None:
                    active_reason = f"{field}={raw!r} is active"
                continue
            # Unrecognised value -> ambiguous -> dead (strict).
            return False, (
                f"{field}={raw!r} is not a recognised active value "
                "(ambiguous liveness signal)"
            )

    for field in _BOOLEAN_ACTIVE_FIELDS:
        if field not in payload:
            continue
        raw = payload[field]
        if raw in (None, ""):
            continue
        saw_field = True
        if not _truthy(raw):
            return False, f"{field}={raw!r} is falsy"
        if active_reason is None:
            active_reason = f"{field}={raw!r} is truthy"

    if active_reason is not None:
        return True, active_reason
    if saw_field:
        return False, "no_conclusive_active_status_signal"
    return None, "no_active_status_signal_in_mcp_payload"


def _collect_stock_values(node: Any, out: List[Tuple[Optional[str], float]], depth: int = 0) -> None:
    """Collect (country_or_None, value) for every recognised stock field."""
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _collect_stock_values(item, out, depth + 1)
        return
    if not isinstance(node, Mapping):
        return

    country: Optional[str] = None
    for key in _COUNTRY_KEYS:
        raw = node.get(key)
        if isinstance(raw, str) and raw.strip():
            country = raw.strip().upper()
            break

    for field in _STOCK_FIELDS:
        if field in node:
            value = _as_number(node[field])
            if value is not None:
                out.append((country, value))

    for field in _STOCK_CONTAINER_FIELDS:
        if field in node:
            _collect_stock_values(node[field], out, depth + 1)


def extract_stock(payload: Any, target_country: str = "AU") -> Optional[float]:
    """Best available stock quantity, or None when the payload says nothing.

    None is meaningfully different from 0: no stock field at all is an
    *unverifiable* payload, which the gate drops (inverted tolerance).
    """
    if not isinstance(payload, Mapping):
        return None
    values: List[Tuple[Optional[str], float]] = []
    _collect_stock_values(payload, values)
    if not values:
        return None

    target = (target_country or "").strip().upper()
    if target:
        preferred = [value for country, value in values if country == target]
        positives = [value for value in preferred if value > 0]
        if positives:
            return max(positives)

    positives = [value for _, value in values if value > 0]
    if positives:
        return max(positives)
    return max(value for _, value in values)


def merge_product_payloads(
    hit: Mapping, detail: Mapping, target_country: str = "AU"
) -> Dict[str, Any]:
    """Merge a search hit with its detail payload for liveness judging.

    The detail record is the authoritative product record and wins wherever
    both carry a field. The one exception is inventory: CJ's detail tool
    returns null inventory for catalog products, so the search hit's
    `warehouseInventoryNum` is carried over — but only when the detail
    payload has no populated stock of its own. When the detail *does* report
    stock, it owns the availability verdict outright and the search hit's
    stock fields are dropped, so a staler figure can never rescue a listing
    the detail says is empty.
    """
    merged: Dict[str, Any] = {**dict(hit), **dict(detail)}
    if extract_stock(detail, target_country) is None:
        for field in _STOCK_FIELDS:
            if _as_number(detail.get(field)) is None and field in hit:
                merged[field] = hit[field]
    else:
        for field in _STOCK_FIELDS:
            if field not in detail:
                merged.pop(field, None)
    return merged


def is_mcp_payload_live(
    payload: Any, target_country: str = "AU"
) -> Tuple[bool, str]:
    """The MCP Payload Liveness Gate — validate before emitting a candidate.

    A payload is LIVE only when it explicitly confirms BOTH:

    (a) the listing is active — a recognised active status value or a
        truthy listing flag, with no explicit delist flag set, and
    (b) stock availability — at least one recognised inventory field with a
        positive quantity (target country preferred).

    Everything else is DEAD, including the previously-tolerated "stock not
    populated" case: the MCP stream is the source of truth, so a missing or
    ambiguous signal is a drop rather than a benefit of the doubt.
    """
    if not isinstance(payload, Mapping) or not payload:
        return False, "empty_mcp_payload"

    for field in _DELIST_FLAG_FIELDS:
        if field in payload and _truthy(payload[field]):
            return False, f"delist_flag:{field}={payload[field]!r}"

    status_verdict, status_reason = _status_verdict(payload)
    if status_verdict is False:
        return False, status_reason
    if status_verdict is None:
        return False, status_reason

    stock = extract_stock(payload, target_country)
    if stock is None:
        return False, "no_availability_signal_in_mcp_payload"
    if stock <= 0:
        return False, "zero_stock_in_mcp_payload"

    return True, f"{status_reason}; stock={stock:g}"


# ---------------------------------------------------------------------------
# Session factory (StreamableHTTP)
# ---------------------------------------------------------------------------


_REDACTED = "***"

# Every logger that could echo a request URL: the MCP SDK's own loggers and
# the HTTP transports underneath it (mcp 2.x vendors httpx as `httpx2`).
_REDACTED_LOGGER_NAMES: Tuple[str, ...] = (
    "httpx", "httpx2", "httpcore", "httpcore2", "mcp", "sse_starlette",
)
_REDACTION_INSTALLED: set = set()


class TokenRedactingFilter(logging.Filter):
    """Scrub the MCP token out of log records before any handler formats them.

    The token travels in the endpoint URL *path*, and the HTTP transport
    logs every request URL at INFO. Left alone, a plain `logging.INFO` run
    writes the credential to stdout and any log file — the one thing
    `Settings` promises never to do.
    """

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            if self.secret in value:
                return value.replace(self.secret, _REDACTED)
            return value
        if isinstance(value, tuple):
            return tuple(self._scrub(item) for item in value)
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, Mapping):
            return {key: self._scrub(item) for key, item in value.items()}
        # httpx passes its URL *object* (not a string) as a log argument, and
        # `%`-formatting stringifies it later. Stringify now so the secret is
        # caught before it reaches the handler.
        try:
            text = str(value)
        except Exception:  # pragma: no cover - defensive
            return value
        if self.secret in text:
            return text.replace(self.secret, _REDACTED)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        record.args = self._scrub(record.args)
        return True


def install_token_redaction(token: Optional[str]) -> None:
    """Attach a redacting filter for `token` to every URL-logging logger.

    Idempotent per token, so repeated connections do not stack filters.
    """
    if not token or token in _REDACTION_INSTALLED:
        return
    _REDACTION_INSTALLED.add(token)
    redactor = TokenRedactingFilter(token)
    for name in _REDACTED_LOGGER_NAMES:
        logging.getLogger(name).addFilter(redactor)
    # Anything that escapes the named loggers still passes through the root
    # handlers on its way out — scrub there too.
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


@asynccontextmanager
async def default_session_factory(endpoint_url: str, read_timeout: float):
    """Open one initialized MCP session over StreamableHTTP.

    Imports the SDK lazily so the rest of the pipeline (and its tests) does
    not pay for it unless a CJ MCP run actually happens.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(endpoint_url) as (read_stream, write_stream):
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=read_timeout
        ) as session:
            await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CjMcpClient:
    """Async client for the remote CJdropshipping MCP server.

    One instance per pipeline run: `async with client:` connects, discovers
    the tool catalog, and holds the session open for every keyword and
    sku-detail call until the block exits.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        session_factory=None,
        read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        resolved = token if token is not None else settings.CJ_MCP_TOKEN
        self._token: Optional[str] = (resolved or "").strip() or None
        self._base_url: str = (
            base_url or settings.CJ_MCP_BASE_URL or DEFAULT_CJ_MCP_BASE_URL
        ).rstrip("/")
        self._session_factory = session_factory or default_session_factory
        self._read_timeout = read_timeout

        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[Any] = None
        self._tools: Dict[str, Any] = {}
        self._search_tool: Optional[Any] = None
        self._sku_detail_tool: Optional[Any] = None

    # -- introspection --------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._token)

    @property
    def endpoint_url(self) -> str:
        """The full remote endpoint, token included. NEVER log this value."""
        if not self._token:
            raise CjMcpNotConfiguredError(
                "CJ_MCP_TOKEN is not configured (set it in .env to enable "
                "the CJdropshipping MCP extractor)"
            )
        return f"{self._base_url}/{self._token}"

    @property
    def redacted_endpoint(self) -> str:
        """A log-safe rendering of the endpoint (token elided)."""
        return f"{self._base_url}/***"

    @property
    def tool_names(self) -> List[str]:
        """Names of every tool the server advertised on connect."""
        return sorted(self._tools)

    @property
    def connected(self) -> bool:
        return self._session is not None

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> "CjMcpClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False

    async def connect(self) -> "CjMcpClient":
        """Open the session and pin the tool catalog. Idempotent."""
        if self._session is not None:
            return self
        if not self.configured:
            raise CjMcpNotConfiguredError(
                "CJ_MCP_TOKEN is not configured (set it in .env to enable "
                "the CJdropshipping MCP extractor)"
            )

        # The token is in the URL path; make sure no transport log can echo it.
        install_token_redaction(self._token)

        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                self._session_factory(self.endpoint_url, self._read_timeout)
            )
            tools_result = await session.list_tools()
        except CjMcpError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise CjMcpConnectionError(
                f"CJ MCP session against {self.redacted_endpoint} failed: {exc}"
            ) from exc

        self._exit_stack = stack
        self._session = session
        try:
            self._register_tools(tools_result)
        except CjMcpError:
            await self.aclose()
            raise
        logger.info(
            "CJ MCP connected to %s — %d tool(s) discovered: %s",
            self.redacted_endpoint, len(self._tools), self.tool_names,
        )
        return self

    async def aclose(self) -> None:
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        self._tools = {}
        self._search_tool = None
        self._sku_detail_tool = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.debug("CJ MCP session teardown raised", exc_info=True)

    def _require_session(self) -> Any:
        if self._session is None:
            raise CjMcpConnectionError(
                "CJ MCP client is not connected — call connect() (or use "
                "`async with client:`) before issuing tool calls"
            )
        return self._session

    # -- tool discovery -------------------------------------------------

    def _register_tools(self, tools_result: Any) -> None:
        tools = getattr(tools_result, "tools", None) or []
        catalog: Dict[str, Any] = {}
        for tool in tools:
            name = getattr(tool, "name", None)
            if isinstance(name, str) and name.strip():
                catalog[name.strip()] = tool
        self._tools = catalog

        self._search_tool = self._resolve_tool(
            catalog, _SEARCH_TOOL_ALIASES, SEARCH_TOOL_NAME
        )
        self._sku_detail_tool = self._resolve_tool(
            catalog, _SKU_DETAIL_TOOL_ALIASES, DETAIL_TOOL_NAME
        )

    @staticmethod
    def _resolve_tool(
        catalog: Dict[str, Any], aliases: Tuple[str, ...], canonical: str
    ) -> Any:
        """Bind a catalog entry to a canonical tool name, deterministically.

        Exact name, then case-insensitive name, then normalised name, then
        normalised substring — shortest match wins so the choice never
        depends on catalog ordering. Nothing found is an error, not a guess.
        """
        if not catalog:
            raise CjMcpToolError(
                f"CJ MCP server advertised no tools; expected {canonical!r}"
            )
        if canonical in catalog:
            return catalog[canonical]

        lowered = {name.lower(): name for name in catalog}
        for alias in aliases:
            match = lowered.get(alias.lower())
            if match is not None:
                return catalog[match]

        normalised = {_normalise_tool_name(name): name for name in catalog}
        for alias in aliases:
            match = normalised.get(_normalise_tool_name(alias))
            if match is not None:
                return catalog[match]

        candidates: List[str] = []
        for alias in aliases:
            needle = _normalise_tool_name(alias)
            for key, name in normalised.items():
                if needle and (needle in key or key in needle):
                    candidates.append(name)
        if candidates:
            return catalog[min(candidates, key=lambda name: (len(name), name))]

        raise CjMcpToolError(
            f"CJ MCP tool {canonical!r} is not in the discovered catalog "
            f"({sorted(catalog)}); refusing to guess a substitute"
        )

    # -- tool calls -----------------------------------------------------

    async def _call_tool(self, tool: Any, arguments: Mapping[str, Any]) -> Any:
        """Execute one tool call, mapping every failure onto the taxonomy."""
        session = self._require_session()
        name = getattr(tool, "name", None) or "unknown"
        payload = dict(arguments) or None

        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                result = await session.call_tool(name, arguments=payload)
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0 and self._is_rate_limited(last_error):
                    logger.warning(
                        "CJ MCP tool %r rate-limited; retrying once in %.1fs",
                        name, _TOOL_RETRY_BACKOFF_SECONDS,
                    )
                    await asyncio.sleep(_TOOL_RETRY_BACKOFF_SECONDS)
                    continue
                raise CjMcpToolError(
                    f"CJ MCP tool {name!r} call failed: {exc}"
                ) from exc

            if getattr(result, "is_error", False):
                detail = result_text(result) or "no error detail returned"
                if attempt == 0 and self._is_rate_limited(detail):
                    logger.warning(
                        "CJ MCP tool %r rate-limited; retrying once in %.1fs",
                        name, _TOOL_RETRY_BACKOFF_SECONDS,
                    )
                    await asyncio.sleep(_TOOL_RETRY_BACKOFF_SECONDS)
                    continue
                raise CjMcpToolError(
                    f"CJ MCP tool {name!r} returned an error result: {detail}"
                )
            return result

        raise CjMcpToolError(
            f"CJ MCP tool {name!r} failed after a bounded retry: {last_error}"
        )

    @staticmethod
    def _is_rate_limited(text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)

    # -- product surface ------------------------------------------------

    async def search_products(
        self,
        keyword: str,
        limit: Optional[int] = None,
        warehouse_country: Optional[str] = DEFAULT_WAREHOUSE_COUNTRY,
        global_warehouse: bool = True,
        inventory_available: bool = True,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
        features: Optional[Sequence[str]] = None,
    ) -> List[dict]:
        """Run the product-search tool for one keyword.

        Returns the raw product records the tool reported. Liveness is NOT
        judged here — every hit must still pass `is_mcp_payload_live`, both
        on its own and again after the detail call is merged in.
        """
        self._require_session()
        tool = self._search_tool
        if tool is None:
            raise CjMcpToolError(
                "CJ MCP search tool was never resolved — the client is not "
                "connected"
            )
        arguments = build_tool_arguments(
            getattr(tool, "input_schema", None),
            {
                "keyword": keyword,
                "warehouse_country": warehouse_country,
                "global_warehouse": global_warehouse,
                "inventory_available": inventory_available,
                "limit": limit,
                "price_min": price_min,
                "price_max": price_max,
                "features": list(features) if features else None,
            },
        )
        if not arguments:
            raise CjMcpToolError(
                f"CJ MCP search tool {getattr(tool, 'name', '?')!r} declares "
                "no parameter this client can map — refusing to call it blind"
            )
        logger.debug("CJ MCP search arguments: %s", arguments)
        result = await self._call_tool(tool, arguments)
        return iter_product_dicts(extract_payload(result))

    async def query_sku_details(self, pid: str) -> dict:
        """Fetch pricing/variant/gallery detail for one product id.

        Named for the plan's contract; on the live CJ catalog the resolved
        tool is `get_product_detail` (see `_SKU_DETAIL_TOOL_ALIASES`).
        Returns the best single product record the tool reported ({} when
        it reported nothing usable). Callers treat an empty payload as an
        immediate drop, per the liveness gate.
        """
        self._require_session()
        tool = self._sku_detail_tool
        if tool is None:
            raise CjMcpToolError(
                "CJ MCP sku-detail tool was never resolved — the client is "
                "not connected"
            )
        arguments = build_tool_arguments(
            getattr(tool, "input_schema", None), {"pid": pid}
        )
        if not arguments:
            raise CjMcpToolError(
                f"CJ MCP sku-detail tool {getattr(tool, 'name', '?')!r} "
                "declares no product-id parameter this client can map"
            )
        logger.debug("CJ MCP sku-detail arguments: %s", arguments)
        result = await self._call_tool(tool, arguments)
        payload = extract_payload(result)
        products = iter_product_dicts(payload)
        if products:
            return products[0]
        unwrapped = unwrap_envelope(payload)
        return dict(unwrapped) if isinstance(unwrapped, Mapping) else {}


# ---------------------------------------------------------------------------
# Product-field extraction (shared with the extractor)
# ---------------------------------------------------------------------------


def extract_pid(payload: Mapping) -> Optional[str]:
    """The CJ product id behind a payload, or None."""
    value = _first_present(payload, _PID_KEYS)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_title(payload: Mapping) -> str:
    value = _first_present(payload, _TITLE_KEYS)
    return str(value).strip() if value is not None else ""


def extract_description(payload: Mapping) -> str:
    value = _first_present(payload, _DESCRIPTION_KEYS)
    return clean_description(value)


def extract_price_usd(payload: Mapping) -> Optional[float]:
    """The listed USD unit price (low end of a quoted range)."""
    return _coerce_price(_first_present(payload, _PRICE_KEYS))


def extract_listed_count(payload: Mapping) -> Optional[int]:
    """How many dropshippers have imported this listing, or None.

    The CJ commercial gate's only metric (plan §6.2): CJ's MCP surface
    reports no historical-sales figure under any name, so the dropshipper
    listing count stands in as proof that the item is already being sold
    elsewhere. None means the payload did not report one — unproven, which
    the gate drops rather than treats as a pass.
    """
    number = _as_number(_first_present(payload, _LISTED_COUNT_KEYS))
    if number is None:
        return None
    return int(number)


def _image_url_from_entry(entry: Any) -> Optional[str]:
    if isinstance(entry, str):
        text = entry.strip()
        return text or None
    if isinstance(entry, Mapping):
        for key in _IMAGE_ENTRY_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _collect_image_urls(node: Any, out: List[str], depth: int = 0) -> None:
    """Gather CDN gallery URLs from any of the payload's image shapes."""
    if depth > 6 or node is None:
        return
    if isinstance(node, str):
        url = _image_url_from_entry(node)
        if url:
            out.append(url)
        return
    if isinstance(node, list):
        for entry in node:
            _collect_image_urls(entry, out, depth + 1)
        return
    if not isinstance(node, Mapping):
        return

    # A bare image entry ({url: ...} / {imagePath: ...}) inside a gallery
    # array carries the URL directly. Product records are excluded so their
    # own link fields are never mistaken for gallery assets.
    if not _looks_like_product(node):
        entry_url = _image_url_from_entry(node)
        if entry_url is not None:
            out.append(entry_url)
            return

    for key in _IMAGE_SET_KEYS:
        if key in node:
            _collect_image_urls(node[key], out, depth + 1)
    # Variant-level images (the gallery is sometimes split per variant).
    for key in ("variants", "variantList", "variant_list", "skus", "skuList", "sku_list"):
        if key in node:
            _collect_image_urls(node[key], out, depth + 1)


def extract_gallery(payload: Mapping) -> List[str]:
    """Distinct, order-preserving CDN gallery URLs from a product payload."""
    urls: List[str] = []
    _collect_image_urls(payload, urls)
    seen: set = set()
    gallery: List[str] = []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        gallery.append(url)
    return gallery


def product_page_url(pid: str) -> str:
    """The canonical CJdropshipping product-detail page for a pid."""
    return f"https://cjdropshipping.com/product/{pid}.html"


_PRODUCT_URL_KEYS: Tuple[str, ...] = (
    "productUrl", "product_url", "detailUrl", "detail_url", "pdpUrl",
    "pdp_url", "link", "url",
)
_CJ_PRODUCT_URL_PATTERN = re.compile(
    r"/product/(?:[\w-]+-p-)?"
    r"(?:\d+|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})\.html"
)


def extract_product_url(payload: Mapping) -> Optional[str]:
    """CJ's OWN product-detail link, when the payload carries one.

    Preferred over a synthesized URL because it is supplier data rather
    than something this pipeline assembled — and because CJ's canonical
    plain-pid form is only known-good for numeric pids (older UUID-pid
    listings are published by CJ under the `{slug}-p-{pid}.html` shape).
    """
    value = _first_present(payload, _PRODUCT_URL_KEYS)
    if isinstance(value, str) and _CJ_PRODUCT_URL_PATTERN.search(value):
        return value
    return None
