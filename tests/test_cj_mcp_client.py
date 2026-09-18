"""Hermetic unit tests for `src.pipeline.cj_mcp_client`.

No network and no MCP transport: a scripted fake session stands in for the
remote CJdropshipping MCP server, so the suite covers session/token
handling, `tools/list` discovery, schema-driven tool-call serialization,
result parsing, the `CjMcp*` error taxonomy, and the MCP Payload Liveness
Gate (out-of-stock / delisted / ambiguous payloads strictly dropped).
"""

import json
import logging
from contextlib import asynccontextmanager

import pytest

from src.pipeline import cj_mcp_client as cc
from src.pipeline.cj_mcp_client import (
    CjMcpClient,
    CjMcpConnectionError,
    CjMcpNotConfiguredError,
    CjMcpToolError,
    build_tool_arguments,
    decode_json_loosely,
    extract_gallery,
    extract_payload,
    extract_price_usd,
    is_mcp_payload_live,
    iter_product_dicts,
    merge_product_payloads,
    product_page_url,
)

_TOKEN = "mcp-token-abc123"
_BASE = "https://developers.cjdropshipping.com/mcp"


# ----------------------------------------------------------------------
# Fakes for the MCP session
# ----------------------------------------------------------------------


class FakeTool:
    def __init__(self, name, schema=None):
        self.name = name
        self.input_schema = schema if schema is not None else {}


class FakeTextBlock:
    def __init__(self, text):
        self.text = text


class FakeCallToolResult:
    def __init__(self, text=None, structured=None, is_error=False):
        self.content = [FakeTextBlock(text)] if text is not None else []
        self.structured_content = structured
        self.is_error = is_error


class FakeListToolsResult:
    def __init__(self, tools):
        self.tools = tools


class FakeSession:
    """Scripted MCP session.

    `responses` maps a tool name to a list of results (or Exceptions to
    raise); each call pops the next entry and the last entry repeats.
    """

    def __init__(self, tools, responses=None, list_tools_error=None):
        self._tools = tools
        self._responses = {k: list(v) for k, v in (responses or {}).items()}
        self._list_tools_error = list_tools_error
        self.calls = []
        self.list_tools_calls = 0

    async def list_tools(self):
        self.list_tools_calls += 1
        if self._list_tools_error is not None:
            raise self._list_tools_error
        return FakeListToolsResult(self._tools)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        script = self._responses.get(name)
        if not script:
            return FakeCallToolResult(text=json.dumps({"data": {"list": []}}))
        entry = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeSessionFactory:
    """Async-context-manager session factory that records its lifecycle."""

    def __init__(self, session=None, error=None):
        self.session = session
        self.error = error
        self.endpoints = []
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self, endpoint_url, read_timeout):
        self.endpoints.append(endpoint_url)
        if self.error is not None:
            raise self.error
        self.entered += 1
        try:
            yield self.session
        finally:
            self.exited += 1


def _schema(*names):
    """A tool inputSchema declaring the given property names (all strings)."""
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
    }


def _search_schema(**overrides):
    """CJ's live search_products schema (the parameters this client uses)."""
    properties = {
        "keyword": {"type": "string"},
        "isWarehouse": {"type": "boolean"},
        "countryCode": {"type": "string"},
        "startWarehouseInventory": {"type": "number"},
        "pageSize": {"type": "number"},
    }
    properties.update(overrides)
    return {"type": "object", "properties": properties}


_DEFAULT_TOOLS = [
    FakeTool("search_products", _search_schema()),
    FakeTool("query_sku_details", _schema("pid")),
    FakeTool("get_order_list", _schema("status")),
    FakeTool("calculate_freight", _schema("pid")),
]


def _client(session=None, factory=None, token=_TOKEN, tools=None, **kwargs):
    if factory is None:
        if session is None:
            session = FakeSession(tools if tools is not None else _DEFAULT_TOOLS)
        factory = FakeSessionFactory(session)
    return CjMcpClient(
        token=token, base_url=_BASE, session_factory=factory, **kwargs
    ), factory


# ----------------------------------------------------------------------
# Token / endpoint handling
# ----------------------------------------------------------------------


def test_client_without_a_token_is_unconfigured():
    client = CjMcpClient(token=None, base_url=_BASE)
    assert client.configured is False


def test_empty_token_reads_as_unconfigured():
    # The shipped .env placeholder is an empty string, not a credential.
    assert CjMcpClient(token="", base_url=_BASE).configured is False
    assert CjMcpClient(token="   ", base_url=_BASE).configured is False


async def test_unconfigured_client_raises_before_any_connection():
    factory = FakeSessionFactory(FakeSession(_DEFAULT_TOOLS))
    client = CjMcpClient(token=None, base_url=_BASE, session_factory=factory)

    with pytest.raises(CjMcpNotConfiguredError):
        await client.connect()
    with pytest.raises(CjMcpNotConfiguredError):
        _ = client.endpoint_url
    assert factory.endpoints == []  # never touched the wire


def test_endpoint_url_embeds_the_token_in_the_path():
    client = CjMcpClient(token=_TOKEN, base_url=_BASE)
    assert client.endpoint_url == f"{_BASE}/{_TOKEN}"


def test_endpoint_url_strips_a_trailing_slash_on_the_base():
    client = CjMcpClient(token=_TOKEN, base_url=_BASE + "/")
    assert client.endpoint_url == f"{_BASE}/{_TOKEN}"


def test_redacted_endpoint_never_leaks_the_token():
    client = CjMcpClient(token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in client.redacted_endpoint
    assert client.redacted_endpoint == f"{_BASE}/***"


async def test_connect_uses_the_token_bearing_endpoint():
    client, factory = _client()
    async with client:
        pass
    assert factory.endpoints == [f"{_BASE}/{_TOKEN}"]


# ----------------------------------------------------------------------
# Connection lifecycle & taxonomy
# ----------------------------------------------------------------------


async def test_connect_is_idempotent_and_closes_once():
    client, factory = _client()
    await client.connect()
    await client.connect()  # second call is a no-op
    assert factory.entered == 1
    assert client.connected is True
    await client.aclose()
    assert factory.exited == 1
    assert client.connected is False


async def test_session_factory_failure_raises_connection_error():
    factory = FakeSessionFactory(error=RuntimeError("connection refused"))
    client = CjMcpClient(token=_TOKEN, base_url=_BASE, session_factory=factory)

    with pytest.raises(CjMcpConnectionError, match="connection refused"):
        await client.connect()


async def test_handshake_failure_raises_connection_error():
    session = FakeSession(_DEFAULT_TOOLS, list_tools_error=RuntimeError("401 unauthorized"))
    client, _ = _client(session=session)

    with pytest.raises(CjMcpConnectionError, match="401 unauthorized"):
        await client.connect()


async def test_tool_calls_before_connect_raise_connection_error():
    client, _ = _client()
    with pytest.raises(CjMcpConnectionError, match="not connected"):
        await client.search_products("anything")


async def test_aclose_releases_the_session_without_raising():
    client, factory = _client()
    await client.connect()
    await client.aclose()
    assert factory.exited == 1
    # A second close is harmless.
    await client.aclose()


# ----------------------------------------------------------------------
# tools/list discovery
# ----------------------------------------------------------------------


async def test_tool_catalog_is_discovered_on_connect():
    client, _ = _client()
    async with client:
        assert client.tool_names == [
            "calculate_freight",
            "get_order_list",
            "query_sku_details",
            "search_products",
        ]
        assert client._search_tool.name == "search_products"
        assert client._sku_detail_tool.name == "query_sku_details"


async def test_list_tools_is_called_once_per_connection():
    session = FakeSession(_DEFAULT_TOOLS)
    client, _ = _client(session=session)
    async with client:
        pass
    assert session.list_tools_calls == 1


async def test_missing_search_tool_raises_tool_error():
    tools = [FakeTool("get_order_list", _schema("status"))]
    client, _ = _client(tools=tools)
    with pytest.raises(CjMcpToolError, match="search_products"):
        await client.connect()


async def test_missing_sku_detail_tool_raises_tool_error():
    tools = [FakeTool("search_products", _search_schema())]
    client, _ = _client(tools=tools)
    with pytest.raises(CjMcpToolError, match="get_product_detail"):
        await client.connect()


async def test_empty_tool_catalog_raises_tool_error():
    client, _ = _client(tools=[])
    with pytest.raises(CjMcpToolError):
        await client.connect()


async def test_renamed_search_tool_is_bound_by_alias():
    # A server that renames the tool still binds deterministically.
    tools = [
        FakeTool("search_products_v2", _search_schema()),
        FakeTool("query_sku_details", _schema("pid")),
    ]
    client, _ = _client(tools=tools)
    async with client:
        assert client._search_tool.name == "search_products_v2"


async def test_unknown_tool_names_are_not_guessed():
    tools = [
        FakeTool("list_all_the_things", _schema("keyword")),
        FakeTool("query_sku_details", _schema("pid")),
    ]
    client, _ = _client(tools=tools)
    with pytest.raises(CjMcpToolError):
        await client.connect()


# ----------------------------------------------------------------------
# Parameter mapping onto the discovered schema
# ----------------------------------------------------------------------


def test_build_tool_arguments_maps_logical_names_onto_declared_ones():
    arguments = build_tool_arguments(
        _search_schema(),
        {
            "keyword": "kitchen gadgets",
            "global_warehouse": True,
            "inventory_available": True,
            "warehouse_country": "CN",
            "limit": 10,
            "price_min": None,
            "price_max": None,
        },
    )
    assert arguments == {
        "keyword": "kitchen gadgets",
        "isWarehouse": True,
        "startWarehouseInventory": 1,
        "countryCode": "CN",
        "pageSize": 10,
    }


def test_build_tool_arguments_only_sends_declared_parameters():
    # A schema declaring only a keyword must not receive the other filters.
    arguments = build_tool_arguments(
        _schema("keyword"),
        {"keyword": "gadgets", "global_warehouse": True, "limit": 10},
    )
    assert arguments == {"keyword": "gadgets"}


def test_build_tool_arguments_coerces_to_the_declared_type():
    schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "onlyAvailable": {"type": "boolean"},
            "q": {"type": "string"},
        },
    }
    arguments = build_tool_arguments(
        schema, {"limit": "10", "inventory_available": 1, "keyword": "gadgets"}
    )
    assert arguments == {"limit": 10, "onlyAvailable": True, "q": "gadgets"}


def test_boolean_inventory_flag_becomes_one_for_a_numeric_field():
    # CJ's startWarehouseInventory is a NUMBER ("minimum stock >= 1"), so a
    # boolean logical value must serialize as 1, never as `true`.
    arguments = build_tool_arguments(
        {"type": "object", "properties": {"startWarehouseInventory": {"type": "number"}}},
        {"inventory_available": True},
    )
    assert arguments == {"startWarehouseInventory": 1}


def test_price_bounds_map_onto_min_and_max_price():
    arguments = build_tool_arguments(
        {
            "type": "object",
            "properties": {"minPrice": {"type": "number"}, "maxPrice": {"type": "number"}},
        },
        {"price_min": 3, "price_max": 25.5},
    )
    assert arguments == {"minPrice": 3.0, "maxPrice": 25.5}


def test_build_tool_arguments_returns_nothing_without_a_schema():
    assert build_tool_arguments(None, {"keyword": "gadgets"}) == {}
    assert build_tool_arguments({}, {"keyword": "gadgets"}) == {}


async def test_search_serializes_keyword_warehouse_stock_and_limit():
    session = FakeSession(_DEFAULT_TOOLS)
    client, _ = _client(session=session)
    async with client:
        await client.search_products("kitchen gadgets", limit=7)

    name, arguments = session.calls[0]
    assert name == "search_products"
    assert arguments["keyword"] == "kitchen gadgets"
    assert arguments["isWarehouse"] is True
    assert arguments["countryCode"] == "CN"
    assert arguments["startWarehouseInventory"] == 1
    assert arguments["pageSize"] == 7


async def test_search_defaults_to_the_china_warehouse():
    session = FakeSession(_DEFAULT_TOOLS)
    client, _ = _client(session=session)
    async with client:
        await client.search_products("gadgets")
    arguments = session.calls[0][1]
    assert arguments["countryCode"] == "CN"
    assert arguments["isWarehouse"] is True


async def test_search_without_a_mappable_schema_raises_tool_error():
    tools = [
        FakeTool("search_products", {"type": "object", "properties": {}}),
        FakeTool("query_sku_details", _schema("pid")),
    ]
    session = FakeSession(tools)
    client, _ = _client(session=session)
    with pytest.raises(CjMcpToolError, match="no parameter"):
        async with client:
            await client.search_products("gadgets")
    assert session.calls == []


async def test_sku_detail_serializes_the_product_id():
    session = FakeSession(_DEFAULT_TOOLS)
    client, _ = _client(session=session)
    async with client:
        await client.query_sku_details("2097572473520115714")
    assert session.calls[0] == ("query_sku_details", {"pid": "2097572473520115714"})


async def test_sku_detail_binds_an_alternative_id_parameter_name():
    tools = [
        FakeTool("search_products", _search_schema()),
        FakeTool("query_sku_details", _schema("productId")),
    ]
    session = FakeSession(tools)
    client, _ = _client(session=session)
    async with client:
        await client.query_sku_details("42")
    assert session.calls[0] == ("query_sku_details", {"productId": "42"})


# ----------------------------------------------------------------------
# Tool-call failures
# ----------------------------------------------------------------------


async def test_tool_call_exception_maps_to_tool_error():
    session = FakeSession(
        _DEFAULT_TOOLS, responses={"search_products": [RuntimeError("boom")]}
    )
    client, _ = _client(session=session)
    with pytest.raises(CjMcpToolError, match="boom"):
        async with client:
            await client.search_products("gadgets")


async def test_tool_error_result_maps_to_tool_error():
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "search_products": [
                FakeCallToolResult(text="invalid parameter: warehouse", is_error=True)
            ]
        },
    )
    client, _ = _client(session=session)
    with pytest.raises(CjMcpToolError, match="invalid parameter"):
        async with client:
            await client.search_products("gadgets")


async def test_rate_limited_tool_call_is_retried_once(monkeypatch):
    monkeypatch.setattr(cc, "_TOOL_RETRY_BACKOFF_SECONDS", 0.01)
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "search_products": [
                FakeCallToolResult(text="HTTP 429 too many requests", is_error=True),
                FakeCallToolResult(
                    text=json.dumps(
                        {"data": {"list": [{"pid": "1", "productNameEn": "X"}]}}
                    )
                ),
            ]
        },
    )
    client, _ = _client(session=session)
    async with client:
        products = await client.search_products("gadgets")

    assert len(products) == 1
    assert len(session.calls) == 2  # the 429, then the success


async def test_persistent_rate_limit_surfaces_tool_error(monkeypatch):
    monkeypatch.setattr(cc, "_TOOL_RETRY_BACKOFF_SECONDS", 0.01)
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "search_products": [
                FakeCallToolResult(text="HTTP 429 too many requests", is_error=True)
            ]
        },
    )
    client, _ = _client(session=session)
    with pytest.raises(CjMcpToolError, match="429"):
        async with client:
            await client.search_products("gadgets")


# ----------------------------------------------------------------------
# Result parsing
# ----------------------------------------------------------------------


def test_structured_content_is_preferred():
    result = FakeCallToolResult(
        text="ignored", structured={"data": {"list": [{"pid": "1"}]}}
    )
    assert extract_payload(result) == {"data": {"list": [{"pid": "1"}]}}


def test_json_text_content_is_decoded():
    result = FakeCallToolResult(text='{"data": {"list": [{"pid": "1"}]}}')
    assert extract_payload(result) == {"data": {"list": [{"pid": "1"}]}}


def test_non_json_text_content_is_returned_as_text():
    assert extract_payload(FakeCallToolResult(text="<html>nope</html>")) == (
        "<html>nope</html>"
    )


def test_empty_result_yields_no_payload():
    assert extract_payload(FakeCallToolResult()) is None


def test_iter_product_dicts_walks_the_cj_envelope():
    payload = {
        "code": 200,
        "result": True,
        "data": {"list": [{"pid": "1", "productNameEn": "A"}]},
    }
    assert iter_product_dicts(payload) == [{"pid": "1", "productNameEn": "A"}]


def test_iter_product_dicts_handles_json_encoded_strings():
    payload = '{"data": {"list": [{"pid": "7", "productNameEn": "B"}]}}'
    assert iter_product_dicts(payload) == [{"pid": "7", "productNameEn": "B"}]


def test_iter_product_dicts_ignores_non_product_entries():
    payload = {"data": {"list": [None, "junk", {"no_id": 1}, {"pid": "3"}]}}
    assert iter_product_dicts(payload) == [{"pid": "3"}]


async def test_search_returns_the_parsed_product_records():
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "search_products": [
                FakeCallToolResult(
                    text=json.dumps(
                        {
                            "code": 200,
                            "result": True,
                            "data": {
                                "list": [
                                    {"pid": "1", "productNameEn": "Caddy"},
                                    {"pid": "2", "productNameEn": "Spine"},
                                ]
                            },
                        }
                    )
                )
            ]
        },
    )
    client, _ = _client(session=session)
    async with client:
        products = await client.search_products("caddy")
    assert [p["pid"] for p in products] == ["1", "2"]


async def test_sku_details_returns_a_single_product_record():
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "query_sku_details": [
                FakeCallToolResult(
                    text=json.dumps(
                        {
                            "code": 200,
                            "result": True,
                            "data": {"pid": "42", "productNameEn": "Caddy"},
                        }
                    )
                )
            ]
        },
    )
    client, _ = _client(session=session)
    async with client:
        detail = await client.query_sku_details("42")
    assert detail["pid"] == "42"
    assert detail["productNameEn"] == "Caddy"


async def test_empty_sku_detail_payload_returns_an_empty_dict():
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={"query_sku_details": [FakeCallToolResult(text="{}")]},
    )
    client, _ = _client(session=session)
    async with client:
        assert await client.query_sku_details("42") == {}


# ----------------------------------------------------------------------
# Loose JSON decoding (CJ prefixes every tool body with prose)
# ----------------------------------------------------------------------


def test_loose_json_decodes_a_prose_prefixed_body():
    # The live shape: "📋 Found 852 products total.\n\n{...}".
    text = '📋 Found 852 products total.\n\n{"pageSize": 20, "content": []}'
    assert decode_json_loosely(text) == {"pageSize": 20, "content": []}


def test_loose_json_decodes_a_prose_suffixed_body():
    assert decode_json_loosely('{"ok": true}\n\n✅ Done.') == {"ok": True}


def test_loose_json_decodes_arrays_and_bare_json():
    assert decode_json_loosely("[1, 2, 3]") == [1, 2, 3]
    assert decode_json_loosely('{"a": 1}') == {"a": 1}


def test_loose_json_returns_none_without_json():
    assert decode_json_loosely("no json here") is None
    assert decode_json_loosely("") is None
    assert decode_json_loosely(None) is None


def test_extract_payload_decodes_a_prose_prefixed_text_block():
    result = FakeCallToolResult(
        text='🔍 Product detail loaded.\n\n{"pid": "42", "sellPrice": "1.67"}'
    )
    assert extract_payload(result) == {"pid": "42", "sellPrice": "1.67"}


def test_iter_product_dicts_walks_the_live_search_envelope():
    # The live shape: {"content": [{"productList": [...]}]}.
    payload = {
        "pageSize": 20,
        "pageNumber": 1,
        "totalRecords": 6000,
        "content": [
            {"productList": [{"id": "1745360894529376256", "nameEn": "Steamer"}]}
        ],
    }
    assert iter_product_dicts(payload) == [
        {"id": "1745360894529376256", "nameEn": "Steamer"}
    ]


async def test_search_parses_a_prose_prefixed_live_response():
    session = FakeSession(
        _DEFAULT_TOOLS,
        responses={
            "search_products": [
                FakeCallToolResult(
                    text=(
                        "📋 Found 6000 products total.\n\n"
                        '{"pageSize": 20, "content": [{"productList": '
                        '[{"id": "1745360894529376256", "nameEn": "Steamer"}]}]}'
                    )
                )
            ]
        },
    )
    client, _ = _client(session=session)
    async with client:
        products = await client.search_products("kitchen gadgets")
    assert products == [{"id": "1745360894529376256", "nameEn": "Steamer"}]


async def test_get_product_detail_is_preferred_over_query_sku_details():
    # Both exist in the live catalog; query_sku_details returns [] for
    # catalog product ids, so the contract-resolving detail tool wins.
    tools = [
        FakeTool("search_products", _search_schema()),
        FakeTool("query_sku_details", _schema("productId")),
        FakeTool("get_product_detail", _schema("pid")),
    ]
    client, _ = _client(tools=tools)
    async with client:
        assert client._sku_detail_tool.name == "get_product_detail"


# ----------------------------------------------------------------------
# Search-hit / detail-payload merge
# ----------------------------------------------------------------------


def test_merge_lets_the_detail_record_win():
    hit = {"id": "1", "nameEn": "Hit title", "sellPrice": "2.38"}
    detail = {"pid": "1", "productNameEn": "Detail title", "sellPrice": "1.67"}
    merged = merge_product_payloads(hit, detail)
    assert merged["productNameEn"] == "Detail title"
    assert merged["sellPrice"] == "1.67"


def test_merge_carries_the_search_hit_stock_when_the_detail_has_none():
    # The live detail payload has no inventory at all, so the search hit's
    # warehouse figure is the only availability signal.
    hit = {"id": "1", "saleStatus": "3", "warehouseInventoryNum": 14092}
    detail = {"pid": "1", "status": "3", "variants": [{"inventoryNum": None}]}
    merged = merge_product_payloads(hit, detail)
    assert merged["warehouseInventoryNum"] == 14092
    assert is_mcp_payload_live(merged)[0] is True


def test_merge_never_lets_a_staler_hit_stock_rescue_a_zeroed_detail():
    hit = {"id": "1", "saleStatus": "3", "warehouseInventoryNum": 14092}
    detail = {
        "pid": "1",
        "status": "3",
        "variants": [
            {
                "inventoryNum": 0,
                "inventories": [
                    {"countryCode": "CN", "totalInventoryNum": 0, "cjInventoryNum": 0}
                ],
            }
        ],
    }
    merged = merge_product_payloads(hit, detail)
    alive, reason = is_mcp_payload_live(merged)
    assert alive is False
    assert "zero_stock" in reason


# ----------------------------------------------------------------------
# Field extraction helpers
# ----------------------------------------------------------------------


def test_product_page_url_is_the_canonical_pid_form():
    assert (
        product_page_url("2097572473520115714")
        == "https://cjdropshipping.com/product/2097572473520115714.html"
    )


def test_price_parsing_handles_numbers_strings_and_ranges():
    assert extract_price_usd({"sellPrice": 3.75}) == 3.75
    assert extract_price_usd({"sellPrice": "8.10"}) == 8.10
    # Range strings take the conservative low end.
    assert extract_price_usd({"sellPrice": "4.56 -- 4.92"}) == 4.56
    assert extract_price_usd({"price": "12.00-15.00"}) == 12.00


def test_price_parsing_rejects_missing_or_non_positive_values():
    assert extract_price_usd({}) is None
    assert extract_price_usd({"sellPrice": None}) is None
    assert extract_price_usd({"sellPrice": "free"}) is None
    assert extract_price_usd({"sellPrice": 0}) is None


def test_gallery_extraction_dedupes_and_keeps_order():
    payload = {
        "productImageSet": [
            "https://cdn.cj/a.jpg",
            "https://cdn.cj/b.jpg",
            {"url": "https://cdn.cj/c.jpg"},
            "https://cdn.cj/a.jpg",  # duplicate
            "not-a-url",
        ]
    }
    assert extract_gallery(payload) == [
        "https://cdn.cj/a.jpg",
        "https://cdn.cj/b.jpg",
        "https://cdn.cj/c.jpg",
    ]


def test_gallery_extraction_reads_variant_images():
    payload = {
        "variants": [
            {"variantImage": "https://cdn.cj/v1.jpg"},
            {"images": ["https://cdn.cj/v2.jpg"]},
        ],
        "productImageSet": ["https://cdn.cj/main.jpg"],
    }
    assert extract_gallery(payload) == [
        "https://cdn.cj/main.jpg",
        "https://cdn.cj/v1.jpg",
        "https://cdn.cj/v2.jpg",
    ]


def test_title_and_image_resolve_from_the_live_search_field_names():
    # CJ's search hits use `nameEn` for the title and `bigImage` for the
    # single thumbnail the search endpoint returns.
    from src.pipeline.cj_mcp_client import extract_title

    hit = {
        "id": "1745360894529376256",
        "nameEn": "Kitchen Plastic Microwave Steaming Container",
        "bigImage": "https://oss-cf.cjdropshipping.com/product/2024/01/11/x.jpg",
    }
    assert extract_title(hit) == "Kitchen Plastic Microwave Steaming Container"
    assert extract_gallery(hit) == [
        "https://oss-cf.cjdropshipping.com/product/2024/01/11/x.jpg"
    ]


def test_product_url_prefers_cjs_own_pdp_link():
    from src.pipeline.cj_mcp_client import extract_product_url

    # CJ publishes UUID-pid listings under the legacy slug form; that link
    # is supplier data and is used verbatim.
    slug = (
        "https://www.cjdropshipping.com/product/"
        "kitchen-gadgets-garlic-peeler-p-B03F2DFF-276D-481C-AD18-28DF22E411CC.html"
    )
    assert extract_product_url({"productUrl": slug}) == slug


def test_product_url_ignores_non_pdp_links():
    from src.pipeline.cj_mcp_client import extract_product_url

    assert extract_product_url({}) is None
    assert extract_product_url({"productUrl": "https://cjdropshipping.com/search?q=x"}) is None
    assert extract_product_url({"productUrl": "not a url"}) is None


def test_product_id_resolves_from_the_live_search_field_name():
    from src.pipeline.cj_mcp_client import extract_pid

    assert extract_pid({"id": "1745360894529376256"}) == "1745360894529376256"
    assert extract_pid({"pid": "abc"}) == "abc"
    assert extract_pid({"nameEn": "no id"}) is None


def test_warehouse_inventory_num_is_read_as_stock():
    from src.pipeline.cj_mcp_client import extract_stock

    assert extract_stock({"warehouseInventoryNum": 14092}) == 14092.0
    # The ...Num-suffixed inventory response fields count too.
    assert extract_stock(
        {"inventories": [{"countryCode": "CN", "totalInventoryNum": 10475}]}
    ) == 10475.0
    # A product with no recognised inventory field is unverifiable, not zero.
    assert extract_stock({"listedNum": 2607, "totalVerifiedInventory": 0}) is None


# ----------------------------------------------------------------------
# Token redaction
# ----------------------------------------------------------------------


def test_redacting_filter_scrubs_the_token_from_records():
    from src.pipeline.cj_mcp_client import TokenRedactingFilter

    redactor = TokenRedactingFilter(_TOKEN)
    record = logging.LogRecord(
        name="httpx2",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", f"{_BASE}/{_TOKEN}", "HTTP/1.1", 200, ""),
        exc_info=None,
    )
    assert redactor.filter(record) is True
    rendered = record.getMessage()
    assert _TOKEN not in rendered
    assert "***" in rendered
    assert _BASE in rendered  # the endpoint itself is still diagnosable


def test_redacting_filter_scrubs_non_string_log_arguments():
    # httpx logs a URL *object*, and the token only appears once the handler
    # stringifies it — the filter has to stringify first to catch it.
    from src.pipeline.cj_mcp_client import TokenRedactingFilter

    class _Url:
        def __init__(self, url):
            self._url = url

        def __str__(self):
            return self._url

    redactor = TokenRedactingFilter(_TOKEN)
    record = logging.LogRecord(
        name="httpx2",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d"',
        args=("POST", _Url(f"{_BASE}/{_TOKEN}"), "HTTP/1.1", 200),
        exc_info=None,
    )
    redactor.filter(record)
    rendered = record.getMessage()
    assert _TOKEN not in rendered
    assert "***" in rendered


def test_redacting_filter_leaves_unrelated_records_untouched():
    from src.pipeline.cj_mcp_client import TokenRedactingFilter

    redactor = TokenRedactingFilter(_TOKEN)
    record = logging.LogRecord(
        name="httpx2",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s %s",
        args=("GET", "https://cdn.cjdropshipping.com/a.jpg"),
        exc_info=None,
    )
    redactor.filter(record)
    assert record.getMessage() == "HTTP Request: GET https://cdn.cjdropshipping.com/a.jpg"


def test_install_token_redaction_is_idempotent(monkeypatch):
    from src.pipeline import cj_mcp_client as module

    monkeypatch.setattr(module, "_REDACTION_INSTALLED", set())
    module.install_token_redaction("tok-idempotent")
    module.install_token_redaction("tok-idempotent")
    attached = [
        f
        for name in module._REDACTED_LOGGER_NAMES
        for f in logging.getLogger(name).filters
        if getattr(f, "secret", None) == "tok-idempotent"
    ]
    # One filter per logger, not one per call.
    assert len(attached) == len(module._REDACTED_LOGGER_NAMES)


def test_install_token_redaction_ignores_an_empty_token():
    from src.pipeline import cj_mcp_client as module

    module.install_token_redaction(None)
    module.install_token_redaction("")


async def test_connect_installs_token_redaction():
    from src.pipeline import cj_mcp_client as module

    token = "tok-connect-redaction"
    client, _ = _client(token=token)
    await client.connect()
    try:
        attached = [
            f
            for name in module._REDACTED_LOGGER_NAMES
            for f in logging.getLogger(name).filters
            if getattr(f, "secret", None) == token
        ]
        assert attached
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# MCP Payload Liveness Gate
# ----------------------------------------------------------------------


def _live_payload(**overrides):
    """A payload shaped like a stocked, active CJ product."""
    payload = {
        "pid": "2097572473520115714",
        "productNameEn": "Kitchen Sink Caddy",
        "saleStatus": 3,
        "entryStatus": 1,
        "variants": [
            {
                "vid": "456",
                "variantSku": "CJCF292182201AZ",
                "inventoryNum": 200,
                "inventories": [
                    {"countryCode": "AU", "totalInventory": 200, "cjInventory": 200}
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_gate_accepts_an_active_stocked_payload():
    alive, reason = is_mcp_payload_live(_live_payload())
    assert alive is True
    assert "stock=200" in reason


def test_gate_drops_an_empty_payload():
    for empty in ({}, None, []):
        alive, reason = is_mcp_payload_live(empty)
        assert alive is False
        assert "empty_mcp_payload" in reason


def test_gate_drops_a_delist_flag():
    alive, reason = is_mcp_payload_live(_live_payload(isDeleted=True))
    assert alive is False
    assert "delist_flag" in reason


def test_gate_ignores_a_falsy_delist_flag():
    alive, _ = is_mcp_payload_live(_live_payload(isDeleted=False))
    assert alive is True


def test_gate_drops_an_inactive_sale_status():
    alive, reason = is_mcp_payload_live(_live_payload(saleStatus=2))
    assert alive is False
    assert "saleStatus" in reason


def test_gate_drops_an_inactive_entry_status():
    alive, reason = is_mcp_payload_live(_live_payload(entryStatus=0))
    assert alive is False
    assert "entryStatus" in reason


def test_gate_drops_a_payload_with_an_ambiguous_status_value():
    alive, reason = is_mcp_payload_live(_live_payload(saleStatus="unknown"))
    assert alive is False
    assert "ambiguous" in reason


def test_gate_drops_a_falsy_listing_flag():
    payload = _live_payload()
    payload.pop("saleStatus")
    payload.pop("entryStatus")
    alive, reason = is_mcp_payload_live(payload, target_country="AU")
    assert alive is False  # no boolean flag either -> no active signal
    assert "no_active_status_signal" in reason

    alive, reason = is_mcp_payload_live(dict(payload, isActive=False))
    assert alive is False
    assert "falsy" in reason


def test_gate_accepts_a_truthy_listing_flag():
    payload = _live_payload()
    payload.pop("saleStatus")
    payload.pop("entryStatus")
    alive, _ = is_mcp_payload_live(dict(payload, isActive=True))
    assert alive is True


def test_gate_drops_when_all_variants_are_out_of_stock():
    payload = _live_payload(
        variants=[
            {
                "variantSku": "X",
                "inventoryNum": 0,
                "inventories": [
                    {"countryCode": "AU", "totalInventory": 0, "cjInventory": 0}
                ],
            }
        ]
    )
    alive, reason = is_mcp_payload_live(payload)
    assert alive is False
    assert "zero_stock" in reason


def test_gate_drops_a_payload_with_no_availability_signal():
    # Inverted tolerance: the retired REST gate treated unpopulated stock as
    # unverifiable-not-dead; the MCP gate drops it outright.
    payload = _live_payload(variants=[{"variantSku": "X"}])
    alive, reason = is_mcp_payload_live(payload)
    assert alive is False
    assert "no_availability_signal" in reason


def test_gate_drops_a_payload_missing_status_entirely():
    payload = {"pid": "1", "inventoryNum": 50}
    alive, reason = is_mcp_payload_live(payload)
    assert alive is False
    assert "no_active_status_signal" in reason


def test_gate_accepts_stock_anywhere_when_the_target_country_has_none():
    payload = _live_payload(
        variants=[
            {
                "variantSku": "X",
                "inventories": [
                    {"countryCode": "US", "totalInventory": 12, "cjInventory": 12}
                ],
            }
        ]
    )
    alive, reason = is_mcp_payload_live(payload, target_country="AU")
    assert alive is True
    assert "stock=12" in reason


def test_gate_prefers_target_country_stock():
    payload = _live_payload(
        variants=[
            {
                "variantSku": "X",
                "inventories": [
                    {"countryCode": "US", "totalInventory": 900, "cjInventory": 900},
                    {"countryCode": "AU", "totalInventory": 4, "cjInventory": 4},
                ],
            }
        ]
    )
    alive, reason = is_mcp_payload_live(payload, target_country="AU")
    assert alive is True
    assert "stock=4" in reason


def test_gate_reads_string_encoded_stock():
    alive, _ = is_mcp_payload_live(_live_payload(inventoryNum="200"))
    assert alive is True


def test_gate_does_not_read_a_boolean_flag_as_stock():
    # `available: true` is a listing flag, never a quantity — with no real
    # inventory field the payload stays unverifiable and is dropped.
    payload = {
        "pid": "1",
        "saleStatus": 3,
        "available": True,
    }
    alive, reason = is_mcp_payload_live(payload)
    assert alive is False
    assert "no_availability_signal" in reason


def test_gate_accepts_top_level_stock_fields():
    payload = {"pid": "1", "saleStatus": 3, "stock": 25}
    alive, reason = is_mcp_payload_live(payload)
    assert alive is True
    assert "stock=25" in reason
