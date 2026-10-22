"""Unit tests for the Step-5 keyword bank and its supplier ingestion.

Hermetic: the bank loader is exercised on tmp files, and
`ingest_keyword_bank` runs against fake extractors, a fake evaluator and fake
exporters. The real `run_pipeline` IS used (the module reuses it rather than
forking it), so these tests also prove the bank path inherits the existing
per-leg funnel counters, the image-gate accounting and the early-stop rule.
No network, no real engines.

The Morning Anthem ingests through CJdropshipping only, so the single-engine
tests use the real `BANK_ENGINES`. The per-leg fan-out / leg-failure
machinery is still generic (it exists so a second supplier can be added
without a rewrite), so those tests drive it with a synthetic second engine
patched in — coverage is preserved without resurrecting a retired supplier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.config import settings
from src.exporter import ExportResult
from src.extractors.base import (
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
)
from src.models import RawSupplierProduct
from src.pipeline.keyword_bank import (
    BANK_ENGINES,
    BankIngestionFailedError,
    BankKeyword,
    KeywordBankError,
    _default_exporters,
    ingest_keyword_bank,
    load_keyword_bank,
)

# ----------------------------------------------------------------------
# Fixtures and fakes
# ----------------------------------------------------------------------


def _bank_row(
    keyword: str, product: str = "Gold Product", match: str = "exact"
) -> Dict[str, Any]:
    return {
        "keyword": keyword,
        "product": product,
        "pillar": "morning_table",
        "match": match,
        "rationale": "the gold product's own title",
    }


def _write_bank(tmp_path: Path, payload: Any, name: str = "bank.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _product(keyword: str, index: int) -> RawSupplierProduct:
    """A verified CJ listing (the pipeline's only supplier)."""
    return RawSupplierProduct(
        supplier_name="CJdropshipping",
        supplier_retail_url=f"https://cjdropshipping.com/product/{index}.html",
        product_title=f"cjdropshipping {keyword} #{index}",
        product_description="A physical tool.",
        price_aud=5.0,
        shipping_cost_aud=9.0,
        image_urls=[
            f"https://cdn.example.com/cj/{keyword}/{index}-{n}.jpg"
            for n in range(3)
        ],
    )


class _FakeEvaluation:
    """Only `verdict` is read by `run_pipeline`; the fake exporter ignores it."""

    def __init__(self, verdict: str = "ACCEPT") -> None:
        self.verdict = verdict


class _FakeEvaluator:
    def __init__(self, verdict: str = "ACCEPT", log: Optional[list] = None) -> None:
        self.verdict = verdict
        self.log = log if log is not None else []

    async def evaluate(self, product: RawSupplierProduct) -> _FakeEvaluation:
        self.log.append(product.product_title)
        return _FakeEvaluation(self.verdict)


class _FakeExtractor:
    """One engine's fake extractor: recorded calls, scripted products/error."""

    def __init__(
        self,
        engine: str,
        products: List[RawSupplierProduct],
        error: Optional[Exception] = None,
        calls: Optional[list] = None,
    ) -> None:
        self.engine_name = engine
        self._products = products
        self._error = error
        self.calls = calls if calls is not None else []

    async def fetch_products(self, keywords, country: str = "AU"):
        self.calls.append((self.engine_name, keywords[0]))
        if self._error is not None:
            raise self._error
        return list(self._products)


class _FakeExporter:
    """Exports one package per accepted candidate; records every call.

    `skipped_duplicate` / `dropped_no_valid_images` are CUMULATIVE attributes,
    exactly as the real `CandidateExporter` keeps them, and
    `duplicates_from_call` makes every candidate a duplicate from that call
    onward. That is what proves the bank folds these two as deltas.
    """

    def __init__(
        self,
        engine: str,
        export_dir: Optional[str] = None,
        duplicates_from_call: Optional[int] = None,
    ) -> None:
        self.engine = engine
        self.export_dir = export_dir or f"/tmp/{engine}"
        self.dropped_no_valid_images = 0
        self.skipped_duplicate = 0
        self.duplicates_from_call = duplicates_from_call
        self.calls: List[list] = []

    async def export_candidates(self, candidates):
        pairs = list(candidates)
        self.calls.append(pairs)
        is_duplicate_call = (
            self.duplicates_from_call is not None
            and len(self.calls) >= self.duplicates_from_call
        )
        results = []
        for index, (evaluation, product) in enumerate(pairs, start=1):
            if is_duplicate_call:
                self.skipped_duplicate += 1
                continue
            results.append(
                ExportResult(
                    product_dir=f"{self.export_dir}/product-{index:02d}",
                    product_title=product.product_title,
                    metadata_path=f"{self.export_dir}/product-{index:02d}/metadata.json",
                    evaluation=evaluation,
                    supplier_retail_url=product.supplier_retail_url,
                )
            )
        return results


def _factory(
    engine: str,
    products: List[RawSupplierProduct],
    error: Optional[Exception] = None,
    calls: Optional[list] = None,
):
    def build() -> _FakeExtractor:
        return _FakeExtractor(engine, products, error=error, calls=calls)

    return build


def _products_per_engine(
    count: int = 2, engines: Optional[List[str]] = None
) -> Dict[str, List[RawSupplierProduct]]:
    import src.pipeline.keyword_bank as bank_module

    return {
        engine: [_product("kw", index) for index in range(count)]
        for engine in (engines if engines is not None else bank_module.BANK_ENGINES)
    }


async def _ingest(
    keywords: List[BankKeyword],
    tmp_path: Path,
    products: Optional[Dict[str, List[RawSupplierProduct]]] = None,
    target: int = 2,
    calls: Optional[list] = None,
    evaluator: Optional[Any] = None,
    errors: Optional[Dict[str, Exception]] = None,
    engines: Optional[List[str]] = None,
    export_root: Optional[Path] = None,
    exporters: Optional[Dict[str, Any]] = None,
):
    import src.pipeline.keyword_bank as bank_module

    engines = engines or list(bank_module.BANK_ENGINES)
    products = products or _products_per_engine(engines=engines)
    errors = errors or {}
    exporters = exporters or {engine: _FakeExporter(engine) for engine in engines}
    factories = {
        engine: _factory(engine, products.get(engine, []), errors.get(engine), calls)
        for engine in engines
    }
    return await ingest_keyword_bank(
        keywords=keywords,
        target_per_product=target,
        evaluator=evaluator or _FakeEvaluator(),
        exporters=exporters,
        extractor_factories=factories,
        export_root=export_root or tmp_path,
    )


def _keywords(count: int = 2) -> List[BankKeyword]:
    """`count` keywords, each belonging to its OWN product.

    One product per keyword keeps these tests about the per-leg machinery; the
    product-major behaviour (exact first, fallback only to fill) is exercised by
    `_product_keywords` below.
    """
    return [
        BankKeyword(
            keyword=f"keyword {index}",
            product=f"Gold Product {index}",
            pillar="morning_table",
            match="exact",
            rationale="the gold product's own title",
        )
        for index in range(count)
    ]


def _product_keywords(
    exact: Optional[str] = None, fallback: Optional[str] = None
) -> List[BankKeyword]:
    """One product's rows, exact before fallback, as the loader would sort them."""
    rows: List[BankKeyword] = []
    if exact is not None:
        rows.append(
            BankKeyword(
                keyword=exact,
                product="Gold Product",
                pillar="morning_table",
                match="exact",
                rationale="the gold product's own title",
            )
        )
    if fallback is not None:
        rows.append(
            BankKeyword(
                keyword=fallback,
                product="Gold Product",
                pillar="morning_table",
                match="tight_fallback",
                rationale="names the mechanism",
            )
        )
    return rows


#: A synthetic second engine, used ONLY to exercise the generic per-leg
#: fan-out / leg-failure machinery (which the single-supplier bank still
#: carries so a future supplier needs no rewrite). HasData/CJ remain the only
#: real engines.
SECOND_ENGINE = "secondary"


@pytest.fixture
def two_engines(monkeypatch):
    """Patch a second fake engine into the bank's registered engine set."""
    import src.pipeline.keyword_bank as bank_module

    monkeypatch.setattr(
        bank_module, "BANK_ENGINES", ("cjdropshipping", SECOND_ENGINE)
    )
    # The synthetic engine needs a supplier subfolder too, or the summary's
    # export-root map cannot name where its packages would land.
    monkeypatch.setattr(
        bank_module,
        "OPTIMAL_SUPPLIER_DIRS",
        {"cjdropshipping": "cjdropshipping", SECOND_ENGINE: SECOND_ENGINE},
    )
    monkeypatch.setattr(
        settings, "SUPPLIER_PRIORITY_ORDER", ["cjdropshipping", SECOND_ENGINE]
    )
    return ("cjdropshipping", SECOND_ENGINE)


# ----------------------------------------------------------------------
# load_keyword_bank
# ----------------------------------------------------------------------


def test_load_keyword_bank_reads_typed_rows(tmp_path):
    path = _write_bank(tmp_path, {"keywords": [_bank_row("stainless garlic press")]})
    bank = load_keyword_bank(path)
    assert [row.keyword for row in bank] == ["stainless garlic press"]
    assert isinstance(bank[0], BankKeyword)
    assert bank[0].pillar == "morning_table"
    assert bank[0].match == "exact"
    assert bank[0].rationale


def test_load_keyword_bank_accepts_a_bare_list_payload(tmp_path):
    path = _write_bank(tmp_path, [_bank_row("pumice foot file")])
    assert [row.keyword for row in load_keyword_bank(path)] == ["pumice foot file"]


def test_load_keyword_bank_reads_a_fallback_row(tmp_path):
    row = _bank_row("stainless steel garlic press with cleaner", match="tight_fallback")
    path = _write_bank(tmp_path, {"keywords": [row]})
    loaded = load_keyword_bank(path)[0]
    assert loaded.match == "tight_fallback"
    assert loaded.rationale


def test_load_keyword_bank_defaults_to_the_configured_path(tmp_path, monkeypatch):
    path = _write_bank(tmp_path, {"keywords": [_bank_row("garlic press")]})
    monkeypatch.setattr(settings, "MORNING_ANTHEM_KEYWORD_BANK_PATH", str(path))
    assert [row.keyword for row in load_keyword_bank()] == ["garlic press"]


def test_load_keyword_bank_missing_file_names_the_generator(tmp_path):
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(tmp_path / "absent.json")
    message = str(excinfo.value)
    assert "not found" in message
    assert "generate_gold_keywords.py" in message


def test_load_keyword_bank_rejects_malformed_json(tmp_path):
    path = tmp_path / "bank.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "not valid JSON" in str(excinfo.value)


def test_load_keyword_bank_rejects_an_empty_pool(tmp_path):
    path = _write_bank(tmp_path, {"keywords": []})
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "empty" in str(excinfo.value)


def test_load_keyword_bank_rejects_a_payload_without_a_keyword_list(tmp_path):
    path = _write_bank(tmp_path, {"keywords": {"not": "a list"}})
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "must be a JSON list" in str(excinfo.value)


def test_load_keyword_bank_rejects_a_non_object_row(tmp_path):
    path = _write_bank(tmp_path, {"keywords": ["garlic press"]})
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "row 1" in str(excinfo.value)


def test_load_keyword_bank_rejects_a_non_string_keyword(tmp_path):
    row = _bank_row("garlic press")
    row["keyword"] = 42
    path = _write_bank(tmp_path, {"keywords": [row]})
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "no usable 'keyword' string" in str(excinfo.value)


def test_load_keyword_bank_defaults_a_missing_match_field_to_exact(tmp_path):
    # A bank written before the exact-title strategy existed carries no
    # `match`; the loader reads it as an exact keyword rather than dropping it.
    row = _bank_row("stainless garlic press")
    row.pop("match")
    path = _write_bank(tmp_path, {"keywords": [row]})
    assert load_keyword_bank(path)[0].match == "exact"


def test_load_keyword_bank_orders_exact_keywords_before_their_fallbacks(tmp_path):
    # The exact (verbatim-title) keywords ingest first, so the highest-precision
    # query gets the first chance at the per-keyword export slots and a tight
    # fallback only fills what the exact title could not.
    path = _write_bank(
        tmp_path,
        {
            "keywords": [
                _bank_row("stainless steel garlic press", match="tight_fallback"),
                _bank_row("Stainless Steel Garlic Press", match="exact"),
                _bank_row("garlic press with cleaner", match="tight_fallback"),
                _bank_row("Olive Wood Serving Board", match="exact"),
            ]
        },
    )
    assert [row.match for row in load_keyword_bank(path)] == [
        "exact",
        "exact",
        "tight_fallback",
        "tight_fallback",
    ]


def test_load_keyword_bank_rejects_a_blank_keyword(tmp_path):
    path = _write_bank(tmp_path, {"keywords": [_bank_row("   ")]})
    with pytest.raises(KeywordBankError):
        load_keyword_bank(path)


def test_load_keyword_bank_rejects_a_banned_token_keyword(tmp_path):
    # Defence in depth on top of the Step-4 pool validator: a bank that
    # reached disk some other way still cannot smuggle an AICIS token into a
    # supplier query.
    path = _write_bank(tmp_path, {"keywords": [_bank_row("jade gua sha tool")]})
    with pytest.raises(KeywordBankError) as excinfo:
        load_keyword_bank(path)
    assert "banned" in str(excinfo.value)
    assert "jade" in str(excinfo.value)


# ----------------------------------------------------------------------
# ingest_keyword_bank — per-engine fan-out
# ----------------------------------------------------------------------


async def test_ingest_runs_the_registered_engine_for_every_keyword(tmp_path):
    keywords = _keywords(3)
    calls: list = []
    await _ingest(keywords, tmp_path, calls=calls)
    assert sorted(calls) == sorted(
        ("cjdropshipping", keyword.keyword) for keyword in keywords
    )
    assert list(BANK_ENGINES) == ["cjdropshipping"]


async def test_ingest_runs_every_registered_engine_for_every_keyword(
    tmp_path, two_engines
):
    # The fan-out is generic: with a second engine registered, every keyword
    # runs through EVERY engine (the Morning Anthem registers one, but the
    # machinery is what lets a second supplier be added without a rewrite).
    keywords = _keywords(3)
    calls: list = []
    await _ingest(keywords, tmp_path, calls=calls)
    assert sorted(calls) == sorted(
        (engine, keyword.keyword)
        for keyword in keywords
        for engine in two_engines
    )


async def test_ingest_shares_one_evaluator_across_engines(tmp_path):
    log: list = []
    await _ingest(_keywords(1), tmp_path, evaluator=_FakeEvaluator(log=log))
    # The evaluator saw the leg's products — the leg is a run_pipeline call of
    # its own, not a chain that stops at the first engine.
    assert log and all(title.startswith("cjdropshipping") for title in log)


async def test_ingest_folds_counters_across_per_leg_summaries(tmp_path):
    summary = await _ingest(_keywords(2), tmp_path, target=2)
    assert summary.keywords_total == 2
    assert summary.products_attempted == 2
    for engine in BANK_ENGINES:
        counters = summary.per_engine[engine]
        # 2 keywords x 2 products per leg, all ACCEPTed and exported.
        assert counters["keywords_run"] == 2
        assert counters["leg_failures"] == 0
        assert counters["candidates_scraped"] == 4
        assert counters["evaluated"] == 4
        assert counters["accepted"] == 4
        assert counters["rejected"] == 0
        assert counters["candidates_exported"] == 4
    assert summary.total_exports == 4


async def test_ingest_counts_rejected_candidates_per_engine(tmp_path):
    summary = await _ingest(
        _keywords(1), tmp_path, target=2, evaluator=_FakeEvaluator(verdict="REJECT")
    )
    for engine in BANK_ENGINES:
        assert summary.per_engine[engine]["evaluated"] == 2
        assert summary.per_engine[engine]["rejected"] == 2
        assert summary.per_engine[engine]["accepted"] == 0
        assert summary.per_engine[engine]["candidates_exported"] == 0
    assert summary.total_exports == 0


async def test_ingest_stops_a_leg_at_the_target_per_keyword(tmp_path):
    summary = await _ingest(_keywords(1), tmp_path, target=1)
    # 2 products available per leg, target 1: the leg's early stop is the
    # existing run_pipeline rule, inherited unchanged.
    for engine in BANK_ENGINES:
        assert summary.per_engine[engine]["candidates_exported"] == 1


async def test_ingest_folds_exporter_counters_as_deltas_not_running_totals(tmp_path):
    # The bank reuses ONE exporter across an engine's legs, and the exporter's
    # duplicate / no-image counters are cumulative — so each leg's
    # PipelineSummary reports the running total, not that leg's delta. The fold
    # must add the delta; the invariant that proves it is that the folded
    # per-engine total equals the exporter's own final count. (Folding the
    # running totals instead would double-count every leg.)
    keywords = _keywords(4)
    exporters = {
        engine: _FakeExporter(engine, duplicates_from_call=2)
        for engine in BANK_ENGINES
    }
    summary = await _ingest(keywords, tmp_path, target=1, exporters=exporters)
    for engine in BANK_ENGINES:
        folded = summary.per_engine[engine]["skipped_duplicate"]
        assert folded == exporters[engine].skipped_duplicate
        assert folded > 0
    assert summary.total_exports == 1


async def test_ingest_does_not_go_negative_when_a_leg_scrapes_nothing(tmp_path):
    # `run_pipeline` returns EARLY on an empty funnel — before it copies the
    # exporter's cumulative counters onto the leg summary — so such a leg
    # reports 0 for those two. Folding the leg summary against the exporter's
    # running total therefore went NEGATIVE on the live run (no_images=-8,
    # duplicate=-43 for CJ). The fold reads the exporter itself, so a leg that
    # exported nothing contributes exactly 0.
    keywords = _keywords(3)
    exporters = {}
    for engine in BANK_ENGINES:
        exporter = _FakeExporter(engine)
        # As if an earlier leg had already skipped duplicates and dropped
        # candidates for missing images.
        exporter.skipped_duplicate = 5
        exporter.dropped_no_valid_images = 2
        exporters[engine] = exporter
    summary = await _ingest(
        keywords,
        tmp_path,
        products={engine: [] for engine in BANK_ENGINES},
        exporters=exporters,
    )
    for engine in BANK_ENGINES:
        counters = summary.per_engine[engine]
        assert counters["skipped_duplicate"] == 0
        assert counters["dropped_no_valid_images"] == 0


async def test_ingest_uses_the_configured_target_when_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BANK_TARGET_PER_PRODUCT", 1)
    products = _products_per_engine(count=3)
    engines = list(BANK_ENGINES)
    summary = await ingest_keyword_bank(
        keywords=_keywords(1),
        evaluator=_FakeEvaluator(),
        exporters={engine: _FakeExporter(engine) for engine in engines},
        extractor_factories={
            engine: _factory(engine, products[engine]) for engine in engines
        },
        export_root=tmp_path,
    )
    for engine in engines:
        assert summary.per_engine[engine]["candidates_exported"] == 1


# ----------------------------------------------------------------------
# ingest_keyword_bank — engine failures
# ----------------------------------------------------------------------


async def test_ingest_skips_only_the_failing_leg_and_counts_it(tmp_path, two_engines):
    cj_engine, second = two_engines
    summary = await _ingest(
        _keywords(2),
        tmp_path,
        errors={cj_engine: ExtractorNotConfiguredError("CJ_MCP_TOKEN")},
    )
    cj = summary.per_engine[cj_engine]
    assert cj["keywords_run"] == 0
    assert cj["leg_failures"] == 2
    assert cj["candidates_scraped"] == 0
    # The other engine still ran every keyword and exported.
    other = summary.per_engine[second]
    assert other["keywords_run"] == 2
    assert other["leg_failures"] == 0
    assert other["candidates_exported"] == 4
    assert summary.total_exports == 4


async def test_ingest_skips_a_blocked_leg(tmp_path, two_engines):
    cj_engine, second = two_engines
    summary = await _ingest(
        _keywords(1),
        tmp_path,
        errors={second: ExtractorBlockedException("supplier wall")},
    )
    assert summary.per_engine[second]["leg_failures"] == 1
    assert summary.per_engine[cj_engine]["candidates_exported"] == 2


async def test_ingest_raises_when_every_engine_fails_every_keyword(tmp_path, two_engines):
    cj_engine, second = two_engines
    with pytest.raises(BankIngestionFailedError) as excinfo:
        await _ingest(
            _keywords(2),
            tmp_path,
            errors={
                cj_engine: ExtractorNotConfiguredError("CJ_MCP_TOKEN"),
                second: ExtractorBlockedException("supplier wall"),
            },
        )
    message = str(excinfo.value)
    assert "Every registered bank engine failed" in message
    assert cj_engine in message
    assert second in message


async def test_ingest_does_not_raise_when_one_engine_handles_every_keyword(
    tmp_path, two_engines
):
    cj_engine, _second = two_engines
    summary = await _ingest(
        _keywords(2),
        tmp_path,
        errors={cj_engine: ExtractorNotConfiguredError("CJ_MCP_TOKEN")},
    )
    assert summary.total_exports > 0


async def test_ingest_counts_products_that_returned_no_product(tmp_path):
    products = {engine: [] for engine in BANK_ENGINES}
    summary = await _ingest(_keywords(3), tmp_path, products=products)
    assert summary.products_empty == 3
    # Both legs RAN (an empty catalogue is not a failure) — nothing to export.
    for engine in BANK_ENGINES:
        assert summary.per_engine[engine]["keywords_run"] == 3
        assert summary.per_engine[engine]["leg_failures"] == 0
    assert summary.total_exports == 0


async def test_ingest_with_one_engine_still_raises_when_it_fails_everything(tmp_path):
    # With CJ the only engine, a total failure is the intervention case.
    with pytest.raises(BankIngestionFailedError):
        await _ingest(
            _keywords(1),
            tmp_path,
            engines=["cjdropshipping"],
            errors={"cjdropshipping": ExtractorBlockedException("MCP wall")},
        )


# ----------------------------------------------------------------------
# Export roots
# ----------------------------------------------------------------------


async def test_export_roots_are_the_batch_root(tmp_path):
    summary = await _ingest(_keywords(1), tmp_path)
    assert summary.export_roots == {
        "cjdropshipping": str(tmp_path),
    }


def test_default_exporters_bind_cj_to_batch_root(tmp_path):
    exporters = _default_exporters(tmp_path, list(BANK_ENGINES))
    assert set(exporters) == set(BANK_ENGINES)
    assert Path(exporters["cjdropshipping"].export_dir) == tmp_path
    assert tmp_path.exists()
    assert not list(tmp_path.iterdir())


async def test_exported_packages_land_under_the_batch_root(tmp_path):
    bound = _default_exporters(tmp_path, list(BANK_ENGINES))
    exporters = {
        engine: _FakeExporter(engine, export_dir=exporter.export_dir)
        for engine, exporter in bound.items()
    }
    summary = await _ingest(
        _keywords(1), tmp_path,
        exporters=exporters,
    )
    folders = {Path(result.product_dir).parent.name for result in summary.exports}
    assert folders == {tmp_path.name}


# ----------------------------------------------------------------------
# ingest_keyword_bank — the product is the unit: exact first, fallback
# only to fill what the exact left open
# ----------------------------------------------------------------------


async def test_the_fallback_runs_only_when_the_exact_keyword_underfills(tmp_path):
    """The exact title gets first refusal; a satisfied product costs no more."""
    # Two products per keyword, target 2: the exact keyword fills both slots, so
    # the fallback is never searched.
    keywords = _product_keywords("gold title", "tight fallback")
    summary = await _ingest(keywords, tmp_path, target=2)

    cj = summary.per_engine["cjdropshipping"]
    assert cj["keywords_run"] == 1  # only the exact keyword
    assert cj["keywords_skipped"] == 1
    assert cj["candidates_exported"] == 2
    assert summary.total_exports == 2


async def test_the_fallback_fills_the_slots_the_exact_keyword_left_open(tmp_path):
    # Target 3 with 2 products available per leg: the exact keyword returns both,
    # leaving one slot, so the fallback runs for exactly that one.
    keywords = _product_keywords("gold title", "tight fallback")
    summary = await _ingest(keywords, tmp_path, target=3)

    cj = summary.per_engine["cjdropshipping"]
    assert cj["keywords_run"] == 2
    assert cj["keywords_skipped"] == 0
    assert cj["candidates_exported"] == 3


async def test_the_fallback_runs_when_the_exact_keyword_returns_nothing(tmp_path):
    # Target 2, but the product's leg returns nothing for the exact keyword: the
    # fallback then runs with the full target, which is the case the whole
    # two-strategy design exists for.
    keywords = _product_keywords("gold title", "tight fallback")
    products = {engine: _products_per_engine(1, engines=[engine])[engine]
                for engine in BANK_ENGINES}
    summary = await _ingest(keywords, tmp_path, target=2, products=products)
    cj = summary.per_engine["cjdropshipping"]
    assert cj["keywords_run"] == 2
    assert cj["candidates_exported"] == 2


async def test_a_skipped_fallback_is_logged_so_the_decision_is_visible(tmp_path, caplog):
    with caplog.at_level("INFO"):
        await _ingest(
            _product_keywords("gold title", "tight fallback"), tmp_path, target=2
        )
    assert any("already filled this product's" in r.message for r in caplog.records)
    # And the leg line names which strategy ran, so a log reader can tell the
    # exact title from the fallback.
    assert any("match=exact" in r.message for r in caplog.records)


async def test_each_engine_gets_its_own_pass_at_the_products_keywords(tmp_path, two_engines):
    """The two engines are independent: one filling its target does not stop the
    other from searching the same product."""
    cj_engine, second = two_engines
    keywords = _product_keywords("gold title", "tight fallback")
    summary = await _ingest(keywords, tmp_path, target=2)

    for engine in two_engines:
        counters = summary.per_engine[engine]
        assert counters["keywords_run"] == 1
        assert counters["keywords_skipped"] == 1
        assert counters["candidates_exported"] == 2
    assert summary.total_exports == 4


async def test_a_product_whose_exact_leg_fails_still_tries_its_fallback(tmp_path):
    """A blocked exact leg must not swallow the product's fallback.

    With one engine and every leg blocked the bank raises — the intervention
    case — so the proof that the fallback was still attempted is the leg count in
    the failure message: 2 legs for 1 product means the exact keyword AND its
    fallback were both searched.
    """
    keywords = _product_keywords("gold title", "tight fallback")
    with pytest.raises(BankIngestionFailedError) as excinfo:
        await _ingest(
            keywords,
            tmp_path,
            target=1,
            errors={"cjdropshipping": ExtractorBlockedException("MCP wall")},
        )
    message = str(excinfo.value)
    assert "1 product(s)" in message
    assert "cjdropshipping (2 leg(s))" in message


async def test_products_are_iterated_in_bank_order(tmp_path):
    keywords = _product_keywords("alpha exact", None) + _product_keywords("beta exact", None)
    keywords[1] = BankKeyword(
        keyword="beta exact", product="Second Product", pillar="brew_fuel",
        match="exact", rationale="its own title",
    )
    summary = await _ingest(keywords, tmp_path, target=1)
    assert summary.products_attempted == 2
