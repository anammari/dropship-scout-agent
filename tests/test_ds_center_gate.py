"""Offline unit tests for the AliExpress Dropshipping Center gate.

Zero network: the DS Center page, the authenticated MTOP lookup, and the
Playwright browser are all scripted. The two live products from the gate's
directive are the fixtures — `_DS_CENTER_ITEM` (listed in the DS Center) and
`_NON_DS_CENTER_ITEM` (absent from it).
"""

import hashlib

import pytest

from src.config import settings
from src.extractors import aliexpress_apify as ali
from src.extractors.aliexpress_apify import (
    AliExpressApifyExtractor,
    DsCenterSessionExpiredError,
    _classify_ds_item,
    _ds_center_verdict,
    _ds_country_cookie,
    _ds_item_id,
    _h5_sign,
    _is_login_wall,
)
from tests.test_aliexpress_apify import FakeApifyClient, _GALLERY, _item

# The directive's live test cases, plus the item id behind each URL.
_DS_CENTER_ITEM = "https://www.aliexpress.com/item/1005012359331033.html"
_DS_CENTER_ID = "1005012359331033"
_NON_DS_CENTER_ITEM = "https://www.aliexpress.com/item/1005007987710373.html"
_NON_DS_CENTER_ID = "1005007987710373"

_ANALYSIS_URL = "https://ds.aliexpress.com/product-analysis"
_LOOKUP_URL = "https://acs.aliexpress.com/h5/mtop.aidc.ds.center.selection.queryByItemUrl/1.0/"
_TOKEN = "956eec23d14ef4d18d76555d6077126a"

_SUPPORTED_PAYLOAD = {
    "ret": ["SUCCESS::调用成功"],
    "data": {"code": "", "message": "", "data": {"itemId": _DS_CENTER_ID}},
}
_ABSENT_CODE_PAYLOAD = {
    "ret": ["SUCCESS::调用成功"],
    "data": {"code": "-1", "message": "none_of_item", "ret": False},
}
_ABSENT_MESSAGE_PAYLOAD = {
    "ret": ["SUCCESS::调用成功"],
    "data": {"code": "", "message": "none_of_item", "ret": False},
}
_EXPIRED_SESSION_PAYLOAD = {
    "ret": ["FAIL_SYS_SESSION_EXPIRED::Session expired"],
    "data": {},
}


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeResponse:
    """Playwright's `APIResponse.json()` is a coroutine; the fake mirrors that."""

    def __init__(self, payload) -> None:
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeRequest:
    """Scripted `context.request`: one payload per `get`, calls recorded."""

    def __init__(self, payloads=None) -> None:
        self.payloads = list(payloads or [])
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return _FakeResponse(self.payloads.pop(0) if self.payloads else {})


class _FakePage:
    """Scripted DS Center / PDP page."""

    def __init__(self, url=f"{_ANALYSIS_URL}?itemId={_DS_CENTER_ID}", dom=None,
                 evaluate_returns=None) -> None:
        self.url = url
        self.dom = dom if dom is not None else {"loginWall": False, "reject": None, "rows": 0}
        self.evaluate_returns = evaluate_returns
        self.gotos = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def evaluate(self, js):
        if self.evaluate_returns is not None:
            return self.evaluate_returns
        return self.dom

    async def close(self):
        self.closed = True


class _FakeContext:
    """Authenticated context stand-in: cookies + scripted request + pages."""

    def __init__(self, page=None, request=None, token=_TOKEN) -> None:
        self._page = page or _FakePage()
        self.request = request or _FakeRequest()
        self._token = token
        self.kwargs = None
        self.added_cookies = []

    async def new_page(self):
        return self._page

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    async def cookies(self, urls=None):
        if not self._token:
            return []
        return [{
            "name": "_m_h5_tk",
            "value": f"{self._token}_1789740076523",
            "domain": "acs.aliexpress.com",
            "path": "/",
        }]

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, recorder, context) -> None:
        self.recorder = recorder
        self.context = context

    async def new_context(self, **kwargs):
        self.recorder["context_kwargs"] = dict(kwargs)
        return self.context

    async def close(self):
        self.recorder["browser_closed"] = True


class _FakeChromium:
    def __init__(self, recorder, context) -> None:
        self.recorder = recorder
        self.context = context

    async def launch(self, **kwargs):
        self.recorder["launch_kwargs"] = dict(kwargs)
        return _FakeBrowser(self.recorder, self.context)


class _FakeAsyncPlaywright:
    def __init__(self, recorder, context) -> None:
        self.chromium = _FakeChromium(recorder, context)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeStealth:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        _FakeStealth.instances.append(self)

    async def apply_stealth_async(self, context):
        self.applied_to = context


@pytest.fixture
def ds_gate(monkeypatch, tmp_path):
    """Gate on, scripted SDK + browser, saved session file present.

    Returns a namespace-ish dict with the recorder, the scripted page, and
    the state path so each test can assert on what the run did.
    """
    FakeApifyClient.script = []
    FakeApifyClient.calls = []
    FakeApifyClient.datasets = {}
    FakeApifyClient.dataset_reads = []
    FakeApifyClient.dataset_error = None
    FakeApifyClient.return_run_model = False
    FakeApifyClient._counter = 0
    _FakeStealth.instances = []

    state_path = tmp_path / "ali_ds_state.json"
    state_path.write_text('{"cookies": [], "origins": []}')

    monkeypatch.setattr("apify_client.ApifyClient", FakeApifyClient)
    monkeypatch.setattr(settings, "APIFY_API_TOKEN", "test-token")
    monkeypatch.setattr(
        settings, "APIFY_ALIEXPRESS_ACTOR", "cryptosignals/aliexpress-scraper"
    )
    monkeypatch.setattr(settings, "APIFY_MAX_ITEMS_PER_KEYWORD", 20)
    monkeypatch.setattr(settings, "APIFY_MAX_ITEMS_PER_RUN", 100)
    monkeypatch.setattr(settings, "APIFY_RUN_TIMEOUT_SECS", 60)
    monkeypatch.setattr(settings, "APIFY_PRICE_PER_RESULT_USD", 0.005)
    monkeypatch.setattr(settings, "USD_TO_AUD", 1.55)
    monkeypatch.setattr(settings, "USER_AGENT", "test-agent")
    monkeypatch.setattr(settings, "ENABLE_DS_CENTER_GATE", True)
    monkeypatch.setattr(settings, "ALI_DS_STATE_PATH", str(state_path))

    page = _FakePage(evaluate_returns=list(_GALLERY))
    context = _FakeContext(page=page)
    recorder: dict = {}
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: _FakeAsyncPlaywright(recorder, context),
    )
    monkeypatch.setattr("playwright_stealth.Stealth", _FakeStealth)
    return {
        "recorder": recorder,
        "page": page,
        "context": context,
        "state_path": state_path,
        "extractor": AliExpressApifyExtractor(),
    }


def _verdict_script(monkeypatch, mapping):
    """Script `_ds_center_verdict` per item id."""
    calls = []

    async def fake(context, page, product_url):
        calls.append(_ds_item_id(product_url))
        return mapping.get(_ds_item_id(product_url))

    monkeypatch.setattr(ali, "_ds_center_verdict", fake)
    return calls


async def _leaf_harvest(self, candidates):
    """Harvest stub: leaves candidates untouched (no browser at all)."""


# ----------------------------------------------------------------------
# URL / signature helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (_DS_CENTER_ITEM, _DS_CENTER_ID),
        (_NON_DS_CENTER_ITEM, _NON_DS_CENTER_ID),
        ("https://www.aliexpress.com/item/1005006234567890.html?x=1", "1005006234567890"),
        ("https://www.aliexpress.com/wholesale?SearchText=gadgets", None),
        ("", None),
    ],
)
def test_item_id_is_read_from_a_pdp_url_only(url, expected):
    assert _ds_item_id(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://login.aliexpress.com/i/account/index.htm", True),
        ("https://ds.aliexpress.com/product-analysis?itemId=1", False),
        ("https://www.aliexpress.com/item/1.html", False),
        ("", False),
    ],
)
def test_login_wall_detection(url, expected):
    assert _is_login_wall(url) is expected


def test_signature_is_the_documented_mtop_md5():
    token, timestamp, data = "abc123", "1789738006190", '{"itemId":"42"}'
    expected = hashlib.md5(
        f"{token}&{timestamp}&12574478&{data}".encode()
    ).hexdigest()
    assert _h5_sign(token, timestamp, data) == expected


# ----------------------------------------------------------------------
# Payload classification
# ----------------------------------------------------------------------


def test_listed_item_payload_is_supported():
    assert _classify_ds_item(_SUPPORTED_PAYLOAD) is True


@pytest.mark.parametrize(
    "payload", [_ABSENT_CODE_PAYLOAD, _ABSENT_MESSAGE_PAYLOAD]
)
def test_absent_item_payload_is_not_supported(payload):
    assert _classify_ds_item(payload) is False


def test_unreadable_payload_is_unknown():
    assert _classify_ds_item(None) is None
    assert _classify_ds_item({}) is None
    assert _classify_ds_item({"ret": ["SUCCESS::调用成功"], "data": {}}) is None


def test_expired_session_payload_raises():
    with pytest.raises(DsCenterSessionExpiredError):
        _classify_ds_item(_EXPIRED_SESSION_PAYLOAD)


def test_challenge_payload_raises():
    with pytest.raises(DsCenterSessionExpiredError):
        _classify_ds_item({"ret": ["FAIL_SYS_USER_VALIDATE::challenge"], "data": {}})


# ----------------------------------------------------------------------
# Verdict resolution (page + authenticated lookup)
# ----------------------------------------------------------------------


async def test_login_redirect_raises():
    page = _FakePage(url="https://login.aliexpress.com/i/account/index.htm")
    with pytest.raises(DsCenterSessionExpiredError):
        await _ds_center_verdict(_FakeContext(page=page), page, _DS_CENTER_ITEM)


async def test_password_field_raises():
    page = _FakePage(dom={"loginWall": True, "reject": None, "rows": 0})
    with pytest.raises(DsCenterSessionExpiredError):
        await _ds_center_verdict(_FakeContext(page=page), page, _DS_CENTER_ITEM)


async def test_page_navigates_to_the_analysis_entry_for_the_item():
    page = _FakePage()
    request = _FakeRequest([{}, _SUPPORTED_PAYLOAD])
    await _ds_center_verdict(_FakeContext(page=page, request=request), page, _DS_CENTER_ITEM)
    assert page.gotos == [f"{_ANALYSIS_URL}?itemId={_DS_CENTER_ID}"]


async def test_lookup_priming_call_then_signed_call():
    page = _FakePage()
    request = _FakeRequest([{}, _SUPPORTED_PAYLOAD])
    await _ds_center_verdict(_FakeContext(page=page, request=request), page, _DS_CENTER_ITEM)
    assert [c["url"] for c in request.calls] == [_LOOKUP_URL, _LOOKUP_URL]
    primed, signed = request.calls
    assert primed["params"]["sign"] == ""
    data = signed["params"]["data"]
    assert data == f'{{"itemUrl":"https://www.aliexpress.com/item/{_DS_CENTER_ID}.html"}}'
    assert signed["params"]["sign"] == _h5_sign(
        _TOKEN, signed["params"]["t"], data
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (_SUPPORTED_PAYLOAD, True),
        (_ABSENT_CODE_PAYLOAD, False),
        (_ABSENT_MESSAGE_PAYLOAD, False),
    ],
)
async def test_verdict_comes_from_the_ds_center_record(payload, expected):
    page = _FakePage()
    request = _FakeRequest([{}, payload])
    verdict = await _ds_center_verdict(
        _FakeContext(page=page, request=request), page, _DS_CENTER_ITEM
    )
    assert verdict is expected


async def test_dom_rejection_is_the_fallback_when_the_api_is_silent():
    page = _FakePage(dom={"loginWall": False, "reject": "Not supported", "rows": 0})
    request = _FakeRequest([{}, {}])
    verdict = await _ds_center_verdict(
        _FakeContext(page=page, request=request), page, _NON_DS_CENTER_ITEM
    )
    assert verdict is False


async def test_rendered_analysis_is_the_fallback_pass():
    page = _FakePage(dom={"loginWall": False, "reject": None, "rows": 15})
    request = _FakeRequest([{}, {}])
    verdict = await _ds_center_verdict(
        _FakeContext(page=page, request=request), page, _DS_CENTER_ITEM
    )
    assert verdict is True


async def test_no_signal_at_all_is_inconclusive():
    page = _FakePage()
    request = _FakeRequest([{}, {}])
    verdict = await _ds_center_verdict(
        _FakeContext(page=page, request=request), page, _DS_CENTER_ITEM
    )
    assert verdict is None


async def test_missing_token_is_inconclusive_rather_than_a_crash():
    page = _FakePage()
    context = _FakeContext(page=page, request=_FakeRequest([{}]), token="")
    assert await _ds_center_verdict(context, page, _DS_CENTER_ITEM) is None


# ----------------------------------------------------------------------
# Extractor wiring
# ----------------------------------------------------------------------


async def test_gate_disabled_never_touches_the_ds_center(monkeypatch, ds_gate):
    monkeypatch.setattr(settings, "ENABLE_DS_CENTER_GATE", False)

    async def boom(context, page, product_url):
        raise AssertionError("DS Center probe ran while the gate was off")

    monkeypatch.setattr(ali, "_ds_center_verdict", boom)
    monkeypatch.setattr(AliExpressApifyExtractor, "_harvest_galleries", _leaf_harvest)
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert [p.supplier_retail_url for p in products] == [_DS_CENTER_ITEM]


async def test_missing_session_raises_for_the_operator(monkeypatch, ds_gate, tmp_path):
    monkeypatch.setattr(
        settings, "ALI_DS_STATE_PATH", str(tmp_path / "absent.json")
    )
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    with pytest.raises(DsCenterSessionExpiredError):
        await ds_gate["extractor"].fetch_products(["gadgets"], "AU")


async def test_saved_state_is_injected_into_the_browser_context(monkeypatch, ds_gate):
    _verdict_script(monkeypatch, {_DS_CENTER_ID: True})
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    kwargs = ds_gate["recorder"]["context_kwargs"]
    assert kwargs["storage_state"] == str(ds_gate["state_path"])
    assert kwargs["user_agent"] == "test-agent"
    assert _FakeStealth.instances, "stealth must still be applied"


async def test_gate_judges_the_item_for_the_run_target_country(monkeypatch, ds_gate):
    _verdict_script(monkeypatch, {_DS_CENTER_ID: True})
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert ds_gate["context"].added_cookies == [_ds_country_cookie("AU")]


def test_country_cookie_pins_the_market():
    cookie = _ds_country_cookie("AU")
    assert cookie["name"] == "aep_usuc_f"
    assert cookie["value"] == "site=glo&region=AU&b_locale=en_US"
    assert cookie["domain"] == ".aliexpress.com"


async def test_non_ds_center_item_is_dropped_with_the_documented_log(
    monkeypatch, ds_gate, caplog
):
    _verdict_script(monkeypatch, {_NON_DS_CENTER_ID: False})
    FakeApifyClient.script = [
        [_item(productUrl=_NON_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]
    ]
    with caplog.at_level("WARNING"):
        products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert products == []
    assert f"Skipping {_NON_DS_CENTER_ID}: Not supported in DS Center" in caplog.text


async def test_ds_center_item_survives_the_gate(monkeypatch, ds_gate):
    _verdict_script(monkeypatch, {_DS_CENTER_ID: True})
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert [p.supplier_retail_url for p in products] == [_DS_CENTER_ITEM]
    # A gallery-complete candidate is not re-harvested off its own PDP.
    assert ds_gate["page"].gotos == []


async def test_inconclusive_verdict_drops_the_candidate(
    monkeypatch, ds_gate, caplog
):
    _verdict_script(monkeypatch, {_DS_CENTER_ID: None})
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY))]]
    with caplog.at_level("WARNING"):
        products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert products == []
    assert "DS Center gate inconclusive" in caplog.text


async def test_thin_candidate_is_gated_then_harvested(monkeypatch, ds_gate):
    _verdict_script(monkeypatch, {_DS_CENTER_ID: True})
    FakeApifyClient.script = [[_item(productUrl=_DS_CENTER_ITEM)]]
    products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert len(products) == 1
    assert products[0].image_urls == list(_GALLERY)
    assert ds_gate["page"].gotos[-1] == _DS_CENTER_ITEM


async def test_dropped_candidates_are_not_exported_as_products(monkeypatch, ds_gate):
    _verdict_script(
        monkeypatch, {_NON_DS_CENTER_ID: False, _DS_CENTER_ID: True}
    )
    FakeApifyClient.script = [[
        _item(productUrl=_NON_DS_CENTER_ITEM, imageUrl=list(_GALLERY)),
        _item(productUrl=_DS_CENTER_ITEM, imageUrl=list(_GALLERY)),
    ]]
    products = await ds_gate["extractor"].fetch_products(["gadgets"], "AU")
    assert [p.supplier_retail_url for p in products] == [_DS_CENTER_ITEM]
