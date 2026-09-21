"""Offline unit tests for the native AliExpress Dropshipping Center extractor.

Zero network: the MTOP search and item-record exchanges, the DS Center
landing page, and the Playwright PDP harvest are all scripted. The payload
shapes mirror the live DS Center responses (`selection.search` v2.0 and
`selection.queryByItemUrl` v1.0) captured during implementation.
"""

import pytest

from src.config import settings
from src.extractors import aliexpress_ds as ds
from src.extractors.aliexpress_ds import (
    AliExpressDsCenterExtractor,
    DsCenterSessionExpiredError,
    _canonical_product_url,
    _decode_payload,
    _ds_country_cookie,
    _ds_item_id,
    _is_login_wall,
    _parse_orders,
    _parse_rating,
    _price_aud,
)
from src.extractors.base import ExtractorBlockedException

_GOOD_ID = "1005010217898346"
_WEAK_ID = "1005007987710373"
_NO_RATING_ID = "1005013060942931"

_GALLERY = [
    "https://ae-pic-a1.aliexpress-media.com/kf/a.jpg",
    "https://ae-pic-a1.aliexpress-media.com/kf/b.jpg",
    "https://ae-pic-a1.aliexpress-media.com/kf/c.jpg",
]
_META = "A stainless steel garlic grater for the kitchen."


def _record(
    item_id: str = _GOOD_ID,
    *,
    name: str = "Stainless Steel 4-in-1 Manual Grater & Slicer",
    trade: str = "10000+ sold",
    score: str = "4.7",
    price: str = "US $7.22",
    cent="722",
    currency: str = "USD",
    pic: str = "https://ae-pic-a1.aliexpress-media.com/kf/main.jpg",
    url: str = None,
) -> dict:
    """One `queryByItemUrl` record, shaped like the live payload."""
    return {
        "itemId": item_id,
        "itemName": name,
        "itemUrl": url or f"https://aliexpress.com/item/{item_id}.html",
        "itemMainPic": pic,
        "discountMinPriceFormat": price,
        "rangePriceFormat": price,
        "tradeDesc": trade,
        "score": score,
        "originMinPriceFormatJson": {
            "structure": {"cent": cent, "currencyCode": currency}
        },
    }


def _search_hit(item_id: str = _GOOD_ID, **kwargs) -> dict:
    """One `selection.search` hit (the search page's own item shape)."""
    record = _record(item_id, **kwargs)
    return {
        "itemId": item_id,
        "itemName": record["itemName"],
        "itemUrl": record["itemUrl"],
        "itemMainPic": record["itemMainPic"],
        "tradeDesc": record["tradeDesc"],
        "score": record["score"],
    }


def _ok(payload: dict) -> dict:
    return {"ret": ["SUCCESS::调用成功"], "data": payload}


def _search_payload(items) -> dict:
    return _ok({"code": "", "data": {"data": items, "totalCount": len(items)}})


def _item_payload(record) -> dict:
    return _ok({"code": "", "message": "", "data": record})


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeResponse:
    """Playwright's `APIResponse.text()` is a coroutine; the fake mirrors that."""

    def __init__(self, body) -> None:
        self._body = body

    async def text(self):
        if isinstance(self._body, Exception):
            raise self._body
        if not isinstance(self._body, str):
            import json

            return json.dumps(self._body)
        return self._body


class _FakeRequest:
    """Scripted `context.request`: one scripted body per GET, calls recorded."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if not self.responses:
            return _FakeResponse({})
        return _FakeResponse(self.responses.pop(0))


class _FakePage:
    """Scripted PDP: gallery + meta description, or a login wall."""

    def __init__(self, url="https://ds.aliexpress.com/find-products", gallery=None,
                 meta=_META) -> None:
        self.url = url
        self.gallery = _GALLERY if gallery is None else gallery
        self.meta = meta
        self.gotos = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def evaluate(self, js):
        if "srcset" in js:
            return list(self.gallery)
        return self.meta

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, request=None, pages=None, token="token123") -> None:
        self.request = request or _FakeRequest()
        self._pages = list(pages or [])
        self._token = token
        self.added_cookies = []

    async def new_page(self):
        return self._pages.pop(0) if self._pages else _FakePage()

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    async def cookies(self, urls=None):
        if not self._token:
            return []
        return [
            {
                "name": "_m_h5_tk",
                "value": f"{self._token}_1789994481443",
                "domain": "acs.aliexpress.com",
                "path": "/",
            }
        ]


class _FakeBrowserCM:
    """Stands in for `_DsBrowserContext` at the extractor boundary."""

    def __init__(self, context, page=None) -> None:
        self.context = context
        self.page = page or _FakePage()

    async def __aenter__(self):
        return self.context, self.page

    async def __aexit__(self, *exc_info):
        return False


@pytest.fixture
def ali(monkeypatch):
    """Extractor wired to scripted MTOP responses and a scripted PDP."""
    monkeypatch.setattr(settings, "ALI_DS_MAX_PRODUCTS", 20)
    monkeypatch.setattr(settings, "MIN_DS_ORDER_COUNT", 500)
    monkeypatch.setattr(settings, "MIN_DS_RATING", 4.5)
    monkeypatch.setattr(settings, "USD_TO_AUD", 1.55)
    monkeypatch.setattr(settings, "USER_AGENT", "test-agent")
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", "nonexistent-state.json")

    def _install(*, records, hits=None, search_responses=None, gallery=None,
                 meta=_META, context=None):
        """Script one keyword's exchange and return the recorded request journal.

        Every MTOP call is a priming GET (token priming, unsigned) followed
        by the signed GET, so each one consumes two scripted bodies.
        """
        responses = list(search_responses) if search_responses else []
        if records is not None:
            responses.append({})  # search priming call
            responses.append(_search_payload(hits or [_search_hit(records[0]["itemId"])]))
            for record in records:
                responses.extend([{}, _item_payload(record)])
        pages = [_FakePage(gallery=gallery, meta=meta) for _ in records or [1]]
        ctx = context or _FakeContext(request=_FakeRequest(responses), pages=pages)
        monkeypatch.setattr(
            AliExpressDsCenterExtractor,
            "_browser_context",
            lambda self: _FakeBrowserCM(ctx),
        )
        return ctx

    return {"install": _install, "extractor": AliExpressDsCenterExtractor()}


# ----------------------------------------------------------------------
# Payload parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("148 sold", 148),
        ("10000+ sold", 10000),
        ("1000+ sold", 1000),
        ("1,234 sold", 1234),
        ("4 sold", 4),
        ("1 sold", 1),
        ("", None),
        (None, None),
        ("no data", None),
    ],
)
def test_order_count_is_read_from_the_trade_description(raw, expected):
    assert _parse_orders(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4.2", 4.2),
        ("4.9", 4.9),
        ("4", 4.0),
        ("", None),
        (None, None),
        ("0", None),
        ("not rated", None),
    ],
)
def test_rating_is_read_from_the_score(raw, expected):
    assert _parse_rating(raw) == expected


def test_plain_json_payload_decodes():
    assert _decode_payload('{"ret": ["SUCCESS::调用成功"]}') == {
        "ret": ["SUCCESS::调用成功"]
    }


def test_jsonp_wrapped_payload_decodes():
    # The DS Center's own UI requests `dataType=jsonp`, so a wrapped body can
    # arrive whichever way the call is made.
    body = 'mtopjsonp9({"ret": ["SUCCESS::调用成功"], "data": {}})'
    assert _decode_payload(body) == {"ret": ["SUCCESS::调用成功"], "data": {}}


@pytest.mark.parametrize("raw", [None, "", "not json at all", "<html>blocked</html>"])
def test_undecodable_payload_is_none(raw):
    assert _decode_payload(raw) is None


def test_item_id_is_read_from_a_pdp_url_only():
    assert _ds_item_id(f"https://www.aliexpress.com/item/{_GOOD_ID}.html") == _GOOD_ID
    assert _ds_item_id("https://www.aliexpress.com/wholesale?SearchText=grater") is None
    assert _ds_item_id("") is None


def test_pdp_url_is_canonicalised_to_the_www_form():
    assert (
        _canonical_product_url(f"https://aliexpress.com/item/{_GOOD_ID}.html")
        == f"https://www.aliexpress.com/item/{_GOOD_ID}.html"
    )
    assert _canonical_product_url("https://aliexpress.com/wholesale") == (
        "https://aliexpress.com/wholesale"
    )


def test_country_cookie_pins_the_market():
    cookie = _ds_country_cookie("AU")
    assert cookie["name"] == "aep_usuc_f"
    assert cookie["value"] == "site=glo&region=AU&b_locale=en_US"
    assert cookie["domain"] == ".aliexpress.com"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://login.aliexpress.com/i/account/index.htm", True),
        ("https://ds.aliexpress.com/find-products", False),
        ("", False),
    ],
)
def test_login_wall_detection(url, expected):
    assert _is_login_wall(url) is expected


# ----------------------------------------------------------------------
# Pricing (the "welcome deal" fix)
# ----------------------------------------------------------------------


def test_usd_price_is_converted_to_aud(monkeypatch):
    monkeypatch.setattr(settings, "USD_TO_AUD", 1.55)
    # The live AU quote for this item: US $7.22 -> AUD 11.19, against the
    # AUD 1.53 the retired unauthenticated actor reported for the same item.
    assert _price_aud(_record()) == 11.19


def test_structured_minor_units_beat_the_display_string():
    record = _record(price="US $0.99", cent="722")
    assert _price_aud(record) == 11.19


def test_aud_quote_is_taken_as_is(monkeypatch):
    monkeypatch.setattr(settings, "USD_TO_AUD", 1.55)
    assert _price_aud(_record(price="AU $7.22", cent="722", currency="AUD")) == 7.22


def test_other_currency_is_skipped_rather_than_mispriced():
    assert _price_aud(_record(currency="EUR", cent="722")) is None


def test_price_falls_back_to_the_display_string():
    record = _record(cent=None, price="US $5.32")
    assert _price_aud(record) == pytest.approx(5.32 * 1.55, rel=1e-3)


def test_missing_price_is_none():
    assert _price_aud(_record(cent=None, price="")) is None


# ----------------------------------------------------------------------
# The winning-product gate
# ----------------------------------------------------------------------


def test_gate_passes_a_proven_seller(ali):
    assert ali["extractor"]._passes_winner_gate(_GOOD_ID, _record()) is True


def test_low_order_volume_is_dropped_with_the_documented_log(ali, caplog):
    with caplog.at_level("WARNING"):
        passed = ali["extractor"]._passes_winner_gate(_WEAK_ID, _record(trade="148 sold"))
    assert passed is False
    assert f"Skipping {_WEAK_ID}: Insufficient order volume (148)" in caplog.text


def test_low_rating_is_dropped_with_the_documented_log(ali, caplog):
    with caplog.at_level("WARNING"):
        passed = ali["extractor"]._passes_winner_gate(
            _GOOD_ID, _record(trade="900 sold", score="4.2")
        )
    assert passed is False
    assert f"Skipping {_GOOD_ID}: Rating too low (4.2)" in caplog.text


def test_unreported_order_volume_is_dropped_as_unproven(ali, caplog):
    with caplog.at_level("WARNING"):
        passed = ali["extractor"]._passes_winner_gate(_GOOD_ID, _record(trade=""))
    assert passed is False
    assert f"Skipping {_GOOD_ID}: Insufficient order volume (unavailable)" in caplog.text


def test_unrated_item_is_dropped_as_unproven(ali, caplog):
    with caplog.at_level("WARNING"):
        passed = ali["extractor"]._passes_winner_gate(_NO_RATING_ID, _record(score=""))
    assert passed is False
    assert f"Skipping {_NO_RATING_ID}: Rating unavailable" in caplog.text


def test_floor_that_is_exactly_met_passes(ali):
    assert (
        ali["extractor"]._passes_winner_gate(
            _GOOD_ID, _record(trade="500 sold", score="4.5")
        )
        is True
    )


# ----------------------------------------------------------------------
# Ingestion wiring
# ----------------------------------------------------------------------


async def test_viable_seller_becomes_a_product(ali):
    ali["install"](records=[_record()])
    products = await ali["extractor"].fetch_products(["garlic grater"], "AU")
    assert len(products) == 1
    product = products[0]
    assert product.supplier_name == "AliExpress"
    assert product.supplier_retail_url == (
        f"https://www.aliexpress.com/item/{_GOOD_ID}.html"
    )
    assert product.price_aud == 11.19
    assert product.image_urls == _GALLERY
    assert product.product_description == _META


async def test_search_is_sorted_by_orders_server_side(ali):
    ctx = ali["install"](records=[_record()])
    await ali["extractor"].fetch_products(["garlic grater"], "AU")
    search_calls = [c for c in ctx.request.calls if "selection.search" in c["url"]]
    assert search_calls, "the DS Center search was never called"
    assert '"sort":"ORDERS_DESC"' in search_calls[0]["params"]["data"]
    assert '"searchText":"garlic grater"' in search_calls[0]["params"]["data"]
    assert search_calls[0]["params"]["sign"] == ""


async def test_item_record_is_fetched_with_the_signed_token(ali):
    ctx = ali["install"](records=[_record()])
    await ali["extractor"].fetch_products(["garlic grater"], "AU")
    item_calls = [c for c in ctx.request.calls if "queryByItemUrl" in c["url"]]
    assert len(item_calls) == 2, "expected a priming call then the signed call"
    assert item_calls[0]["params"]["sign"] == ""
    assert item_calls[1]["params"]["sign"] != ""


async def test_candidates_are_ordered_by_order_volume(ali):
    weak = _record(_WEAK_ID, trade="600 sold", score="4.6")
    strong = _record(_GOOD_ID, trade="10000+ sold", score="4.9")
    ali["install"](
        records=[weak, strong],
        hits=[_search_hit(_WEAK_ID), _search_hit(_GOOD_ID)],
    )
    products = await ali["extractor"].fetch_products(["garlic grater"], "AU")
    assert [p.supplier_retail_url for p in products] == [
        f"https://www.aliexpress.com/item/{_GOOD_ID}.html",
        f"https://www.aliexpress.com/item/{_WEAK_ID}.html",
    ]


async def test_gated_out_items_never_reach_the_harvest(ali):
    ali["install"](records=[_record(_WEAK_ID, trade="12 sold")])
    products = await ali["extractor"].fetch_products(["garlic grater"], "AU")
    assert products == []


async def test_duplicate_items_across_keywords_are_evaluated_once(ali):
    ctx = ali["install"](records=[_record()])
    # The search script answers the same hit for both keywords.
    ctx.request.responses = [
        {},
        _search_payload([_search_hit(_GOOD_ID)]),
        {},
        _item_payload(_record()),
        {},
        _search_payload([_search_hit(_GOOD_ID)]),
    ]
    await ali["extractor"].fetch_products(["garlic grater", "garlic press"], "AU")
    item_calls = [c for c in ctx.request.calls if "queryByItemUrl" in c["url"]]
    assert len(item_calls) == 2, "the repeat item should not be re-queried"


async def test_thin_gallery_is_dropped_not_exported(ali):
    ali["install"](records=[_record()], gallery=["https://cdn.example.com/only.jpg"])
    products = await ali["extractor"].fetch_products(["garlic grater"], "AU")
    assert products == []


async def test_no_search_hits_yields_no_products(ali):
    ali["install"](records=None, search_responses=[{}, _search_payload([])])
    products = await ali["extractor"].fetch_products(["garlic grater"], "AU")
    assert products == []


async def test_failed_search_exchange_is_a_block(ali):
    # A failed exchange is not an empty result set: the chain must fall
    # through rather than report an empty funnel.
    ali["install"](
        records=None,
        search_responses=[{}, {"ret": ["FAIL_SYS_API_LIMITED::rate limited"]}],
    )
    with pytest.raises(ExtractorBlockedException):
        await ali["extractor"].fetch_products(["garlic grater"], "AU")


async def test_expired_session_on_the_search_raises_for_the_operator(ali):
    ali["install"](
        records=None,
        search_responses=[
            {},
            {"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"], "data": {}},
        ],
    )
    with pytest.raises(DsCenterSessionExpiredError):
        await ali["extractor"].fetch_products(["garlic grater"], "AU")


async def test_expired_session_on_the_item_record_raises_for_the_operator(ali):
    ali["install"](
        records=None,
        search_responses=[
            {},
            _search_payload([_search_hit(_GOOD_ID)]),
            {},
            # The item record's own exchange answers with a challenge.
            {"ret": ["FAIL_SYS_USER_VALIDATE::challenge"], "data": {}},
        ],
    )
    with pytest.raises(DsCenterSessionExpiredError):
        await ali["extractor"].fetch_products(["garlic grater"], "AU")


# ----------------------------------------------------------------------
# Browser context (session optionality + login wall)
# ----------------------------------------------------------------------


class _FakePlaywright:
    """`async_playwright().start()` stand-in exposing a scripted chromium."""

    def __init__(self, recorder, context) -> None:
        self.chromium = _FakeChromium(recorder, context)
        self.stopped = False

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True


class _FakeChromium:
    def __init__(self, recorder, context) -> None:
        self.recorder = recorder
        self.context = context

    async def launch(self, **kwargs):
        self.recorder["launch_kwargs"] = dict(kwargs)
        return self

    async def new_context(self, **kwargs):
        self.recorder["context_kwargs"] = dict(kwargs)
        return self.context

    async def close(self):
        self.recorder["closed"] = True


class _FakeStealth:
    def __init__(self, **kwargs) -> None:
        pass

    async def apply_stealth_async(self, context):
        pass


def _script_playwright(monkeypatch, recorder, context):
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: _FakePlaywright(recorder, context),
    )
    monkeypatch.setattr("playwright_stealth.Stealth", _FakeStealth)


async def test_state_file_is_injected_when_it_exists(monkeypatch, tmp_path):
    state = tmp_path / "ali_ds_state.json"
    state.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", str(state))
    recorder: dict = {}
    _script_playwright(monkeypatch, recorder, _FakeContext())
    extractor = AliExpressDsCenterExtractor()
    async with extractor._browser_context():
        pass
    assert recorder["context_kwargs"]["storage_state"] == str(state)


async def test_absent_state_file_is_not_an_error(monkeypatch, tmp_path):
    # The DS Center answers anonymously: a missing session must not block a run.
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", str(tmp_path / "absent.json"))
    recorder: dict = {}
    _script_playwright(monkeypatch, recorder, _FakeContext())
    extractor = AliExpressDsCenterExtractor()
    async with extractor._browser_context():
        pass
    assert "storage_state" not in recorder["context_kwargs"]


async def test_browser_context_pins_the_market_cookie(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(settings, "TARGET_COUNTRY", "AU")
    recorder: dict = {}
    context = _FakeContext()
    _script_playwright(monkeypatch, recorder, context)
    extractor = AliExpressDsCenterExtractor()
    async with extractor._browser_context():
        pass
    assert context.added_cookies == [_ds_country_cookie("AU")]


async def test_login_wall_on_a_supplied_session_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", str(tmp_path / "absent.json"))
    recorder: dict = {}
    page = _FakePage(url="https://login.aliexpress.com/i/account/index.htm")
    _script_playwright(monkeypatch, recorder, _FakeContext(pages=[page]))
    extractor = AliExpressDsCenterExtractor()
    with pytest.raises(DsCenterSessionExpiredError):
        async with extractor._browser_context():
            pass
