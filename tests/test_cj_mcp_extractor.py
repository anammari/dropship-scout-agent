"""Hermetic unit tests for `src.extractors.cj_mcp_extractor`.

A scripted stand-in for `CjMcpClient` (and, for one end-to-end case, the
real client driven by a fake MCP session) covers the extractor's contract:
search parameters actually sent, the MCP Payload Liveness Gate wired
through to candidate drops, gallery/price/title mapping into
`RawSupplierProduct`, and the chain exception mapping onto the base
extractor taxonomy. Zero network.
"""

import pytest

from src.config import settings
from src.extractors.base import (
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
)
from src.extractors.cj_mcp_extractor import CjMcpExtractor
from src.pipeline.cj_mcp_client import (
    CjMcpClient,
    CjMcpConnectionError,
    CjMcpNotConfiguredError,
    CjMcpToolError,
)

_PID = "2097572473520115714"
_PDP = f"https://cjdropshipping.com/product/{_PID}.html"

_GALLERY = [
    "https://oss-cf.cjdropshipping.com/p/one.jpeg",
    "https://oss-cf.cjdropshipping.com/p/two.jpeg",
    "https://oss-cf.cjdropshipping.com/p/three.jpeg",
]


def _search_hit(pid=_PID, title="Kitchen Sink Caddy Organiser", listed=500):
    """A search hit shaped like CJ's real search_products record.

    `listedNum` is carried because it is the CJ commercial gate's only
    metric — the same record the live catalogue returns. `listed=None`
    models a payload that reports no listing count at all.
    """
    hit = {
        "id": pid,
        "nameEn": title,
        "sku": "CJCF292182201AZ",
        "sellPrice": "4.56",
        "saleStatus": "3",
        "warehouseInventoryNum": 14092,
        "bigImage": _GALLERY[0],
    }
    if listed is not None:
        hit["listedNum"] = listed
    return hit


def _detail_payload(pid=_PID, **overrides):
    """A detail payload shaped like CJ's real get_product_detail record."""
    payload = {
        "pid": pid,
        "productNameEn": "Kitchen Sink Caddy Organiser",
        "status": "3",
        "description": "<p>Stainless steel sink caddy.</p>",
        "sellPrice": "13.00",
        "productImageSet": list(_GALLERY),
        "variants": [
            {
                "vid": "456",
                "variantSku": "CJCF292182201AZ",
                "variantSellPrice": 13.00,
                "inventoryNum": None,
                "inventories": None,
            }
        ],
    }
    payload.update(overrides)
    return payload


_FREIGHT_QUOTES = [
    {"method": "CJPacket Eub", "price_usd": 9.37, "transit_days": "6-10"},
    {"method": "CJPacket Ordinary", "price_usd": 12.23, "transit_days": "4-8"},
    {"method": "DHL Official", "price_usd": 33.54, "transit_days": "4-6"},
]

_FREIGHT_TIP_QUOTES = [
    {"method": "CJPacket Eub", "price_usd": 9.37, "transit_days": "6-10"},
]


class FakeMcpClient:
    """Scriptable stand-in for `CjMcpClient` (an async context manager)."""

    def __init__(
        self,
        hits=None,
        details=None,
        search_error=None,
        connect_error=None,
        configured=True,
        freight=None,
        freight_error=None,
        freight_tip=None,
        freight_tip_error=None,
    ):
        self.configured = configured
        self._hits = list(hits or [])
        self._details = dict(details or {})
        self._search_error = search_error
        self._connect_error = connect_error
        # A freight quote is mandatory for every emitted product, so the
        # default is a real quote; `freight=None` is how a test asks for the
        # "CJ quoted nothing" case.
        self._freight = _FREIGHT_QUOTES if freight is None else freight
        self._freight_error = freight_error
        self._freight_tip = (
            _FREIGHT_TIP_QUOTES if freight_tip is None else freight_tip
        )
        self._freight_tip_error = freight_tip_error
        self.search_calls = []
        self.detail_calls = []
        self.freight_calls = []
        self.freight_tip_calls = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        if self._connect_error is not None:
            raise self._connect_error
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        return False

    async def search_products(self, keyword, **kwargs):
        self.search_calls.append((keyword, kwargs))
        if self._search_error is not None:
            raise self._search_error
        return [dict(hit) for hit in self._hits]

    async def query_sku_details(self, pid):
        self.detail_calls.append(pid)
        entry = self._details.get(pid)
        if isinstance(entry, Exception):
            raise entry
        return dict(entry) if entry is not None else {}

    async def calculate_freight(self, vid, country, **kwargs):
        self.freight_calls.append((vid, country))
        if self._freight_error is not None:
            raise self._freight_error
        return [dict(quote) for quote in self._freight]

    async def calculate_freight_tip(self, weight_grams, product_props, country, **kwargs):
        self.freight_tip_calls.append((weight_grams, list(product_props), country))
        if self._freight_tip_error is not None:
            raise self._freight_tip_error
        return [dict(quote) for quote in self._freight_tip]


# ----------------------------------------------------------------------
# Happy path: mapping into RawSupplierProduct
# ----------------------------------------------------------------------


async def test_clean_hit_becomes_a_raw_supplier_product():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    products = await CjMcpExtractor(client=client).fetch_products(["sink caddy"])

    assert len(products) == 1
    product = products[0]
    assert product.supplier_name == "CJdropshipping"
    assert product.supplier_retail_url == _PDP
    assert product.product_title == "Kitchen Sink Caddy Organiser"
    assert len(product.image_urls) == 3
    # Freight is quoted, never assumed: the cheapest offered method (USD
    # 9.37) converted by the same static rate as the listed price.
    assert product.shipping_cost_aud == round(9.37 * 1.55, 2)
    assert product.shipping_method == "CJPacket Eub"
    assert product.shipping_transit_days == "6-10"


async def test_listed_usd_price_is_converted_to_aud():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload(sellPrice=13.00)}
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].price_aud == round(13.00 * 1.55, 2)


async def test_price_range_takes_the_conservative_low_end():
    # The search hit's "4.56 -- 4.92" is used when the detail payload does
    # not quote a price of its own.
    hit = _search_hit()
    hit["sellPrice"] = "4.56 -- 4.92"
    detail = _detail_payload()
    detail.pop("sellPrice")
    client = FakeMcpClient(hits=[hit], details={_PID: detail})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].price_aud == round(4.56 * 1.55, 2)


async def test_description_is_html_stripped():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].product_description == "Stainless steel sink caddy."


async def test_title_falls_back_to_the_search_hit():
    detail = _detail_payload()
    detail.pop("productNameEn")
    client = FakeMcpClient(
        hits=[_search_hit(title="Search Title")], details={_PID: detail}
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].product_title == "Search Title"


async def test_detail_payload_wins_over_the_search_hit():
    detail = _detail_payload(productNameEn="Detail Title")
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: detail})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].product_title == "Detail Title"


async def test_uuid_pid_hit_maps_through_cjs_own_pdp_link():
    # Older CJ listings carry UUID pids; CJ publishes them under the legacy
    # slug form, which must survive validation.
    uuid_pid = "B03F2DFF-276D-481C-AD18-28DF22E411CC"
    slug = (
        "https://www.cjdropshipping.com/product/"
        f"kitchen-gadgets-garlic-peeler-p-{uuid_pid}.html"
    )
    hit = _search_hit(pid=uuid_pid)
    hit["productUrl"] = slug
    detail = _detail_payload(pid=uuid_pid, productUrl=slug)
    client = FakeMcpClient(hits=[hit], details={uuid_pid: detail})

    products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert len(products) == 1
    assert products[0].supplier_retail_url == slug


async def test_uuid_pid_hit_falls_back_to_the_canonical_url():
    uuid_pid = "B03F2DFF-276D-481C-AD18-28DF22E411CC"
    client = FakeMcpClient(
        hits=[_search_hit(pid=uuid_pid)],
        details={uuid_pid: _detail_payload(pid=uuid_pid)},
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].supplier_retail_url == (
        f"https://cjdropshipping.com/product/{uuid_pid}.html"
    )


async def test_gallery_is_deduped_and_ordered():
    detail = _detail_payload(
        productImageSet=[_GALLERY[0], _GALLERY[1], _GALLERY[2], _GALLERY[0]]
    )
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: detail})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert products[0].image_urls == _GALLERY


# ----------------------------------------------------------------------
# Search call parameters
# ----------------------------------------------------------------------


async def test_search_sends_warehouse_country_and_inventory_filters():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    await CjMcpExtractor(client=client, max_products=4).fetch_products(
        ["kitchen gadgets"], country="AU"
    )

    keyword, kwargs = client.search_calls[0]
    assert keyword == "kitchen gadgets"
    # The China warehouse, per CJ's own intent mapping: isWarehouse +
    # countryCode=CN (countryCode is a WAREHOUSE filter on this tool).
    assert kwargs["warehouse_country"] == "CN"
    assert kwargs["global_warehouse"] is True
    assert kwargs["inventory_available"] is True
    assert kwargs["limit"] == 4


async def test_each_keyword_gets_its_own_search_call():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    await CjMcpExtractor(client=client).fetch_products(["alpha", "beta"])
    assert [call[0] for call in client.search_calls] == ["alpha", "beta"]


async def test_one_session_is_opened_for_the_whole_run():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    await CjMcpExtractor(client=client).fetch_products(["alpha", "beta"])
    assert client.entered == 1
    assert client.exited == 1


async def test_sku_detail_is_requested_for_every_hit():
    hit = _search_hit(pid="111")
    client = FakeMcpClient(hits=[hit], details={"111": _detail_payload(pid="111")})
    await CjMcpExtractor(client=client).fetch_products(["k"])
    assert client.detail_calls == ["111"]


# ----------------------------------------------------------------------
# MCP Payload Liveness Gate, wired through the extractor
# ----------------------------------------------------------------------


async def test_delisted_product_is_dropped():
    # The detail payload's own status field overrides the search hit's.
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload(status="2")}
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_inactive_sale_status_on_the_search_hit_drops_before_the_detail_call():
    hit = _search_hit()
    hit["saleStatus"] = "2"
    client = FakeMcpClient(hits=[hit], details={_PID: _detail_payload()})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []
    # The cheap pre-gate spared the detail round-trip.
    assert client.detail_calls == []


async def test_explicitly_removed_product_is_dropped():
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload(isDeleted=True)}
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_zero_inventory_search_hit_is_dropped():
    hit = _search_hit()
    hit["warehouseInventoryNum"] = 0
    client = FakeMcpClient(hits=[hit], details={_PID: _detail_payload()})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_zero_inventory_in_the_detail_payload_is_dropped():
    # A detail payload that positively reports zero stock is never rescued
    # by the staler search-hit figure.
    dead = _detail_payload(
        variants=[
            {
                "variantSku": "X",
                "inventoryNum": 0,
                "inventories": [
                    {
                        "countryCode": "CN",
                        "totalInventoryNum": 0,
                        "cjInventoryNum": 0,
                    }
                ],
            }
        ]
    )
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: dead})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_ambiguous_status_in_the_detail_payload_is_dropped():
    ambiguous = _detail_payload(status="something-unrecognised")
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: ambiguous})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_missing_availability_signal_everywhere_is_dropped():
    # No availability signal in either payload — strictly dropped under the
    # inverted tolerance, even though the listing status looks active.
    hit = _search_hit()
    hit.pop("warehouseInventoryNum")
    client = FakeMcpClient(
        hits=[hit], details={_PID: _detail_payload(variants=[{"variantSku": "X"}])}
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_empty_detail_payload_drops_the_candidate_immediately():
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: None})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_failed_detail_call_drops_only_that_candidate():
    good_pid = "222"
    client = FakeMcpClient(
        hits=[_search_hit(), _search_hit(pid=good_pid, title="Good One")],
        details={
            _PID: CjMcpToolError("sku detail unavailable"),
            good_pid: _detail_payload(pid=good_pid, productNameEn="Good One"),
        },
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert [p.product_title for p in products] == ["Good One"]


async def test_mixed_hits_keep_only_the_alive_ones():
    dead_pid = "999"
    client = FakeMcpClient(
        hits=[_search_hit(), _search_hit(pid=dead_pid, title="Dead Cooler")],
        details={
            _PID: _detail_payload(),
            dead_pid: _detail_payload(pid=dead_pid, status="2"),
        },
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])
    assert [p.product_title for p in products] == ["Kitchen Sink Caddy Organiser"]


# ----------------------------------------------------------------------
# CJ commercial "winning product" gate (plan §6.2)
# ----------------------------------------------------------------------


def _gate_extractor(client):
    return CjMcpExtractor(client=client)


def test_a_proven_listing_passes_the_commercial_gate():
    extractor = _gate_extractor(FakeMcpClient())
    assert extractor._passes_winner_gate(_PID, _search_hit(listed=150)) is True
    assert extractor._passes_winner_gate(_PID, _search_hit(listed=4175)) is True


def test_low_list_count_is_dropped_with_the_documented_log(caplog):
    extractor = _gate_extractor(FakeMcpClient())
    with caplog.at_level("WARNING"):
        passed = extractor._passes_winner_gate(_PID, _search_hit(listed=120))
    assert passed is False
    assert f"Skipping {_PID}: Insufficient CJ list count (120)" in caplog.text


def test_unreported_list_count_is_dropped_as_unproven(caplog):
    # Inverted tolerance: a hit that reports no listing count is unproven,
    # not "unverifiable but probably fine".
    extractor = _gate_extractor(FakeMcpClient())
    with caplog.at_level("WARNING"):
        passed = extractor._passes_winner_gate(_PID, _search_hit(listed=None))
    assert passed is False
    assert (
        f"Skipping {_PID}: Insufficient CJ list count (unavailable)"
        in caplog.text
    )


def test_the_gate_reads_the_configured_floor(monkeypatch):
    extractor = _gate_extractor(FakeMcpClient())
    monkeypatch.setattr(settings, "MIN_CJ_LISTED_COUNT", 2000)
    assert extractor._passes_winner_gate(_PID, _search_hit(listed=1500)) is False
    monkeypatch.setattr(settings, "MIN_CJ_LISTED_COUNT", 10)
    assert extractor._passes_winner_gate(_PID, _search_hit(listed=15)) is True


async def test_commercial_gate_runs_before_the_detail_call():
    # A commercially unproven hit must cost nothing — no detail round-trip
    # is spent on a listing the gate already rejected.
    weak_pid, good_pid = "333", "444"
    client = FakeMcpClient(
        hits=[
            _search_hit(pid=weak_pid, title="Weak", listed=20),
            _search_hit(pid=good_pid, title="Strong", listed=900),
        ],
        details={good_pid: _detail_payload(pid=good_pid, productNameEn="Strong")},
    )
    products = await _gate_extractor(client).fetch_products(["k"])

    assert [p.product_title for p in products] == ["Strong"]
    assert client.detail_calls == [good_pid]


async def test_the_keep_side_of_the_gate_is_logged_in_ranked_order(caplog):
    # The drop lines alone cannot show that the ranking happened, so the
    # survivors' counts are logged strongest-first.
    ranked = [("111", 200), ("222", 4175), ("333", 900)]
    client = FakeMcpClient(
        hits=[
            _search_hit(pid=pid, title=f"T{pid}", listed=listed)
            for pid, listed in ranked
        ],
        details={
            pid: _detail_payload(pid=pid, productNameEn=f"T{pid}")
            for pid, _ in ranked
        },
    )
    with caplog.at_level("INFO"):
        await _gate_extractor(client).fetch_products(["kitchen gadgets"])

    assert (
        "CJ commercial gate passed 3/3 hit(s) for 'kitchen gadgets' "
        "(listed counts, strongest first: [4175, 900, 200])"
    ) in caplog.text


async def test_hits_are_ranked_by_list_count_descending():
    # Survivors are expanded strongest-first, so the LLM meets the most
    # widely listed products before the target count fills.
    ranked = [("111", 200), ("222", 4175), ("333", 900)]
    client = FakeMcpClient(
        hits=[
            _search_hit(pid=pid, title=f"T{pid}", listed=listed)
            for pid, listed in ranked
        ],
        details={
            pid: _detail_payload(pid=pid, productNameEn=f"T{pid}")
            for pid, _ in ranked
        },
    )
    products = await _gate_extractor(client).fetch_products(["k"])

    assert [p.product_title for p in products] == ["T222", "T333", "T111"]
    assert client.detail_calls == ["222", "333", "111"]


async def test_the_gate_applies_to_every_keyword():
    # Each keyword's page is gated and ranked independently, so a run over
    # two keywords still yields a candidate per keyword.
    client = FakeMcpClient(
        hits=[_search_hit()], details={_PID: _detail_payload()}
    )
    products = await _gate_extractor(client).fetch_products(["alpha", "beta"])

    assert [call[0] for call in client.search_calls] == ["alpha", "beta"]
    assert len(products) == 2


# ----------------------------------------------------------------------
# Skip-don't-fabricate rules
# ----------------------------------------------------------------------


async def test_hit_without_a_pid_is_skipped_before_any_detail_call():
    client = FakeMcpClient(hits=[{"productNameEn": "No id here"}])
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []
    assert client.detail_calls == []


async def test_hit_without_a_usable_price_is_skipped():
    detail = _detail_payload()
    detail.pop("sellPrice")
    hit = _search_hit()
    hit.pop("sellPrice")
    client = FakeMcpClient(hits=[hit], details={_PID: detail})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_hit_without_a_title_is_skipped():
    detail = _detail_payload()
    detail.pop("productNameEn")
    hit = _search_hit()
    hit.pop("nameEn")
    client = FakeMcpClient(hits=[hit], details={_PID: detail})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_thin_gallery_is_skipped():
    thin = _detail_payload(productImageSet=_GALLERY[:2])
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: thin})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_gallery_with_duplicates_only_counts_distinct_images():
    dupes = _detail_payload(productImageSet=[_GALLERY[0]] * 4)
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: dupes})
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_search_returning_nothing_yields_no_products():
    client = FakeMcpClient(hits=[])
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


# ----------------------------------------------------------------------
# Chain exception mapping
# ----------------------------------------------------------------------


async def test_unconfigured_client_raises_not_configured():
    client = FakeMcpClient(configured=False)
    with pytest.raises(ExtractorNotConfiguredError) as excinfo:
        await CjMcpExtractor(client=client).fetch_products(["k"])
    assert excinfo.value.credential_name == "CJ_MCP_TOKEN"


async def test_connection_failure_maps_to_blocked():
    client = FakeMcpClient(
        connect_error=CjMcpConnectionError("endpoint unreachable")
    )
    with pytest.raises(ExtractorBlockedException, match="unreachable"):
        await CjMcpExtractor(client=client).fetch_products(["k"])


async def test_search_tool_failure_maps_to_blocked():
    client = FakeMcpClient(search_error=CjMcpToolError("tool exploded"))
    with pytest.raises(ExtractorBlockedException, match="tool exploded"):
        await CjMcpExtractor(client=client).fetch_products(["k"])


async def test_not_configured_error_from_the_client_maps_to_not_configured():
    client = FakeMcpClient(search_error=CjMcpNotConfiguredError("no token"))
    with pytest.raises(ExtractorNotConfiguredError):
        await CjMcpExtractor(client=client).fetch_products(["k"])


# ----------------------------------------------------------------------
# End-to-end through the real client (fake MCP session, no network)
# ----------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name, schema):
        self.name = name
        self.input_schema = schema


class _FakeResult:
    def __init__(self, payload):
        self.content = []
        self.structured_content = payload
        self.is_error = False


class _FakeSession:
    def __init__(self, tools, responses):
        self._tools = tools
        self._responses = responses
        self.calls = []

    async def list_tools(self):
        return type("R", (), {"tools": self._tools})()

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return _FakeResult(self._responses[name])


class _FakeFactory:
    def __init__(self, session):
        self.session = session
        self.endpoints = []

    def __call__(self, endpoint_url, read_timeout):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm():
            self.endpoints.append(endpoint_url)
            yield self.session

        return _cm()


async def test_end_to_end_through_the_real_mcp_client():
    """The real client + extractor, driven by a scripted MCP session."""
    # The live CJ search schema, plus the prose preamble CJ puts in front
    # of the JSON body of every tool response.
    schema = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "isWarehouse": {"type": "boolean"},
            "countryCode": {"type": "string"},
            "startWarehouseInventory": {"type": "number"},
            "pageSize": {"type": "number"},
        },
    }
    session = _FakeSession(
        tools=[
            _FakeTool("search_products", schema),
            _FakeTool(
                "get_product_detail",
                {"type": "object", "properties": {"pid": {"type": "string"}}},
            ),
            _FakeTool(
                "calculate_freight",
                {
                    "type": "object",
                    "properties": {
                        "endCountryCode": {"type": "string"},
                        "startCountryCode": {"type": "string"},
                        "products": {"type": "array"},
                    },
                },
            ),
            _FakeTool(
                "calculate_freight_tip",
                {
                    "type": "object",
                    "properties": {
                        "srcAreaCode": {"type": "string"},
                        "destAreaCode": {"type": "string"},
                        "productProp": {"type": "array"},
                        "weight": {"type": "number"},
                    },
                },
            ),
        ],
        responses={
            "search_products": {
                "pageSize": 10,
                "content": [{"productList": [_search_hit()]}],
            },
            "get_product_detail": _detail_payload(),
            # CJ's real freight shape: a flat list of methods with the name,
            # the USD price and the transit window.
            "calculate_freight": [
                {
                    "logisticName": "CJPacket Eub",
                    "logisticPrice": 9.37,
                    "logisticPriceCn": 57.99,
                    "logisticAging": "6-10",
                    "totalPostageFee": 9.37,
                },
                {
                    "logisticName": "DHL Official",
                    "logisticPrice": 33.54,
                    "logisticAging": "4-6",
                    "totalPostageFee": 33.54,
                },
            ],
        },
    )
    factory = _FakeFactory(session)
    client = CjMcpClient(
        token="tok", base_url="https://mcp.example/mcp", session_factory=factory
    )

    products = await CjMcpExtractor(client=client).fetch_products(
        ["kitchen gadgets"], country="AU"
    )

    assert len(products) == 1
    product = products[0]
    assert product.supplier_retail_url == _PDP
    assert product.price_aud == round(13.00 * 1.55, 2)
    assert len(product.image_urls) == 3
    assert product.shipping_cost_aud == round(9.37 * 1.55, 2)
    assert product.shipping_method == "CJPacket Eub"
    assert factory.endpoints == ["https://mcp.example/mcp/tok"]

    # The wire arguments are exactly what the discovered schema declares.
    search_name, search_arguments = session.calls[0]
    assert search_name == "search_products"
    assert search_arguments["keyword"] == "kitchen gadgets"
    assert search_arguments["isWarehouse"] is True
    assert search_arguments["countryCode"] == "CN"
    assert search_arguments["startWarehouseInventory"] == 1
    assert session.calls[1] == ("get_product_detail", {"pid": _PID})
    # Freight is quoted for the listing's variant, to the target country.
    freight_name, freight_arguments = session.calls[2]
    assert freight_name == "calculate_freight"
    assert freight_arguments["endCountryCode"] == "AU"
    assert freight_arguments["products"] == [{"vid": "456", "quantity": 1}]


async def test_end_to_end_drops_a_delisted_product():
    session = _FakeSession(
        tools=[
            _FakeTool(
                "search_products",
                {"type": "object", "properties": {"keyword": {"type": "string"}}},
            ),
            _FakeTool(
                "get_product_detail",
                {"type": "object", "properties": {"pid": {"type": "string"}}},
            ),
            _FakeTool(
                "calculate_freight",
                {"type": "object", "properties": {"products": {"type": "array"}}},
            ),
            _FakeTool(
                "calculate_freight_tip",
                {
                    "type": "object",
                    "properties": {
                        "productProp": {"type": "array"},
                        "weight": {"type": "number"},
                    },
                },
            ),
        ],
        responses={
            "search_products": {"content": [{"productList": [_search_hit()]}]},
            "get_product_detail": _detail_payload(status="2"),
        },
    )
    client = CjMcpClient(
        token="tok",
        base_url="https://mcp.example/mcp",
        session_factory=_FakeFactory(session),
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


# ----------------------------------------------------------------------
# Freight quoting (plan §6)
# ----------------------------------------------------------------------


async def test_the_cheapest_method_is_costed_by_default():
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: _detail_payload()})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert len(products) == 1
    # 9.37 beats CJPacket Ordinary (12.23) and DHL Official (33.54).
    assert products[0].shipping_method == "CJPacket Eub"
    assert products[0].shipping_cost_aud == round(9.37 * 1.55, 2)


async def test_freight_is_quoted_for_the_cheapest_variant_and_target_country():
    detail = _detail_payload(
        variants=[
            {"vid": "expensive", "variantSellPrice": 11.29},
            {"vid": "cheapest", "variantSellPrice": 4.89},
            {"vid": "middle", "variantSellPrice": 7.53},
        ]
    )
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: detail})
    await CjMcpExtractor(client=client).fetch_products(["k"], country="AU")

    # CJ quotes per variant and weight differs between them, so the quote
    # must name the variant being costed.
    assert client.freight_calls == [("cheapest", "AU")]


async def test_a_pinned_freight_method_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "CJ_FREIGHT_METHOD", "DHL Official")
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: _detail_payload()})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert products[0].shipping_method == "DHL Official"
    assert products[0].shipping_cost_aud == round(33.54 * 1.55, 2)


async def test_an_unoffered_pin_falls_back_to_the_cheapest(monkeypatch, caplog):
    monkeypatch.setattr(settings, "CJ_FREIGHT_METHOD", "Pigeon Post")
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: _detail_payload()})
    with caplog.at_level("WARNING"):
        products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert products[0].shipping_method == "CJPacket Eub"
    assert "Pigeon Post" in caplog.text


async def test_the_weight_trial_quotes_when_no_variant_id_exists():
    # Some listings expose no usable variant; the weight-based trial quotes
    # the same catalogue from weight + logistics attributes instead of
    # dropping the product.
    detail = _detail_payload(
        variants=[],
        productWeight="402.00-1194.00",
        productProEnSet='["COMMON"]',
    )
    client = FakeMcpClient(hits=[_search_hit()], details={_PID: detail})
    products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert client.freight_calls == []
    assert client.freight_tip_calls == [(402.0, ["COMMON"], "AU")]
    assert products[0].shipping_cost_aud == round(9.37 * 1.55, 2)


async def test_a_listing_with_no_quote_at_all_is_dropped():
    # Costing freight at zero is the understatement this step exists to
    # remove, so "no quote" is a drop, not a free pass.
    client = FakeMcpClient(
        hits=[_search_hit()],
        details={_PID: _detail_payload()},
        freight=[],
        freight_tip=[],
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_a_failed_variant_quote_falls_through_to_the_weight_trial():
    detail = _detail_payload(productWeight="410")
    client = FakeMcpClient(
        hits=[_search_hit()],
        details={_PID: detail},
        freight_error=CjMcpToolError("freight tool unavailable"),
    )
    products = await CjMcpExtractor(client=client).fetch_products(["k"])

    assert client.freight_tip_calls == [(410.0, [], "AU")]
    assert len(products) == 1


async def test_a_freight_failure_drops_the_candidate_without_killing_the_run():
    # A freight tool error must not become ExtractorBlockedException: that
    # would hand the whole keyword to the next engine over one unquotable
    # listing. The candidate drops on its own and the run completes.
    client = FakeMcpClient(
        hits=[_search_hit()],
        details={_PID: _detail_payload()},
        freight_error=CjMcpToolError("freight exploded"),
        freight_tip_error=CjMcpToolError("trial exploded"),
    )
    assert await CjMcpExtractor(client=client).fetch_products(["k"]) == []


async def test_a_pin_with_no_quote_still_drops_rather_than_assuming():
    client = FakeMcpClient(
        hits=[_search_hit()],
        details={_PID: _detail_payload()},
        freight=[],
        freight_tip=[],
    )
    assert await CjMcpExtractor(client=client, max_products=1).fetch_products(
        ["k"]
    ) == []
