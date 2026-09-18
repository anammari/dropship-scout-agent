"""Offline unit tests for `src.main` (plan F.5 orchestration).

Extractors, the LLM filter, and the exporter are fakes — no network, no
LLM. Verifies the extractor fallback chain, funnel counting, the
extract-fail and funnel-exhausted intervention paths, and the CLI exit
codes.
"""

import pytest

from src.main import (
    PipelineExtractionFailedError,
    PipelineSummary,
    _build_extractor_chain,
    _build_arg_parser,
    _funnel_exhausted_reason,
    _positive_int,
    main,
    render_intervention_block,
    run_pipeline,
)
from src.models import (
    ProductCandidateEvaluation,
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)
from src.extractors.base import (
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
)

_ALI_URL = "https://www.aliexpress.com/item/1005006112233445.html"
_IMAGES = [
    "https://ae01.alicdn.com/kf/S1.jpg",
    "https://ae01.alicdn.com/kf/S2.jpg",
    "https://ae01.alicdn.com/kf/S3.jpg",
]


def _raw_product(title="Ergonomic Desk Cable Organiser") -> RawSupplierProduct:
    return RawSupplierProduct(
        supplier_name="AliExpress",
        supplier_retail_url=_ALI_URL,
        product_title=title,
        product_description="A spine.",
        price_aud=12.50,
        shipping_cost_aud=4.30,
        image_urls=list(_IMAGES),
    )


def _final(title="Ergonomic Desk Cable Organiser") -> ProductCandidateEvaluation:
    provisional = ProvisionalProductEvaluation(
        verdict="ACCEPT",
        niche_category="Home Office",
        problem_solved="Clutter",
        suggested_retail_aud=49.99,
        marketing_ad_copy="Buy it.",
        saturation_risk="LOW",
        target_tags=["dropship"],
        shipping_notice_au="7-12 business days",
        key_features=["A", "B", "C"],
    )
    raw = _raw_product(title)
    return ProductCandidateEvaluation.from_raw(provisional, raw)


class FakeExtractor:
    """Scriptable extractor stand-in.

    Script entries: a list of RawSupplierProducts (success), an Exception
    (raised), or None (returns an empty list).
    """

    def __init__(self, engine="fake", script=None):
        self.engine_name = engine
        self.script = list(script or [])
        self.calls = []

    async def fetch_products(self, keywords, country="AU"):
        self.calls.append((list(keywords), country))
        if not self.script:
            return []
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeEvaluator:
    def __init__(self, script=None):
        # Each product evaluated pops the next outcome: an evaluation or
        # an Exception (counted as dropped_llm_validation_failed).
        self.script = list(script or [])
        self.calls = []

    async def evaluate(self, raw):
        self.calls.append(raw)
        entry = self.script.pop(0) if self.script else _final(raw.product_title)
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeExporter:
    def __init__(self, results=None, drop_all=False):
        self.results = list(results or [])
        self.drop_all = drop_all
        self.exported = None

    async def export_candidates(self, candidates):
        self.exported = list(candidates)
        if self.drop_all:
            return []
        return self.results or [
            f"product-{i + 1:02d}" for i in range(len(candidates))
        ]


# ----------------------------------------------------------------------
# Extractor chain
# ----------------------------------------------------------------------


def test_extractor_chain_builds_from_configured_priority_order(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(
        settings, "SUPPLIER_PRIORITY_ORDER",
        ["cjdropshipping", "aliexpress", "etsy"],
    )
    chain = _build_extractor_chain()
    assert [e.engine_name for e in chain] == [
        "cjdropshipping", "aliexpress_apify", "etsy_api",
    ]


def test_extractor_chain_skips_unknown_keys(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SUPPLIER_PRIORITY_ORDER", ["taobao", "cjdropshipping"])
    chain = _build_extractor_chain()
    assert [e.engine_name for e in chain] == ["cjdropshipping"]


# ----------------------------------------------------------------------
# run_pipeline stages
# ----------------------------------------------------------------------


async def test_run_pipeline_happy_path_exports_accepted_pairs(tmp_path):
    products = [_raw_product("Product A"), _raw_product("Product B")]
    extractor = FakeExtractor(engine="fake", script=[products])
    evaluator = FakeEvaluator(script=[_final("Product A"), _final("Product B")])
    exporter = FakeExporter()

    summary = await run_pipeline(
        keyword="desk organizer", target_count=2,
        extractors=[extractor], evaluator=evaluator, exporter=exporter,
    )

    assert summary.candidates_scraped == 2
    assert summary.evaluated == 2
    assert summary.accepted == 2
    assert summary.rejected == 0
    assert summary.candidates_exported == 2
    assert [c[1].product_title for c in exporter.exported] == [
        "Product A", "Product B",
    ]
    assert summary.extractor_engine == "fake"


async def test_run_pipeline_tries_the_next_extractor_when_one_fails():
    blocked = FakeExtractor(
        engine="blocked",
        script=[ExtractorBlockedException("captcha wall")],
    )
    fallback = FakeExtractor(engine="fallback", script=[[_raw_product()]])
    evaluator = FakeEvaluator(script=[_final()])
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[blocked, fallback], evaluator=evaluator, exporter=FakeExporter(),
    )
    # The blocked engine raised BEFORE any products were fetched, so the
    # chain moved on to the fallback.
    assert summary.extractor_engine == "fallback"
    assert summary.candidates_scraped == 1


async def test_unconfigured_extractors_fall_through_to_the_next():
    unconfigured = FakeExtractor(
        engine="unconfigured",
        script=[ExtractorNotConfiguredError("CJ_MCP_TOKEN")],
    )
    working = FakeExtractor(engine="working", script=[[_raw_product()]])
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[unconfigured, working],
        evaluator=FakeEvaluator(script=[_final()]), exporter=FakeExporter(),
    )
    assert summary.extractor_engine == "working"


async def test_all_extractors_failing_raises_extraction_failed():
    extractors = [
        FakeExtractor(engine="a", script=[ExtractorNotConfiguredError("no key")]),
        FakeExtractor(engine="b", script=[ExtractorBlockedException("blocked")]),
    ]
    with pytest.raises(PipelineExtractionFailedError, match="no key"):
        await run_pipeline(
            keyword="k", target_count=1,
            extractors=extractors, evaluator=FakeEvaluator(), exporter=FakeExporter(),
        )


async def test_extraction_stops_at_the_target_count():
    products = [_raw_product(f"P{i}") for i in range(5)]
    extractor = FakeExtractor(engine="fake", script=[products])
    evaluator = FakeEvaluator(script=[_final(f"P{i}") for i in range(5)])
    exporter = FakeExporter()
    summary = await run_pipeline(
        keyword="k", target_count=2,
        extractors=[extractor], evaluator=evaluator, exporter=exporter,
    )
    # Evaluation stopped early once two ACCEPTs were in hand.
    assert summary.accepted == 2
    assert len(evaluator.calls) == 2
    assert len(exporter.exported) == 2


async def test_llm_failure_counts_as_dropped_and_continues():
    products = [_raw_product("Broken A"), _raw_product("Good B")]
    extractor = FakeExtractor(engine="fake", script=[products])
    evaluator = FakeEvaluator(script=[
        RuntimeError("schema invalid"),
        _final("Good B"),
    ])
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[extractor], evaluator=evaluator, exporter=FakeExporter(),
    )
    assert summary.dropped_llm_validation_failed == 1
    assert summary.accepted == 1
    assert summary.candidates_exported == 1


async def test_image_gate_drops_surface_in_the_summary(tmp_path):
    products = [_raw_product("Thin gallery")]
    extractor = FakeExtractor(engine="fake", script=[products])

    class CountingExporter(FakeExporter):
        def __init__(self):
            super().__init__(drop_all=True)
            self.dropped_no_valid_images = 1

    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[extractor], evaluator=FakeEvaluator(script=[_final()]),
        exporter=CountingExporter(),
    )
    assert summary.dropped_no_valid_images == 1
    assert summary.candidates_exported == 0


async def test_evaluation_continues_past_an_accept_that_fails_the_image_gate():
    """An ACCEPT is only 'in hand' once its gallery clears the 3-image gate."""
    products = [_raw_product(f"P{i}") for i in range(3)]
    extractor = FakeExtractor(engine="fake", script=[products])
    evaluator = FakeEvaluator(script=[_final(f"P{i}") for i in range(3)])

    class FirstFailsExporter(FakeExporter):
        """The first batch fails the image gate; the second succeeds."""

        def __init__(self):
            super().__init__()
            self.batches = []
            self.dropped_no_valid_images = 1

        async def export_candidates(self, candidates):
            self.batches.append(list(candidates))
            if len(self.batches) == 1:
                return []
            return [f"product-{i + 1:02d}" for i in range(len(candidates))]

    exporter = FirstFailsExporter()
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[extractor], evaluator=evaluator, exporter=exporter,
    )

    # The first ACCEPT was dropped for images, so the next product was
    # evaluated and exported rather than the run ending empty-handed.
    assert summary.accepted == 2
    assert summary.candidates_exported == 1
    assert summary.dropped_no_valid_images == 1
    assert len(exporter.batches) == 2
    assert [pair[1].product_title for pair in exporter.batches[1]] == ["P1"]


async def test_evaluation_stops_once_the_target_is_actually_exported():
    products = [_raw_product(f"P{i}") for i in range(5)]
    extractor = FakeExtractor(engine="fake", script=[products])
    evaluator = FakeEvaluator(script=[_final(f"P{i}") for i in range(5)])
    exporter = FakeExporter()
    summary = await run_pipeline(
        keyword="k", target_count=2,
        extractors=[extractor], evaluator=evaluator, exporter=exporter,
    )
    # Both ACCEPTs exported on the first pass — no further LLM calls.
    assert len(evaluator.calls) == 2
    assert summary.candidates_exported == 2


async def test_skipped_duplicate_surfaces_in_the_summary():
    """A re-run over already-exported products must not look like a failure."""
    products = [_raw_product("Already exported")]
    extractor = FakeExtractor(engine="fake", script=[products])

    class DuplicateSkippingExporter(FakeExporter):
        def __init__(self):
            super().__init__()
            self.skipped_duplicate = 1

        async def export_candidates(self, candidates):
            self.exported = list(candidates)
            return []

    exporter = DuplicateSkippingExporter()
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[extractor], evaluator=FakeEvaluator(script=[_final()]),
        exporter=exporter,
    )

    assert summary.skipped_duplicate == 1
    assert summary.candidates_exported == 0
    # Every candidate was consumed by the duplicate skip, so evaluation is
    # exhausted rather than looping forever hunting for a fresh export.
    assert summary.accepted == 1


async def test_empty_extraction_returns_a_zero_summary():
    extractor = FakeExtractor(engine="empty", script=[[]])
    summary = await run_pipeline(
        keyword="k", target_count=1,
        extractors=[extractor], evaluator=FakeEvaluator(), exporter=FakeExporter(),
    )
    assert summary.candidates_scraped == 0
    assert summary.evaluated == 0
    assert summary.candidates_exported == 0


# ----------------------------------------------------------------------
# Intervention block + CLI
# ----------------------------------------------------------------------


def test_render_intervention_block_matches_the_documented_format():
    block = render_intervention_block(
        step="Test Step", reason="because", instructions=["do A", "do B"]
    )
    assert "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]" in block
    assert "Step: Test Step" in block
    assert "Reason: because" in block
    assert "1. do A" in block
    assert "2. do B" in block


def test_funnel_exhausted_reason_mentions_each_drop_counter():
    summary = PipelineSummary(keyword="k", country="AU", target_count=3)
    summary.candidates_scraped = 10
    summary.dropped_llm_validation_failed = 2
    summary.dropped_no_valid_images = 4
    summary.rejected = 4
    summary.skipped_duplicate = 1
    reason = _funnel_exhausted_reason(summary)
    assert "10 verified supplier product(s)" in reason
    assert "2 dropped" in reason
    assert "4 dropped" in reason
    assert "4 rejected" in reason
    assert "1 skipped as already exported" in reason


def test_positive_int_rejects_non_positive_values():
    assert _positive_int("3") == 3
    with pytest.raises(Exception):
        _positive_int("0")
    with pytest.raises(Exception):
        _positive_int("abc")


def test_arg_parser_defaults_are_preserved():
    args = _build_arg_parser().parse_args([])
    assert args.keyword == "desk organizer"
    assert args.target_count == 3
    assert args.extractor == "auto"


def test_arg_parser_accepts_forced_extractor():
    args = _build_arg_parser().parse_args(["--extractor", "cjdropshipping"])
    assert args.extractor == "cjdropshipping"


def _patch_run(monkeypatch, summary):
    """Point src.main.run_pipeline at a canned summary."""
    import src.main as m

    async def fake_run(**kwargs):
        return summary

    monkeypatch.setattr(m, "run_pipeline", fake_run)
    return m


def test_cli_funnel_exhausted_exits_1(monkeypatch, capsys):
    summary = PipelineSummary(keyword="k", country="AU", target_count=3)
    summary.candidates_scraped = 10
    summary.rejected = 10

    m = _patch_run(monkeypatch, summary)
    code = m.main(["--keyword", "k", "--target-count", "3"])
    assert code == 1
    out = capsys.readouterr().out
    assert "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]" in out
    assert "Pipeline Funnel Exhausted" in out


def test_cli_successful_run_exits_0(monkeypatch, capsys):
    from src.exporter import ExportResult

    summary = PipelineSummary(keyword="k", country="AU", target_count=1)
    summary.candidates_scraped = 2
    summary.accepted = 1
    summary.candidates_exported = 1
    result = ExportResult(
        product_dir="/tmp/product-01",
        product_title="P",
        metadata_path="/tmp/product-01/metadata.json",
        image_files=["image-1.jpg", "image-2.jpg", "image-3.jpg"],
    )
    summary.exports.append(result)

    m = _patch_run(monkeypatch, summary)
    code = m.main(["--keyword", "k", "--target-count", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[PIPELINE COMPLETE]" in out


def test_cli_partial_run_exits_0_with_a_warning(monkeypatch, capsys):
    from src.exporter import ExportResult

    summary = PipelineSummary(keyword="k", country="AU", target_count=3)
    summary.candidates_exported = 1
    summary.dropped_no_valid_images = 2
    result = ExportResult(
        product_dir="/tmp/product-01",
        product_title="P",
        metadata_path="/tmp/product-01/metadata.json",
        image_files=["image-1.jpg", "image-2.jpg", "image-3.jpg"],
    )
    summary.exports.append(result)

    m = _patch_run(monkeypatch, summary)
    code = m.main(["--keyword", "k", "--target-count", "3"])
    assert code == 0
    assert "[PARTIAL]" in capsys.readouterr().out