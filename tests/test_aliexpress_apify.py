"""Offline unit tests for `src/extractors/aliexpress_apify` (plan F.3.1).

Zero network and zero Apify spend: the `apify_client.ApifyClient` SDK is
replaced by an in-memory fake (the extractor imports it lazily inside
`_run_actor`, so patching the module attribute is enough) and the Playwright
PDP harvest is replaced by a scripted coroutine. Verifies the actor payload,
the event-loop offload, the pay-per-result budget guard, the
dataset-to-`RawSupplierProduct` mapping (including the USD currency guard),
the hard 3-image rule, and the failure taxonomy.
"""

import threading
from datetime import timedelta

import pytest

from src.config import settings
from src.extractors.aliexpress_apify import (
    AliExpressApifyExtractor,
    _canonical_product_url,
    _coerce_price_usd,
    _run_dataset_id,
)
from src.extractors.base import (
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
    ExtractorTimeoutException,
)

_ITEM_URL = "https://www.aliexpress.com/item/1005006234567890.html"
_OTHER_ITEM_URL = "https://www.aliexpress.com/item/1005006999888777.html"
_GALLERY = [
    "https://ae01.alicdn.com/kf/A1.jpg",
    "https://ae01.alicdn.com/kf/A2.jpg",
    "https://ae01.alicdn.com/kf/A3.jpg",
]


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _RunModelLike:
    """The attribute the extractor reads off apify-client 3.x's `Run` model.

    The SDK returns a pydantic `Run`, not a dict — the shape that broke the
    first live run. Tests use this stand-in to keep that regression covered
    without importing the SDK's private model module.
    """

    def __init__(self, default_dataset_id: str) -> None:
        self.default_dataset_id = default_dataset_id


class FakeApifyClient:
    """Scripted stand-in for `apify_client.ApifyClient`.

    `script` holds one entry per actor call: a list of dataset items, an
    Exception to raise from `.call()`, or a dict to return as the run object
    (e.g. `{}` to simulate a run with no default dataset). Call kwargs, the
    thread each call ran on, and dataset reads are recorded for assertions.

    `return_run_model` switches successful returns from a dict to the
    pydantic-`Run`-shaped stand-in above.
    """

    script: list = []
    calls: list = []
    threads: list = []
    datasets: dict = {}
    dataset_reads: list = []
    dataset_error: "Exception | None" = None
    return_run_model: bool = False
    _counter: int = 0

    def __init__(self, token: str) -> None:
        self.token = token

    def actor(self, actor_id: str):
        fake = FakeApifyClient

        class _Actor:
            def call(self, run_input=None, wait_duration=None):
                fake.calls.append(
                    {
                        "actor_id": actor_id,
                        "run_input": run_input,
                        "wait_duration": wait_duration,
                    }
                )
                fake.threads.append(threading.current_thread())
                entry = fake.script.pop(0) if fake.script else []
                if isinstance(entry, Exception):
                    raise entry
                if isinstance(entry, dict):
                    return entry
                fake._counter += 1
                dataset_id = f"ds-{fake._counter}"
                fake.datasets[dataset_id] = entry
                if fake.return_run_model:
                    return _RunModelLike(dataset_id)
                return {"defaultDatasetId": dataset_id}

        return _Actor()

    def dataset(self, dataset_id: str):
        fake = FakeApifyClient

        class _Dataset:
            def iterate_items(self):
                fake.dataset_reads.append(dataset_id)
                if fake.dataset_error is not None:
                    raise fake.dataset_error
                return iter(fake.datasets.get(dataset_id, []))

        return _Dataset()


async def _no_harvest(self, candidates):
    """Default harvest: leaves thin candidates untouched (no browser)."""


def _harvest_with(gallery):
    async def _harvest(self, candidates):
        for candidate in candidates:
            candidate["images"] = list(gallery)

    return _harvest


@pytest.fixture
def apify(monkeypatch):
    """Install the scripted fake SDK, pinned settings, and a no-op harvest."""
    FakeApifyClient.script = []
    FakeApifyClient.calls = []
    FakeApifyClient.threads = []
    FakeApifyClient.datasets = {}
    FakeApifyClient.dataset_reads = []
    FakeApifyClient.dataset_error = None
    FakeApifyClient.return_run_model = False
    FakeApifyClient._counter = 0

    monkeypatch.setattr("apify_client.ApifyClient", FakeApifyClient)
    # Pinned so the tests do not depend on the machine's `.env`.
    monkeypatch.setattr(settings, "APIFY_API_TOKEN", "test-token")
    monkeypatch.setattr(
        settings, "APIFY_ALIEXPRESS_ACTOR", "cryptosignals/aliexpress-scraper"
    )
    monkeypatch.setattr(settings, "APIFY_MAX_ITEMS_PER_KEYWORD", 20)
    monkeypatch.setattr(settings, "APIFY_MAX_ITEMS_PER_RUN", 100)
    monkeypatch.setattr(settings, "APIFY_RUN_TIMEOUT_SECS", 60)
    monkeypatch.setattr(settings, "APIFY_PRICE_PER_RESULT_USD", 0.005)
    monkeypatch.setattr(settings, "USD_TO_AUD", 1.55)
    monkeypatch.setattr(AliExpressApifyExtractor, "_harvest_galleries", _no_harvest)
    return FakeApifyClient


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------


def _item(**overrides) -> dict:
    """One dataset item, shaped like the actor's documented output."""
    item = {
        "id": "1005006234567890",
        "title": "TWS Wireless Earbuds Bluetooth 5.3 HiFi Stereo",
        "price": 8.99,
        "originalPrice": 22.0,
        "discount": "59%",
        "currency": "USD",
        "soldCount": 15820,
        "starRating": 4.7,
        "reviewCount": 3241,
        "sellerRating": "97.8%",
        "shipping": "Free Shipping",
        "store": "TechGadgets Official Store",
        "storeId": "910234567",
        "imageUrl": "https://ae01.alicdn.com/kf/S1234567890.jpg",
        "productUrl": _ITEM_URL,
    }
    item.update(overrides)
    return item


def _extractor(**kwargs) -> AliExpressApifyExtractor:
    return AliExpressApifyExtractor(**kwargs)


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def test_identity_is_unchanged():
    extractor = _extractor()
    assert extractor.engine_name == "aliexpress_apify"
    assert extractor.supplier_name == "AliExpress"


# ----------------------------------------------------------------------
# Actor payload + execution paradigm
# ----------------------------------------------------------------------


async def test_run_input_matches_the_actor_schema(apify):
    apify.script = [[_item()]]
    await _extractor().fetch_products(["wireless earbuds"])

    assert len(apify.calls) == 1
    call = apify.calls[0]
    assert call["actor_id"] == "cryptosignals/aliexpress-scraper"
    assert call["run_input"] == {
        "action": "search",
        "query": "wireless earbuds",
        "maxItems": 20,
        "country": "AU",
        "currency": "USD",
        "sort": "default",
        "proxyConfiguration": {"useApifyProxy": True},
    }
    assert call["wait_duration"] == timedelta(seconds=60)


async def test_actor_call_is_offloaded_off_the_event_loop(apify):
    apify.script = [[_item()]]
    await _extractor().fetch_products(["k"])

    assert apify.threads
    assert apify.threads[0] is not threading.main_thread()


async def test_country_argument_reaches_the_actor(apify):
    apify.script = [[]]
    await _extractor().fetch_products(["k"], country="NZ")

    assert apify.calls[0]["run_input"]["country"] == "NZ"


# ----------------------------------------------------------------------
# Dataset item mapping
# ----------------------------------------------------------------------


async def test_item_maps_to_a_raw_supplier_product(apify):
    apify.script = [[_item(imageUrl=_GALLERY)]]
    products = await _extractor().fetch_products(["k"])

    assert len(products) == 1
    product = products[0]
    assert product.supplier_name == "AliExpress"
    assert product.supplier_retail_url == _ITEM_URL
    assert product.product_title == "TWS Wireless Earbuds Bluetooth 5.3 HiFi Stereo"
    assert product.price_aud == round(8.99 * settings.USD_TO_AUD, 2)
    assert product.shipping_cost_aud == 0.0
    assert product.image_urls == _GALLERY
    # No PDP description harvested -> the title stands in.
    assert product.product_description == product.product_title


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (None, None),
        ({}, None),
        ({"defaultDatasetId": "ds-9"}, "ds-9"),
        ({"default_dataset_id": "ds-8"}, "ds-8"),
        (_RunModelLike("ds-7"), "ds-7"),
        (object(), None),
    ],
)
def test_run_dataset_id_reads_both_sdk_shapes(run, expected):
    assert _run_dataset_id(run) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_ITEM_URL, _ITEM_URL),
        # The actor returns the search-result URL with its tracking params.
        (
            f"{_ITEM_URL}?algo_pvid=abc&pdp_npi=6%40dis%21USD&search_p4p_id=1",
            _ITEM_URL,
        ),
        # Whatever host the actor scraped, the canonical PDP form is emitted.
        (
            "https://www.aliexpress.us/item/1005006234567890.html?x=1",
            _ITEM_URL,
        ),
        # Not a PDP at all -> returned untouched for the caller to reject.
        ("https://www.aliexpress.com/w/wholesale-kitchen.html", "https://www.aliexpress.com/w/wholesale-kitchen.html"),
    ],
)
def test_canonical_product_url_strips_tracking_params(raw, expected):
    assert _canonical_product_url(raw) == expected


async def test_tracking_params_are_stripped_from_the_exported_url(apify):
    apify.script = [
        [
            _item(
                productUrl=f"{_ITEM_URL}?algo_pvid=1e09ae73&pdp_npi=6%40dis%21USD&curPageLogUid=xyz",
                imageUrl=_GALLERY,
            )
        ]
    ]

    products = await _extractor().fetch_products(["k"])

    assert [p.supplier_retail_url for p in products] == [_ITEM_URL]


async def test_run_returned_as_a_pydantic_model_is_supported(apify):
    """apify-client 3.x returns a `Run` model, not a dict (live regression)."""
    apify.return_run_model = True
    apify.script = [[_item(imageUrl=_GALLERY)]]

    products = await _extractor().fetch_products(["k"])

    assert len(products) == 1
    assert products[0].supplier_retail_url == _ITEM_URL


async def test_url_field_is_used_when_product_url_is_absent(apify):
    item = _item(imageUrl=_GALLERY)
    item.pop("productUrl")
    item["url"] = _ITEM_URL
    apify.script = [[item]]

    products = await _extractor().fetch_products(["k"])

    assert len(products) == 1
    assert products[0].supplier_retail_url == _ITEM_URL


async def test_non_usd_currency_is_skipped(apify):
    apify.script = [[_item(currency="EUR", imageUrl=_GALLERY)]]
    assert await _extractor().fetch_products(["k"]) == []


async def test_absent_currency_is_accepted(apify):
    item = _item(imageUrl=_GALLERY)
    item.pop("currency")
    apify.script = [[item]]
    assert len(await _extractor().fetch_products(["k"])) == 1


async def test_search_url_is_not_a_valid_product_page(apify):
    apify.script = [
        [
            _item(
                productUrl="https://www.aliexpress.com/w/wholesale-earbuds.html",
                imageUrl=_GALLERY,
            )
        ]
    ]
    assert await _extractor().fetch_products(["k"]) == []


async def test_non_dict_and_untitled_items_are_ignored(apify):
    apify.script = [["junk", None, 42, _item(title="", imageUrl=_GALLERY)]]
    assert await _extractor().fetch_products(["k"]) == []


async def test_empty_actor_output_yields_no_products(apify):
    apify.script = [[]]
    assert await _extractor().fetch_products(["k"]) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (8.99, 8.99),
        ("US $5.32", 5.32),
        ("1,234.50", 1234.50),
        (0, None),
        (-1, None),
        (None, None),
        ("", None),
        ("N/A", None),
    ],
)
def test_price_coercion_handles_numeric_and_string_forms(raw, expected):
    assert _coerce_price_usd(raw) == expected


async def test_items_without_a_usable_price_are_skipped(apify):
    apify.script = [
        [_item(price=0, imageUrl=_GALLERY), _item(price=None, imageUrl=_GALLERY)]
    ]
    assert await _extractor().fetch_products(["k"]) == []


# ----------------------------------------------------------------------
# The hard 3-image rule
# ----------------------------------------------------------------------


async def test_only_thin_candidates_reach_the_harvest(apify, monkeypatch):
    seen = {}

    async def _record(self, candidates):
        seen["urls"] = [c["url"] for c in candidates]

    monkeypatch.setattr(AliExpressApifyExtractor, "_harvest_galleries", _record)
    apify.script = [[_item(imageUrl=_GALLERY), _item(productUrl=_OTHER_ITEM_URL)]]

    await _extractor().fetch_products(["k"])

    assert seen["urls"] == [_OTHER_ITEM_URL]


async def test_thin_gallery_is_upgraded_from_the_pdp(apify, monkeypatch):
    monkeypatch.setattr(
        AliExpressApifyExtractor, "_harvest_galleries", _harvest_with(_GALLERY)
    )
    apify.script = [[_item()]]  # one imageUrl only

    products = await _extractor().fetch_products(["k"])

    assert len(products) == 1
    assert products[0].image_urls == _GALLERY


async def test_candidate_is_dropped_when_the_harvest_cannot_reach_three(
    apify, monkeypatch
):
    monkeypatch.setattr(
        AliExpressApifyExtractor, "_harvest_galleries", _harvest_with(_GALLERY[:2])
    )
    apify.script = [[_item()]]

    # Never fabricated: two images is not a product.
    assert await _extractor().fetch_products(["k"]) == []


async def test_harvest_crash_skips_candidates_without_aborting(apify, monkeypatch):
    async def _boom(self, candidates):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(AliExpressApifyExtractor, "_harvest_galleries", _boom)
    apify.script = [[_item()]]

    assert await _extractor().fetch_products(["k"]) == []


async def test_one_unharvestable_candidate_does_not_abort_the_batch(
    apify, monkeypatch
):
    async def _partial(self, candidates):
        candidates[0]["images"] = list(_GALLERY)

    monkeypatch.setattr(AliExpressApifyExtractor, "_harvest_galleries", _partial)
    apify.script = [[_item(), _item(productUrl=_OTHER_ITEM_URL)]]

    products = await _extractor().fetch_products(["k"])

    assert [p.supplier_retail_url for p in products] == [_ITEM_URL]


# ----------------------------------------------------------------------
# Pay-per-result budget guard
# ----------------------------------------------------------------------


async def test_per_run_item_cap_bounds_the_actor_runs(apify):
    apify.script = [[], []]
    await _extractor(max_items=20, max_items_per_run=30).fetch_products(
        ["a", "b", "c"]
    )

    # 20 for the first keyword, the remaining 10 for the second, and the
    # third is never run because the per-run ceiling is spent.
    assert [c["run_input"]["maxItems"] for c in apify.calls] == [20, 10]
    assert [c["run_input"]["query"] for c in apify.calls] == ["a", "b"]


async def test_max_items_is_clamped_to_the_actor_ceiling(apify):
    apify.script = [[]]
    await _extractor(max_items=900, max_items_per_run=1000).fetch_products(["k"])

    assert apify.calls[0]["run_input"]["maxItems"] == 500


async def test_run_cost_estimate_is_logged(apify, caplog):
    apify.script = [[]]
    with caplog.at_level("INFO"):
        await _extractor(max_items=100, max_items_per_run=100).fetch_products(["k"])

    assert "aliexpress run plan" in caplog.text
    assert "$0.500" in caplog.text


# ----------------------------------------------------------------------
# Failure taxonomy
# ----------------------------------------------------------------------


async def test_missing_token_raises_not_configured(apify, monkeypatch):
    monkeypatch.setattr(settings, "APIFY_API_TOKEN", "")

    with pytest.raises(ExtractorNotConfiguredError) as excinfo:
        await _extractor(token=None).fetch_products(["k"])

    assert excinfo.value.credential_name == "APIFY_API_TOKEN"
    assert apify.calls == []  # nothing was run


async def test_auth_failure_becomes_blocked(apify):
    apify.script = [Exception("401 Unauthorized")]
    with pytest.raises(ExtractorBlockedException):
        await _extractor().fetch_products(["k"])


async def test_timeout_becomes_a_timeout_exception(apify):
    apify.script = [Exception("actor run timeout after 60s")]
    with pytest.raises(ExtractorTimeoutException):
        await _extractor().fetch_products(["k"])


async def test_other_call_failure_becomes_blocked(apify):
    apify.script = [Exception("boom")]
    with pytest.raises(ExtractorBlockedException):
        await _extractor().fetch_products(["k"])


async def test_missing_dataset_id_becomes_blocked(apify):
    apify.script = [{}]  # a run object with no default dataset
    with pytest.raises(ExtractorBlockedException):
        await _extractor().fetch_products(["k"])


async def test_dataset_read_failure_becomes_blocked(apify):
    apify.script = [[_item()]]
    apify.dataset_error = RuntimeError("dataset unavailable")
    with pytest.raises(ExtractorBlockedException):
        await _extractor().fetch_products(["k"])
