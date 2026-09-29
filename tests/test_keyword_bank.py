"""Unit tests for the Step-5 keyword bank and its dual-supplier ingestion.

Hermetic: the bank loader is exercised on tmp files, and
`ingest_keyword_bank` runs against fake extractors, a fake evaluator and fake
exporters. The real `run_pipeline` IS used (the module reuses it rather than
forking it), so these tests also prove the bank path inherits the existing
per-leg funnel counters, the image-gate accounting and the early-stop rule.
No network, no real engines.
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


def _bank_row(keyword: str, product: str = "Gold Product") -> Dict[str, Any]:
    return {
        "keyword": keyword,
        "product": product,
        "pillar": "curated_home",
        "role": "broad",
        "tightens": None,
        "rationale": "Head term",
    }


def _write_bank(tmp_path: Path, payload: Any, name: str = "bank.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _product(engine: str, keyword: str, index: int) -> RawSupplierProduct:
    return RawSupplierProduct(
        supplier_name="CJdropshipping" if engine == "cjdropshipping" else "AliExpress",
        supplier_retail_url=(
            f"https://cjdropshipping.com/product/{index}.html"
            if engine == "cjdropshipping"
            else f"https://www.aliexpress.com/item/{index}.html"
        ),
        product_title=f"{engine} {keyword} #{index}",
        product_description="A physical tool.",
        price_aud=5.0,
        shipping_cost_aud=9.0 if engine == "cjdropshipping" else 0.0,
        image_urls=[
            f"https://cdn.example.com/{engine}/{keyword}/{index}-{n}.jpg"
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


def _products_per_engine(count: int = 2) -> Dict[str, List[RawSupplierProduct]]:
    return {
        engine: [_product(engine, "kw", index) for index in range(count)]
        for engine in BANK_ENGINES
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
    products = products or _products_per_engine()
    engines = engines or list(BANK_ENGINES)
    errors = errors or {}
    exporters = exporters or {engine: _FakeExporter(engine) for engine in engines}
    factories = {
        engine: _factory(engine, products.get(engine, []), errors.get(engine), calls)
        for engine in engines
    }
    return await ingest_keyword_bank(
        keywords=keywords,
        target_per_keyword=target,
        evaluator=evaluator or _FakeEvaluator(),
        exporters=exporters,
        extractor_factories=factories,
        export_root=export_root or tmp_path,
    )


def _keywords(count: int = 2) -> List[BankKeyword]:
    return [
        BankKeyword(
            keyword=f"keyword {index}",
            product="Gold Product",
            pillar="curated_home",
            role="broad",
            tightens=None,
            rationale="Head term",
        )
        for index in range(count)
    ]


# ----------------------------------------------------------------------
# load_keyword_bank
# ----------------------------------------------------------------------


def test_load_keyword_bank_reads_typed_rows(tmp_path):
    path = _write_bank(tmp_path, {"keywords": [_bank_row("stainless garlic press")]})
    bank = load_keyword_bank(path)
    assert [row.keyword for row in bank] == ["stainless garlic press"]
    assert isinstance(bank[0], BankKeyword)
    assert bank[0].pillar == "curated_home"
    assert bank[0].role == "broad"
    assert bank[0].tightens is None


def test_load_keyword_bank_accepts_a_bare_list_payload(tmp_path):
    path = _write_bank(tmp_path, [_bank_row("pumice foot file")])
    assert [row.keyword for row in load_keyword_bank(path)] == ["pumice foot file"]


def test_load_keyword_bank_keeps_a_modifier_s_tightens(tmp_path):
    row = _bank_row("stainless garlic press with peeler")
    row["role"] = "modifier"
    row["tightens"] = "stainless garlic press"
    path = _write_bank(tmp_path, {"keywords": [row]})
    assert (
        load_keyword_bank(path)[0].tightens == "stainless garlic press"
    )


def test_load_keyword_bank_defaults_to_the_configured_path(tmp_path, monkeypatch):
    path = _write_bank(tmp_path, {"keywords": [_bank_row("garlic press")]})
    monkeypatch.setattr(settings, "KEYWORD_BANK_PATH", str(path))
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
# ingest_keyword_bank — dual-engine fan-out
# ----------------------------------------------------------------------


async def test_ingest_runs_both_engines_for_every_keyword(tmp_path):
    keywords = _keywords(3)
    calls: list = []
    await _ingest(keywords, tmp_path, calls=calls)
    assert sorted(calls) == sorted(
        (engine, keyword.keyword)
        for keyword in keywords
        for engine in BANK_ENGINES
    )


async def test_ingest_shares_one_evaluator_across_engines(tmp_path):
    log: list = []
    await _ingest(_keywords(1), tmp_path, evaluator=_FakeEvaluator(log=log))
    # The evaluator saw one product from EACH engine — the legs are separate
    # run_pipeline calls, not one chain that stops at the first engine.
    assert any(title.startswith("cjdropshipping") for title in log)
    assert any(title.startswith("aliexpress") for title in log)


async def test_ingest_folds_counters_across_per_leg_summaries(tmp_path):
    summary = await _ingest(_keywords(2), tmp_path, target=2)
    assert summary.keywords_total == 2
    assert summary.keywords_attempted == 2
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
    assert summary.total_exports == 8


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
    assert summary.total_exports == 2


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
    monkeypatch.setattr(settings, "BANK_TARGET_PER_KEYWORD", 1)
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


async def test_ingest_skips_only_the_failing_leg_and_counts_it(tmp_path):
    summary = await _ingest(
        _keywords(2),
        tmp_path,
        errors={"cjdropshipping": ExtractorNotConfiguredError("CJ_MCP_TOKEN")},
    )
    cj = summary.per_engine["cjdropshipping"]
    assert cj["keywords_run"] == 0
    assert cj["leg_failures"] == 2
    assert cj["candidates_scraped"] == 0
    # The other engine still ran every keyword and exported.
    ali = summary.per_engine["aliexpress"]
    assert ali["keywords_run"] == 2
    assert ali["leg_failures"] == 0
    assert ali["candidates_exported"] == 4
    assert summary.total_exports == 4


async def test_ingest_skips_a_blocked_leg(tmp_path):
    summary = await _ingest(
        _keywords(1),
        tmp_path,
        errors={"aliexpress": ExtractorBlockedException("anti-bot wall")},
    )
    assert summary.per_engine["aliexpress"]["leg_failures"] == 1
    assert summary.per_engine["cjdropshipping"]["candidates_exported"] == 2


async def test_ingest_raises_when_every_engine_fails_every_keyword(tmp_path):
    with pytest.raises(BankIngestionFailedError) as excinfo:
        await _ingest(
            _keywords(2),
            tmp_path,
            errors={
                "cjdropshipping": ExtractorNotConfiguredError("CJ_MCP_TOKEN"),
                "aliexpress": ExtractorBlockedException("anti-bot wall"),
            },
        )
    message = str(excinfo.value)
    assert "Every registered bank engine failed" in message
    assert "cjdropshipping" in message
    assert "aliexpress" in message


async def test_ingest_does_not_raise_when_one_engine_handles_every_keyword(tmp_path):
    summary = await _ingest(
        _keywords(2),
        tmp_path,
        errors={"cjdropshipping": ExtractorNotConfiguredError("CJ_MCP_TOKEN")},
    )
    assert summary.total_exports > 0


async def test_ingest_counts_keywords_that_returned_no_product(tmp_path):
    products = {engine: [] for engine in BANK_ENGINES}
    summary = await _ingest(_keywords(3), tmp_path, products=products)
    assert summary.keywords_empty == 3
    # Both legs RAN (an empty catalogue is not a failure) — nothing to export.
    for engine in BANK_ENGINES:
        assert summary.per_engine[engine]["keywords_run"] == 3
        assert summary.per_engine[engine]["leg_failures"] == 0
    assert summary.total_exports == 0


async def test_ingest_with_one_engine_still_raises_when_it_fails_everything(tmp_path):
    with pytest.raises(BankIngestionFailedError):
        await _ingest(
            _keywords(1),
            tmp_path,
            engines=["aliexpress"],
            errors={"aliexpress": ExtractorBlockedException("anti-bot wall")},
        )


# ----------------------------------------------------------------------
# Export roots
# ----------------------------------------------------------------------


async def test_export_roots_are_the_two_supplier_subfolders(tmp_path):
    summary = await _ingest(_keywords(1), tmp_path)
    assert summary.export_roots == {
        "cjdropshipping": str(tmp_path / "cjdropshipping"),
        "aliexpress": str(tmp_path / "aliexpress"),
    }


def test_default_exporters_bind_one_exporter_per_supplier_subfolder(tmp_path):
    exporters = _default_exporters(tmp_path, list(BANK_ENGINES))
    assert set(exporters) == set(BANK_ENGINES)
    assert Path(exporters["cjdropshipping"].export_dir) == tmp_path / "cjdropshipping"
    assert Path(exporters["aliexpress"].export_dir) == tmp_path / "aliexpress"
    # Nothing is created until a package is actually written.
    assert not (tmp_path / "cjdropshipping").exists()


async def test_exported_packages_land_under_the_supplier_subfolder(tmp_path):
    summary = await _ingest(_keywords(1), tmp_path)
    folders = {Path(result.product_dir).parent.name for result in summary.exports}
    assert folders == set(BANK_ENGINES)
