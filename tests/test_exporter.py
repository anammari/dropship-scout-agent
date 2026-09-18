"""Offline unit tests for `src.exporter` (plan F.5 export contract).

Image downloading is served by a mocked httpx client (real PIL-encoded
bytes) — no network, no LLM. Verifies the 3-valid-image hard gate, the
exact metadata.json contract (supplier data verbatim, marketing fields
from the LLM), directory numbering, and per-candidate failure isolation.
"""

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from src.exporter import CandidateExporter, ExportResult, IMAGE_SOURCE
from src.models import (
    ProductCandidateEvaluation,
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)
from io import BytesIO
import os

_ALI_URL = "https://www.aliexpress.com/item/1005006112233445.html"
_IMAGES = [
    "https://ae01.alicdn.com/kf/S1.jpg",
    "https://ae01.alicdn.com/kf/S2.jpg",
    "https://ae01.alicdn.com/kf/S3.jpg",
]

_blob_cache = {}


def _jpeg(width: int, height: int) -> bytes:
    key = (width, height)
    if key not in _blob_cache:
        img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        _blob_cache[key] = buf.getvalue()
    return _blob_cache[key]


class FakeAsyncClient:
    def __init__(self):
        self.responses = {}
        self.requested = []
        self.closed = False

    def serve(self, url, content, content_type="image/jpeg"):
        resp = httpx.Response(
            200,
            headers={"content-type": content_type},
            content=content,
        )
        self.responses[url] = resp
        return self

    async def get(self, url):
        self.requested.append(url)
        return self.responses.get(url, httpx.Response(200, content=b""))

    async def aclose(self):
        self.closed = True


def _raw_product() -> RawSupplierProduct:
    return RawSupplierProduct(
        supplier_name="AliExpress",
        supplier_retail_url=_ALI_URL,
        product_title="Ergonomic Desk Cable Spine Organiser",
        product_description="Modular cable spine.",
        price_aud=12.50,
        shipping_cost_aud=4.30,
        image_urls=list(_IMAGES),
    )


def _final() -> ProductCandidateEvaluation:
    provisional = ProvisionalProductEvaluation(
        verdict="ACCEPT",
        niche_category="Home Office",
        problem_solved="Cable clutter",
        suggested_retail_aud=49.99,
        marketing_ad_copy="Tame the cable snake.",
        saturation_risk="LOW",
        target_tags=["dropship", "workspace"],
        shipping_notice_au="7-12 business days",
        key_features=["Modular segments", "Steel base", "Under-desk mount"],
    )
    return ProductCandidateEvaluation.from_raw(provisional, _raw_product())


# ----------------------------------------------------------------------
# metadata.json export contract
# ----------------------------------------------------------------------


async def test_exports_accepted_candidate_with_metadata_and_images(tmp_path):
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    results = await exporter.export_candidates([(_final(), _raw_product())])

    assert len(results) == 1
    result = results[0]
    product_dir = Path(result.product_dir)
    assert product_dir.name == "product-01"
    assert result.supplier_retail_url == _ALI_URL

    image_files = sorted((product_dir / "images").iterdir())
    assert [f.name for f in image_files] == ["image-1.jpg", "image-2.jpg", "image-3.jpg"]

    metadata = json.loads((product_dir / "metadata.json").read_text())
    assert metadata["product_title"] == "Ergonomic Desk Cable Spine Organiser"
    assert metadata["category"] == "Home Office"
    assert metadata["suggested_price_aud"] == 49.99
    assert metadata["estimated_cogs_aud"] == 16.80
    assert metadata["projected_margin_aud"] == 33.19
    assert metadata["marketing_ad_copy"] == "Tame the cable snake."
    assert metadata["features"] == [
        "Modular segments", "Steel base", "Under-desk mount",
    ]
    assert metadata["target_tags"] == ["dropship", "workspace"]
    assert metadata["supplier_name"] == "AliExpress"
    assert metadata["supplier_retail_url"] == _ALI_URL
    assert metadata["image_source"] == IMAGE_SOURCE == "supplier_gallery"
    # The old Meta-ad flow's competitor field is gone from the contract.
    assert "competitor_retail_url" not in metadata


# ----------------------------------------------------------------------
# The 3-valid-image hard gate
# ----------------------------------------------------------------------


async def test_candidate_with_fewer_than_three_valid_images_is_dropped(tmp_path):
    client = FakeAsyncClient()
    client.serve(_IMAGES[0], _jpeg(850, 850))
    client.serve(_IMAGES[1], _jpeg(850, 851))
    # _IMAGES[2] is never served -> falls through to an empty 200 -> fails.
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    results = await exporter.export_candidates([(_final(), _raw_product())])

    assert results == []
    assert exporter.dropped_no_valid_images == 1
    assert not list(tmp_path.iterdir())  # no product directory created


# ----------------------------------------------------------------------
# Batch behavior
# ----------------------------------------------------------------------


async def test_non_accept_candidates_are_skipped_without_downloads(tmp_path):
    client = FakeAsyncClient()
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    provisional = ProvisionalProductEvaluation(
        verdict="REJECT",
        niche_category="x",
        problem_solved="y",
        suggested_retail_aud=1.0,
        marketing_ad_copy="",
        saturation_risk="HIGH",
        target_tags=["ignored"],
        shipping_notice_au="z",
        key_features=[],
    )
    rejected = ProductCandidateEvaluation.from_raw(provisional, _raw_product())
    results = await exporter.export_candidates([(rejected, _raw_product())])
    assert results == []
    assert client.requested == []  # no image downloads attempted
    assert not list(tmp_path.iterdir())


async def test_directory_numbering_continues_past_existing_products(tmp_path):
    (tmp_path / "product-01").mkdir()
    (tmp_path / "product-02").mkdir()
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    results = await exporter.export_candidates([(_final(), _raw_product())])
    assert Path(results[0].product_dir).name == "product-03"


# ----------------------------------------------------------------------
# Cross-run duplicate guard
# ----------------------------------------------------------------------


def _write_package(export_dir: Path, name: str, supplier_retail_url: str) -> Path:
    """A minimal already-exported package, as a previous run would leave it."""
    package = export_dir / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "metadata.json").write_text(
        json.dumps({"supplier_retail_url": supplier_retail_url}), encoding="utf-8"
    )
    return package


async def test_duplicate_url_is_skipped_and_creates_no_directory(tmp_path):
    _write_package(tmp_path, "product-01", _ALI_URL)
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    results = await exporter.export_candidates([(_final(), _raw_product())])

    assert results == []
    assert exporter.skipped_duplicate == 1
    assert exporter.dropped_no_valid_images == 0
    # No download work and no new directory — only the pre-existing package.
    assert client.requested == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["product-01"]


async def test_fresh_url_still_exports_next_to_a_duplicate(tmp_path):
    _write_package(tmp_path, "product-01", _ALI_URL)
    fresh_raw = RawSupplierProduct(
        supplier_name="AliExpress",
        supplier_retail_url="https://www.aliexpress.com/item/1005006999888777.html",
        product_title="Fresh product",
        product_description="Another spine.",
        price_aud=9.00,
        shipping_cost_aud=0.0,
        image_urls=[
            "https://ae01.alicdn.com/kf/F1.jpg",
            "https://ae01.alicdn.com/kf/F2.jpg",
            "https://ae01.alicdn.com/kf/F3.jpg",
        ],
    )
    provisional = ProvisionalProductEvaluation(
        verdict="ACCEPT",
        niche_category="Home",
        problem_solved="Clutter",
        suggested_retail_aud=39.00,
        marketing_ad_copy="Buy it.",
        saturation_risk="LOW",
        target_tags=["dropship"],
        shipping_notice_au="7-12 business days",
        key_features=["A", "B", "C"],
    )
    fresh_final = ProductCandidateEvaluation.from_raw(provisional, fresh_raw)

    client = FakeAsyncClient()
    for i, url in enumerate(fresh_raw.image_urls):
        client.serve(url, _jpeg(870 + i, 870))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    results = await exporter.export_candidates([
        (_final(), _raw_product()),        # duplicate -> skipped
        (fresh_final, fresh_raw),          # fresh -> exported
    ])

    assert len(results) == 1
    # The duplicate was skipped without consuming product-02's slot.
    assert Path(results[0].product_dir).name == "product-02"
    assert exporter.skipped_duplicate == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "product-01", "product-02",
    ]


async def test_duplicate_within_one_batch_is_exported_once(tmp_path):
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    results = await exporter.export_candidates([
        (_final(), _raw_product()),
        (_final(), _raw_product()),
    ])

    assert len(results) == 1
    assert exporter.skipped_duplicate == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["product-01"]


async def test_image_gate_drop_does_not_block_a_later_same_url_candidate(tmp_path):
    # A candidate dropped at the image gate writes no package, so it must
    # not mark its URL as exported and block the next candidate.
    client = FakeAsyncClient()
    # First attempt: thin gallery -> dropped.
    client.serve(_IMAGES[0], _jpeg(850, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    assert await exporter.export_candidates([(_final(), _raw_product())]) == []
    assert exporter.dropped_no_valid_images == 1
    assert not list(tmp_path.iterdir())

    # Second attempt with the same URL, now with a full gallery.
    client2 = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client2.serve(url, _jpeg(850 + i, 850))
    exporter2 = CandidateExporter(export_dir=tmp_path, client=client2)
    results = await exporter2.export_candidates([(_final(), _raw_product())])

    assert len(results) == 1
    assert exporter2.skipped_duplicate == 0


async def test_counters_accumulate_across_export_calls(tmp_path):
    # The orchestrator may call export_candidates once per evaluation round;
    # earlier rounds' drops must not be discarded.
    client = FakeAsyncClient()
    client.serve(_IMAGES[0], _jpeg(850, 850))  # thin -> dropped
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    await exporter.export_candidates([(_final(), _raw_product())])
    await exporter.export_candidates([(_final(), _raw_product())])

    assert exporter.dropped_no_valid_images == 2
    assert exporter.skipped_duplicate == 0


async def test_unreadable_package_is_ignored_rather_than_aborting(tmp_path):
    _write_package(tmp_path, "product-01", _ALI_URL)
    (tmp_path / "product-02").mkdir()
    (tmp_path / "product-02" / "metadata.json").write_text("{ not json")
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    results = await exporter.export_candidates([(_final(), _raw_product())])

    # The good package still provides the dedupe, the malformed one is
    # skipped with a warning, and the fresh URL still exports.
    assert results == []
    assert exporter.skipped_duplicate == 1


async def test_non_product_directories_are_not_scanned_for_duplicates(tmp_path):
    # A stray directory that is not a product-NN package must not be read.
    stray = tmp_path / "notes"
    stray.mkdir()
    (stray / "metadata.json").write_text(json.dumps({"supplier_retail_url": _ALI_URL}))
    client = FakeAsyncClient()
    for i, url in enumerate(_IMAGES):
        client.serve(url, _jpeg(850 + i, 850))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)

    results = await exporter.export_candidates([(_final(), _raw_product())])

    assert len(results) == 1
    assert exporter.skipped_duplicate == 0


async def test_one_broken_candidate_does_not_abort_the_batch(tmp_path):
    second_raw = RawSupplierProduct(
        supplier_name="AliExpress",
        supplier_retail_url="https://www.aliexpress.com/item/9999.html",
        product_title="Second product",
        product_description="Another spine.",
        price_aud=8.00,
        shipping_cost_aud=0.0,
        image_urls=[
            "https://ae01.alicdn.com/kf/T1.jpg",
            "https://ae01.alicdn.com/kf/T2.jpg",
            "https://ae01.alicdn.com/kf/T3.jpg",
        ],
    )
    provisional = ProvisionalProductEvaluation(
        verdict="ACCEPT",
        niche_category="Home",
        problem_solved="Clutter",
        suggested_retail_aud=30.00,
        marketing_ad_copy="Buy it.",
        saturation_risk="MEDIUM",
        target_tags=["dropship"],
        shipping_notice_au="7-12 business days",
        key_features=["A", "B", "C"],
    )
    second_final = ProductCandidateEvaluation.from_raw(provisional, second_raw)

    client = FakeAsyncClient()
    # First product's gallery: fewer than 3 valid -> dropped.
    client.serve(_IMAGES[0], _jpeg(850, 850))
    client.serve(_IMAGES[1], _jpeg(850, 851))
    # Second product's gallery: 3 valid -> exported.
    for i, url in enumerate(second_raw.image_urls):
        client.serve(url, _jpeg(860 + i, 860))
    exporter = CandidateExporter(export_dir=tmp_path, client=client)
    results = await exporter.export_candidates([
        (_final(), _raw_product()),
        (second_final, second_raw),
    ])
    assert len(results) == 1
    assert Path(results[0].product_dir).name == "product-01"  # first dropped, no dir
    assert exporter.dropped_no_valid_images == 1