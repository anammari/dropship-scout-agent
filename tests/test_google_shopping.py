"""Offline unit tests for Step 3 (updated pipeline): the Apify Google
Shopping scraper wrapper and the LLM gold-product curator.

Zero network and zero Apify spend: the actor SDK is a scripted fake
(`actor().call()` + `dataset().iterate_items()`), the LLM is an
`httpx.MockTransport`, and the conftest credential scrub keeps the real
`APIFY_TOKEN`/`LLM_API_KEY` out of every assertion. Item fixtures follow the
actor README's documented output shape (title/source/link/price/delivery/
imageUrl/rating/ratingCount/query), pinned by the live spike.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.config import settings
from src.evaluators.gold_curator import (
    SYSTEM_MESSAGE,
    GoldCurationConfigError,
    GoldCurationError,
    GoldProductCurator,
)
from src.extractors.google_shopping import (
    ApifyNotConfiguredError,
    GoogleShoppingError,
    GoogleShoppingRunFailedError,
    GoogleShoppingScraper,
    ShoppingRow,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _actor_item(**overrides):
    """One dataset item in the actor README's documented shape."""
    item = {
        "title": "French Press Coffee Maker 1L Stainless",
        "source": "Example Store AU",
        "link": "https://shop.example.au/products/french-press-1l",
        "price": "$39.95",
        "delivery": "$6.99 shipping",
        "imageUrl": "https://encrypted-tbn1.gstatic.com/shopping?q=x",
        "rating": 4.7,
        "ratingCount": 1203,
        "offers": "4",
        "productId": "14706046030051867026",
        "position": 1,
        "query": "french press",
    }
    item.update(overrides)
    return item


class _FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return iter(self._items)


class _FakeActorClient:
    def __init__(self, run):
        self._run = run
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self._run


class _FakeApifyClient:
    """Duck-types apify-client 3.2.0: call() returns a Run-attributes object
    (id / status / default_dataset_id / usage_total_usd), not a dict."""

    def __init__(self, run, items):
        if run.default_dataset_id is None:
            run.default_dataset_id = "ds-1"
        self.actor_client = _FakeActorClient(run)
        self._items = items
        self.requested_actors = []

    def actor(self, actor_id):
        self.requested_actors.append(actor_id)
        return self.actor_client

    def dataset(self, dataset_id):
        return _FakeDataset(self._items)


def _run(**overrides):
    base = {
        "id": "run-1",
        "status": "SUCCEEDED",
        "default_dataset_id": "ds-1",
        "usage_total_usd": 0.0035,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _scraper(items=None, run=None, **kwargs):
    if items is None:
        items = [_actor_item()]
    if run is None:
        run = _run()
    client = _FakeApifyClient(run, items)
    kwargs.setdefault("apify_client", client)
    kwargs.setdefault("actor", "fake/actor")
    return GoogleShoppingScraper(**kwargs), client


def _row(**overrides):
    base = {
        "title": "French Press Coffee Maker 1L Stainless",
        "url": "https://shop.example.au/products/french-press-1l",
        "merchant": "Example Store AU",
        "price_text": "$39.95",
        "delivery_text": "$6.99 shipping",
        "image_url": "https://encrypted-tbn1.gstatic.com/shopping?q=x",
        "description": None,
        "rating": 4.7,
        "rating_count": 1203,
        "source_keyword": "french press",
    }
    base.update(overrides)
    return ShoppingRow(**base)


def _curator(bodies, log=None, finish_reason="stop", **kwargs):
    """Curator over a scripted LLM transport; one body per chat call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": bodies[len(log) - 1 if log is not None else 0]},
                        "finish_reason": finish_reason,
                    }
                ]
            },
        )

    kwargs.update(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    return GoldProductCurator(**kwargs)


def _selection(url, **overrides):
    body = {
        "url": url,
        "pillar": "curated_home",
        "compliance_note": "Inert stainless kitchen tool; no formulation or IP.",
        "unit_economics_note": None,
    }
    body.update(overrides)
    return json.dumps({"products": [body]})


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


def test_scrape_parses_readme_row_shape():
    scraper, client = _scraper()
    rows = scraper.scrape_keywords(["french press"])

    (call,) = client.actor_client.calls
    assert call["run_input"]["queries"] == ["french press"]
    assert call["run_input"]["country"] == "au"
    assert call["run_input"]["max_pages"] == 1
    assert client.requested_actors == ["fake/actor"]

    (row,) = rows
    assert row.title == "French Press Coffee Maker 1L Stainless"
    assert row.url == "https://shop.example.au/products/french-press-1l"
    assert row.merchant == "Example Store AU"
    assert row.price_text == "$39.95"
    assert row.delivery_text == "$6.99 shipping"
    assert row.rating == 4.7
    assert row.rating_count == 1203
    assert row.source_keyword == "french press"
    assert scraper.last_run["status"] == "SUCCEEDED"
    assert scraper.last_run["result_count"] == 1


def test_scrape_batches_all_keywords_into_one_run():
    scraper, client = _scraper(results_per_keyword=10, max_charge_usd=0.05)
    scraper.scrape_keywords(["french press", "bath pillow", "gua sha"])

    (call,) = client.actor_client.calls  # exactly one actor call
    assert call["run_input"]["queries"] == ["french press", "bath pillow", "gua sha"]
    assert call["run_input"]["num"] == "10"  # actor's `num` is string-typed
    # The hard spend ceiling rides the run options (apify-client 3.x).
    assert str(call["max_total_charge_usd"]) == "0.05"


def test_scrape_rejects_disallowed_results_per_keyword():
    # Live finding (2026-09-28): the actor's `num` is a closed set; anything
    # else must fail before a run is started and money spent.
    scraper_error = pytest.raises(GoogleShoppingError, match="allowed num")
    with scraper_error:
        GoogleShoppingScraper(
            apify_client=_FakeApifyClient(_run(), []), results_per_keyword=3
        )


def test_scrape_unfinished_run_raises():
    scraper, client = _scraper()
    # SDK 3.x: call() returns None when the wait window elapses.
    client.actor_client._run = None
    with pytest.raises(GoogleShoppingRunFailedError, match="did not finish"):
        scraper.scrape_keywords(["french press"])


def test_scrape_tolerates_misattributed_query(caplog):
    scraper, _ = _scraper(items=[_actor_item(query="unrequested term")])
    with caplog.at_level(logging.WARNING, logger="src.extractors.google_shopping"):
        rows = scraper.scrape_keywords(["french press"])
    assert len(rows) == 1
    assert "not a requested keyword" in caplog.text


def test_scrape_skips_rows_without_title_or_link():
    items = [
        _actor_item(title=""),
        _actor_item(link=""),
        _actor_item(link=None),
        _actor_item(),
    ]
    scraper, _ = _scraper(items=items)
    rows = scraper.scrape_keywords(["french press"])
    assert len(rows) == 1


def test_scrape_missing_optional_fields_become_none():
    scraper, _ = _scraper(
        items=[{"title": "Bare row", "link": "https://x.example/p/1", "query": "q"}]
    )
    (row,) = scraper.scrape_keywords(["q"])
    assert row.price_text is None
    assert row.rating is None
    assert row.rating_count is None
    assert row.merchant is None


def test_scrape_missing_token_raises(monkeypatch):
    monkeypatch.setattr(settings, "APIFY_TOKEN", None)
    with pytest.raises(ApifyNotConfiguredError, match="APIFY_TOKEN"):
        GoogleShoppingScraper()


def test_scrape_failed_run_raises():
    scraper, _ = _scraper(run=_run(id="run-9", status="FAILED"))
    with pytest.raises(GoogleShoppingRunFailedError, match="FAILED"):
        scraper.scrape_keywords(["french press"])


def test_scrape_empty_dataset_raises():
    scraper, _ = _scraper(items=[])
    with pytest.raises(GoogleShoppingError, match="produced no rows"):
        scraper.scrape_keywords(["french press"])


def test_scrape_empty_keywords_raises():
    scraper, _ = _scraper()
    with pytest.raises(GoogleShoppingError, match="no keywords"):
        scraper.scrape_keywords([])


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------


def test_curate_assembles_facts_from_rows_not_the_llm():
    row = _row()
    log = []
    curator = _curator([_selection(row.url)], log)
    products = curator.curate([row])

    assert len(products) == 1
    product = products[0]
    # Every product fact comes verbatim from the scraped row; the LLM only
    # contributed pillar / compliance_note / unit_economics_note.
    assert product.name == row.title
    assert product.url == row.url
    assert product.retail_price_text == "$39.95"
    assert product.pillar == "curated_home"
    assert "Inert stainless" in product.compliance_note
    assert product.unit_economics_note is None
    assert product.demand_evidence == "rating 4.7/5, 1203 reviews (Google Shopping AU)"
    assert product.source_keyword == "french press"

    (payload,) = log
    assert payload["messages"][0]["content"] == SYSTEM_MESSAGE
    user = payload["messages"][1]["content"]
    assert row.url in user and "french press" in user


def test_curate_drops_invented_url(caplog):
    curator = _curator([_selection("https://hallucinated.example/p/9")])
    with caplog.at_level(logging.WARNING, logger="src.evaluators.gold_curator"):
        products = curator.curate([_row()])
    assert products == []
    assert "no scraped source row" in caplog.text


def test_curate_demand_evidence_absent_without_kpis():
    row = _row(rating=None, rating_count=None)
    curator = _curator([_selection(row.url)])
    (product,) = curator.curate([row])
    assert product.demand_evidence == "not_available_from_source"


def test_curate_drops_unknown_pillar(caplog):
    row = _row()
    curator = _curator([_selection(row.url, pillar="beauty")])
    with caplog.at_level(logging.WARNING, logger="src.evaluators.gold_curator"):
        assert curator.curate([row]) == []
    assert "unknown pillar" in caplog.text


def test_curate_dedupes_repeated_urls():
    row = _row()
    body = json.dumps(
        {"products": [json.loads(_selection(row.url))["products"][0]] * 2}
    )
    curator = _curator([body])
    assert len(curator.curate([row])) == 1


def test_curate_unselected_rows_stay_out():
    rows = [_row(), _row(url="https://shop.example.au/products/other")]
    curator = _curator([_selection(rows[0].url)])
    products = curator.curate(rows)
    assert [p.url for p in products] == [rows[0].url]


def test_curate_handles_fenced_json():
    row = _row()
    curator = _curator(["```json\n" + _selection(row.url) + "\n```"])
    assert len(curator.curate([row])) == 1


def test_curate_salvages_truncated_response():
    rows = [_row(url=f"https://shop.example.au/products/{i}") for i in range(3)]
    full = json.dumps(
        {"products": [json.loads(_selection(r.url))["products"][0] for r in rows]}
    )
    curator = _curator([full[: len(full) - 30]])
    products = curator.curate(rows)
    # The last (cut) selection is gone; the complete ones survive.
    assert len(products) == 2


def test_curate_rejects_unsalvageable_response():
    curator = _curator(["the model replied in prose, not JSON"])
    with pytest.raises(GoldCurationError, match="could not be parsed"):
        curator.curate([_row()])


def test_curate_empty_content_reports_finish_reason():
    # Live finding (2026-09-28): the reasoning model can exhaust `max_tokens`
    # thinking and return empty `content` with finish_reason "length". That is
    # a distinct, actionable failure from a JSON parse error.
    curator = _curator([""], finish_reason="length")
    with pytest.raises(GoldCurationError, match="empty content"):
        curator.curate([_row()])


def test_curate_batches_rows_and_merges():
    rows = [_row(url=f"https://shop.example.au/products/{i}") for i in range(2)]
    bodies = [_selection(r.url) for r in rows]
    log = []
    curator = _curator(bodies, log, rows_per_call=1)
    assert len(curator.curate(rows)) == 2
    assert len(log) == 2  # one LLM call per batch


def test_curate_http_error_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    curator = GoldProductCurator(
        client=httpx.Client(transport=transport),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    with pytest.raises(GoldCurationError, match="LLM HTTP 500"):
        curator.curate([_row()])


def test_curate_transport_error_raises():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    curator = GoldProductCurator(
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    with pytest.raises(GoldCurationError, match="LLM transport error"):
        curator.curate([_row()])


def test_curate_missing_llm_config_raises(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(GoldCurationConfigError) as excinfo:
        GoldProductCurator()
    assert "LLM_BASE_URL" in str(excinfo.value)
    assert "LLM_API_KEY" in str(excinfo.value)


def test_curate_empty_rows_raises():
    curator = _curator([_selection("https://x.example/1")])
    with pytest.raises(GoldCurationError, match="no scraped rows"):
        curator.curate([])


# ---------------------------------------------------------------------------
# Runner glue (keyword loader)
# ---------------------------------------------------------------------------


def test_step2_keyword_loader(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "step3_runner",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_gold_standard_research.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    payload = tmp_path / "kw.json"
    payload.write_text(
        json.dumps({"keywords": [{"keyword": "french press"}, {"keyword": ""}, {"pillar": "other"}]})
    )
    assert runner.load_step2_keywords(payload) == ["french press"]

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"keywords": []}))
    with pytest.raises(SystemExit):
        runner.load_step2_keywords(empty)