"""Offline unit tests for the Step-3 HasData Google Shopping source.

Zero network and zero HasData credits: the API is an `httpx.MockTransport`,
and the row fixtures follow the LIVE payload shape pinned by the 2026-10-07
probe (`q=french press`, `gl=au`) — `source` is the seller, `reviews` is the
review count, and no `link`/`productLink` field exists at all. Tests also
cover the reference guide's documented spellings (`seller`, `ratingCount`,
`link`/`productLink`), since the guide and the live payload disagree and the
parser must survive either.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from src.config import settings
from src.evaluators.gold_curator import GoldProductCurator
from src.extractors.google_shopping import ShoppingRow
from src.extractors.google_shopping_hasdata import (
    GOOGLE_DOMAINS,
    HasDataNotConfiguredError,
    HasDataShoppingError,
    HasDataShoppingRunFailedError,
    HasDataShoppingScraper,
    parse_response,
)

API_KEY = "mock-test-hasdata_api_key"


# ---------------------------------------------------------------------------
# Fixtures — the live payload shape, and the guide's documented shape
# ---------------------------------------------------------------------------


def _live_item(**overrides):
    """One shoppingResults item exactly as the live 2026-10-07 probe returned."""
    item = {
        "position": 2,
        "category": "French presses",
        "title": "Bodum Chambord French Press Coffee Maker",
        "productId": "11588040498888443284",
        "price": "$89.95",
        "extractedPrice": 89.95,
        "originalPrice": "Usually $99",
        "extractedOriginalPrice": 99,
        "rating": 4.7,
        "reviews": 234,
        "source": "Myer",
        "thumbnail": "https://files.hasdata.com/acct/abc.webp",
        "delivery": "Free delivery",
        "immersiveProductPageToken": "eyJ2ZXJzaW9uIjoyfQ",
    }
    item.update(overrides)
    return item


def _payload(items, status="ok"):
    return {
        "requestMetadata": {"id": "req-1", "status": status},
        "searchInformation": {"totalResults": "100000"},
        "shoppingResults": items,
        "inlineShoppingResults": [],
        "filters": [],
    }


def _scraper(handler=None, **kwargs):
    """A scraper over a scripted transport; returns (scraper, requests list)."""
    requests = []
    scripted = handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if scripted is not None:
            return scripted(request)
        kw = dict(request.url.params).get("q", "")
        return httpx.Response(200, json=_payload([_live_item(title=f"row for {kw}")]))

    kwargs.setdefault("api_key", API_KEY)
    kwargs.setdefault("base_url", "https://api.test")
    kwargs.setdefault("results_per_keyword", 40)
    client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    return HasDataShoppingScraper(client=client, **kwargs), requests


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_request_carries_the_au_market_pin_and_key():
    scraper, requests = _scraper()
    scraper.scrape_keywords(["french press"])

    (request,) = requests
    assert request.url.path == "/scrape/google/shopping"
    assert request.url.params["q"] == "french press"
    # The market pin is load-bearing: Google Shopping quotes differ per gl,
    # exactly as the AliExpress DS Center's do.
    assert request.url.params["gl"] == "au"
    assert request.url.params["domain"] == "google.com.au"
    assert request.url.params["hl"] == "en"
    assert request.url.params["deviceType"] == "desktop"
    assert request.url.params["start"] == "0"
    assert request.headers["x-api-key"] == API_KEY


def test_one_request_per_keyword():
    # Credit accounting depends on this: 10 credits per keyword, so the
    # request count IS the keyword count.
    scraper, requests = _scraper()
    rows = scraper.scrape_keywords(["french press", "coffee press", "cake stand"])
    assert len(requests) == 3
    assert len(rows) == 3
    assert scraper.last_run["requests"] == 3
    assert scraper.last_run["credits"] == 30
    assert scraper.last_run["source"] == "hasdata"


def test_each_row_is_attributed_to_its_own_keyword():
    # HasData items carry no `query` field (unlike the Apify actor's rows), so
    # the attribution has to come from the request.
    scraper, _ = _scraper()
    rows = scraper.scrape_keywords(["french press", "tea infuser"])
    assert [row.source_keyword for row in rows] == ["french press", "tea infuser"]


def test_google_domain_follows_the_pipeline_country(monkeypatch):
    monkeypatch.setattr(settings, "TARGET_COUNTRY", "GB")
    scraper, requests = _scraper()
    scraper.scrape_keywords(["french press"])
    (request,) = requests
    assert request.url.params["gl"] == "gb"
    assert request.url.params["domain"] == "google.co.uk"


def test_unknown_country_falls_back_to_google_com(monkeypatch):
    monkeypatch.setattr(settings, "TARGET_COUNTRY", "ZZ")
    scraper, requests = _scraper()
    scraper.scrape_keywords(["french press"])
    (request,) = requests
    assert request.url.params["domain"] == "google.com"
    assert "AU" in GOOGLE_DOMAINS  # the pinned market stays mapped


# ---------------------------------------------------------------------------
# Row parsing — live shape and guide shape
# ---------------------------------------------------------------------------


def test_parses_the_live_payload_shape():
    scraper, _ = _scraper(
        handler=lambda r: httpx.Response(200, json=_payload([_live_item()]))
    )
    (row,) = scraper.scrape_keywords(["french press"])
    # Live: `source` is the seller and `reviews` is the review count.
    assert row.title == "Bodum Chambord French Press Coffee Maker"
    assert row.merchant == "Myer"
    assert row.price_text == "$89.95"
    assert row.delivery_text == "Free delivery"
    assert row.rating == 4.7
    assert row.rating_count == 234
    assert row.image_url == "https://files.hasdata.com/acct/abc.webp"
    assert row.source_keyword == "french press"


#: One VERBATIM row from the live 2026-10-07 AU probe (only the two large
#: opaque blobs — `hasdataLink` and the tail of `immersiveProductPageToken` —
#: are trimmed). It pins the real field set, including the fields the parser
#: has no use for (`position`, `category`, `extractedPrice`,
#: `immersiveProductPageToken`), so a schema drift that renames a field the
#: parser DOES read shows up as a failure here rather than mid-production.
LIVE_ROW = {
    "position": 2,
    "category": "Ceramic French presses",
    "title": "Frank Green Ceramic French Press",
    "productId": "432592409323461293",
    "immersiveProductPageToken": "eyJyZHMiOiJQQ183ODUyNDUwNTk2MDE3ODY0MTgw...",
    "price": "$59.95",
    "extractedPrice": 59.95,
    "source": "frank green Australia",
    "reviews": 36,
    "rating": 4.1,
    "thumbnail": (
        "https://files.hasdata.com/8Z7H4mSRvfcc7fKal3nJlBJosWP2/"
        "39b7e7cb-fe79-427c-ac90-b16394ac6ce1.webp"
    ),
}


def test_parses_a_verbatim_live_row():
    (row,) = parse_response(_payload([LIVE_ROW]), "french press")
    assert row.title == "Frank Green Ceramic French Press"
    assert row.merchant == "frank green Australia"
    assert row.price_text == "$59.95"
    assert row.rating == 4.1
    assert row.rating_count == 36
    assert row.url == (
        "https://www.google.com.au/shopping/product/432592409323461293"
    )
    assert row.image_url == LIVE_ROW["thumbnail"]
    # Live rows carry no delivery field; it must not be invented.
    assert row.delivery_text is None


def test_parses_the_reference_guide_shape_too():
    # The guide documents `link`, `productLink`, `seller` and `ratingCount`,
    # none of which the live payload returns; the parser reads both sets.
    documented = _live_item(
        link="https://www.google.com/shopping/product/abc",
        seller="Example Store AU",
        ratingCount=88,
    )
    documented.pop("source")
    documented.pop("reviews")
    (row,) = parse_response(_payload([documented]), "french press")
    assert row.url == "https://www.google.com/shopping/product/abc"
    assert row.merchant == "Example Store AU"
    assert row.rating_count == 88


def test_product_link_wins_over_the_canonical_fallback():
    item = _live_item(productLink="https://merchant.example.au/p/9")
    (row,) = parse_response(_payload([item]), "french press")
    assert row.url == "https://merchant.example.au/p/9"


def test_missing_link_falls_back_to_the_canonical_product_identity():
    # The live payload exposes no listing url, so the row identity is derived
    # from its own productId — the same derived-not-invented shape as the CJ
    # extractor's /product/{pid}.html fallback.
    (row,) = parse_response(_payload([_live_item()]), "french press")
    assert row.url == (
        "https://www.google.com.au/shopping/product/11588040498888443284"
    )


def test_rows_sharing_a_product_id_share_an_identity():
    # Live: 65 rows carried only 55 distinct productIds (the same product from
    # several merchants). A shared identity is what lets the curator collapse
    # them, as its prompt already asks.
    items = [
        _live_item(source="Kmart", price="$23.95"),
        _live_item(source="Myer", price="$89.95"),
    ]
    rows = parse_response(_payload(items), "french press")
    assert len(rows) == 2
    assert rows[0].url == rows[1].url


def test_inline_shopping_results_are_merged():
    payload = _payload([])
    payload["inlineShoppingResults"] = [_live_item(title="Inline row")]
    rows = parse_response(payload, "french press")
    assert [row.title for row in rows] == ["Inline row"]


def test_row_without_title_or_identity_is_dropped(caplog):
    payload = _payload([
        _live_item(title=""),
        _live_item(productId=""),
        _live_item(),
    ])
    with caplog.at_level(logging.WARNING, logger="src.extractors.google_shopping_hasdata"):
        rows = parse_response(payload, "french press")
    assert len(rows) == 1
    assert "without title/identity" in caplog.text


def test_missing_optional_fields_become_none():
    payload = _payload([{"title": "Bare row", "productId": "123"}])
    (row,) = parse_response(payload, "q")
    assert row.merchant is None
    assert row.price_text is None
    assert row.rating is None
    assert row.rating_count is None
    assert row.image_url is None
    assert row.url == "https://www.google.com.au/shopping/product/123"


def test_non_object_entries_are_skipped():
    payload = _payload(["not a dict", None, _live_item()])
    assert len(parse_response(payload, "q")) == 1


# ---------------------------------------------------------------------------
# Local row cap
# ---------------------------------------------------------------------------


def test_local_cap_truncates_the_grid_without_touching_the_request():
    items = [_live_item(title=f"row {i}", productId=f"p{i}") for i in range(5)]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_payload(items))

    scraper, _ = _scraper(handler=handler, results_per_keyword=2)
    rows = scraper.scrape_keywords(["french press"])
    # The cap is local: the grid arrives whole and one request still happens.
    assert len(rows) == 2
    assert len(requests) == 1
    assert scraper.last_run["credits"] == 10


def test_non_positive_cap_fails_before_any_request():
    with pytest.raises(HasDataShoppingError, match="would discard every row"):
        HasDataShoppingScraper(api_key=API_KEY, results_per_keyword=0)


# ---------------------------------------------------------------------------
# Failures — all fail closed
# ---------------------------------------------------------------------------


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "HASDATA_API_KEY", None)
    with pytest.raises(HasDataNotConfiguredError, match="HASDATA_API_KEY"):
        HasDataShoppingScraper()


def test_non_200_raises_and_never_echoes_the_key(caplog):
    def handler(request):
        return httpx.Response(401, text="unauthorized")

    scraper, _ = _scraper(handler=handler)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HasDataShoppingRunFailedError, match="HTTP 401"):
            scraper.scrape_keywords(["french press"])
    assert API_KEY not in caplog.text


def test_transport_error_raises():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    scraper, _ = _scraper(handler=refuse)
    with pytest.raises(HasDataShoppingRunFailedError, match="call failed"):
        scraper.scrape_keywords(["french press"])


def test_non_json_response_raises():
    scraper, _ = _scraper(handler=lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(HasDataShoppingRunFailedError, match="non-JSON"):
        scraper.scrape_keywords(["french press"])


def test_error_status_in_metadata_raises():
    payload = _payload([_live_item()], status="error")
    scraper, _ = _scraper(handler=lambda r: httpx.Response(200, json=payload))
    with pytest.raises(HasDataShoppingRunFailedError, match="status 'error'"):
        scraper.scrape_keywords(["french press"])


def test_empty_grid_raises():
    scraper, _ = _scraper(
        handler=lambda r: httpx.Response(200, json=_payload([]))
    )
    with pytest.raises(HasDataShoppingRunFailedError, match="no usable rows"):
        scraper.scrape_keywords(["french press"])


def test_no_keywords_raises():
    scraper, _ = _scraper()
    with pytest.raises(HasDataShoppingError, match="no keywords"):
        scraper.scrape_keywords([])


def test_a_later_keyword_failure_fails_the_run():
    # Fail closed: a half-scraped gold list would silently under-represent
    # the Step-2 keywords the run claims to cover.
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_payload([_live_item()]))

    scraper, _ = _scraper(handler=handler)
    with pytest.raises(HasDataShoppingRunFailedError, match="HTTP 500"):
        scraper.scrape_keywords(["french press", "coffee press"])


# ---------------------------------------------------------------------------
# Downstream compatibility — the shared ShoppingRow contract
# ---------------------------------------------------------------------------


def test_parsed_rows_are_the_shared_shopping_row_contract():
    scraper, _ = _scraper()
    (row,) = scraper.scrape_keywords(["french press"])
    assert isinstance(row, ShoppingRow)
    # The dump/replay path round-trips through ShoppingRow(**entry).
    assert ShoppingRow(**vars(row)) == row


def test_parsed_rows_clear_the_curators_demand_evidence_filter():
    # The pipeline's gold-standard attribute: a row must carry a rating or a
    # review count to become a gold product. A high-rating AU listing is the
    # case the smoke test targeted.
    scraper, _ = _scraper()
    (row,) = scraper.scrape_keywords(["french press"])
    assert row.rating is not None or row.rating_count

    curator = GoldProductCurator(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "products": [
                                                {
                                                    "url": row.url,
                                                    "pillar": "curated_home",
                                                    "compliance_note": "Inert kitchen tool.",
                                                    "unit_economics_note": None,
                                                }
                                            ]
                                        }
                                    )
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )
            )
        ),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    (product,) = curator.curate([row])
    assert product.url == row.url
    assert product.name == row.title
    assert product.retail_price_text == "$89.95"
    assert "4.7/5" in product.demand_evidence
    assert "234 reviews" in product.demand_evidence


# ---------------------------------------------------------------------------
# Runner glue — source selection and the spend banner
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "step3_runner_hasdata",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_gold_standard_research.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_build_scraper_selects_the_requested_source():
    from src.extractors.google_shopping import GoogleShoppingScraper

    runner = _runner()
    assert isinstance(runner.build_scraper("apify", 10), GoogleShoppingScraper)
    assert isinstance(
        runner.build_scraper("hasdata", 40), HasDataShoppingScraper
    )


def test_spend_plan_prices_hasdata_in_credits_and_apify_in_usd(capsys):
    runner = _runner()
    runner.print_spend_plan("hasdata", ["a", "b"], 40)
    hasdata = capsys.readouterr().out
    assert "2 keyword(s) x 1 request" in hasdata
    assert "20 credit(s)" in hasdata
    assert "$" not in hasdata

    runner.print_spend_plan("apify", ["a", "b"], 10)
    apify = capsys.readouterr().out
    assert "planned Apify spend" in apify
    assert "$" in apify


def test_source_label_names_the_active_source():
    runner = _runner()
    assert runner.source_label("hasdata") == "hasdata-google-shopping"
    assert runner.source_label("apify") == settings.APIFY_GS_ACTOR


def test_default_source_is_apify():
    # An existing invocation must be unchanged by the module gaining a source.
    runner = _runner()
    assert runner.SOURCES[0] == "apify"
