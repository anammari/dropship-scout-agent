"""End-to-end CLI orchestrator (Phase 6, rewritten per plan F.5).

Supplier-First pipeline: supplier extractors (CJdropshipping MCP server,
AliExpress Dropshipping Center) fetch verified
`RawSupplierProduct`s -> LLM viability evaluation (the LLM authors only
marketing/viability fields) -> deterministic CDN image download with the
3-image gate -> workspace export -> printed summary with per-run drop
counters.

CLI:
    python -m src.main --keyword "kitchen gadgets" --target-count 1 \
        [--country AU] [--extractor cjdropshipping]

Exit codes:
    0  pipeline completed; a PARTIAL result with fewer exports than the
       target still exits 0
    1  requires human intervention: every extractor unavailable/blocked,
       LLM config missing, or the funnel was exhausted with zero exports
    2  unexpected error (traceback logged)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.config import settings
from src.exporter import CandidateExporter, ExportResult
from src.evaluators.llm_filter import LLMConfigError, LLMEvaluationFilter
from src.extractors.aliexpress_ds import (
    AliExpressDsCenterExtractor,
    DsCenterSessionExpiredError,
)
from src.extractors.base import (
    BaseSupplierExtractor,
    ExtractorBlockedException,
    ExtractorNotConfiguredError,
    ExtractorTimeoutException,
)
from src.extractors.cj_mcp_extractor import CjMcpExtractor
from src.models import ProductCandidateEvaluation, RawSupplierProduct

logger = logging.getLogger(__name__)

# Extractor registry keyed by the same supplier keys SUPPLIER_PRIORITY_ORDER
# uses; auto mode tries them in that configured order.
_EXTRACTOR_REGISTRY = {
    "cjdropshipping": CjMcpExtractor,
    "aliexpress": AliExpressDsCenterExtractor,
}


def _build_extractor_chain() -> List[BaseSupplierExtractor]:
    """Instantiate the extractors named in SUPPLIER_PRIORITY_ORDER."""
    chain: List[BaseSupplierExtractor] = []
    for key in settings.SUPPLIER_PRIORITY_ORDER:
        extractor_cls = _EXTRACTOR_REGISTRY.get(key)
        if extractor_cls is None:
            logger.warning(
                "Unknown supplier key %r in SUPPLIER_PRIORITY_ORDER — skipped "
                "(known: %s)",
                key, sorted(_EXTRACTOR_REGISTRY),
            )
            continue
        chain.append(extractor_cls())
    return chain


@dataclass
class PipelineSummary:
    """Everything the CLI (and tests) need to know about one pipeline run."""

    keyword: str
    country: str
    target_count: int
    extractor_engine: str = ""
    # Funnel counters — printed on every run so data-quality regressions
    # are visible immediately, not discovered by manual audit.
    candidates_scraped: int = 0
    evaluated: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped_llm_validation_failed: int = 0
    # Fewer than 3 valid images downloaded from the verified CDN gallery
    # (the single image source has no fallback tier).
    dropped_no_valid_images: int = 0
    # ACCEPTs whose product page is already in an exported package — a
    # re-run of the same keyword re-offers the same products.
    skipped_duplicate: int = 0
    candidates_exported: int = 0
    exports: List[ExportResult] = field(default_factory=list)
    export_dir: str = ""


def render_intervention_block(
    step: str, reason: str, instructions: List[str]
) -> str:
    """The CLAUDE.md §7 human-intervention block format."""
    lines = [
        "",
        "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]",
        "------------------------------------------------------------",
        f"Step: {step}",
        f"Reason: {reason}",
        "",
        "Instructions for You:",
    ]
    for index, instruction in enumerate(instructions, start=1):
        lines.append(f"{index}. {instruction}")
    lines.append("------------------------------------------------------------")
    lines.append("")
    return "\n".join(lines)


async def run_pipeline(
    keyword: str,
    target_count: int = 3,
    country: Optional[str] = None,
    extractors: Optional[List[BaseSupplierExtractor]] = None,
    evaluator: Optional[Any] = None,
    exporter: Optional[Any] = None,
) -> PipelineSummary:
    """Run extract -> evaluate -> download images -> export. Returns a summary.

    Components are injectable for tests; defaults are the real engines.
    `ExtractorNotConfiguredError`/`ExtractorBlockedException`/
    `ExtractorTimeoutException` (extraction unavailable or blocked) and
    `LLMConfigError` (missing endpoint config) surface to the CLI, which
    renders the §7 intervention block.

    The LLM filter is constructed BEFORE extraction so a misconfigured
    `.env` fails fast without burning supplier API quota.
    """
    country = country or settings.TARGET_COUNTRY
    summary = PipelineSummary(
        keyword=keyword,
        country=country,
        target_count=target_count,
        export_dir=str(settings.EXPORT_DIR),
    )

    evaluator = evaluator or LLMEvaluationFilter()  # raises LLMConfigError early
    exporter = exporter or CandidateExporter()
    extractor_chain = extractors if extractors is not None else _build_extractor_chain()

    # --- Stage 1: extract verified supplier products -----------------------
    products: List[RawSupplierProduct] = []
    chain_notes: List[str] = []
    for extractor in extractor_chain:
        engine = getattr(extractor, "engine_name", extractor.__class__.__name__)
        try:
            found = await extractor.fetch_products([keyword], country)
        except ExtractorNotConfiguredError as exc:
            chain_notes.append(f"{engine}: {exc}")
            logger.warning(
                "Extractor %s not configured — trying the next in the chain",
                engine,
            )
            continue
        except (ExtractorBlockedException, ExtractorTimeoutException) as exc:
            chain_notes.append(f"{engine}: {exc}")
            logger.warning(
                "Extractor %s failed (%s) — trying the next in the chain",
                engine, exc,
            )
            continue
        summary.extractor_engine = engine
        summary.candidates_scraped = len(found)
        products = found
        logger.info(
            "Extraction done via %s: %d verified supplier product(s) for "
            "keyword=%r country=%r",
            engine, len(found), keyword, country,
        )
        break
    if not products:
        if chain_notes:
            # Every extractor in the chain was unavailable or blocked —
            # surface per §7 with each engine's reason.
            raise PipelineExtractionFailedError(
                "No supplier extractor could fetch products for "
                f"keyword={keyword!r}:\n" + "\n".join(chain_notes)
            )
        logger.warning(
            "No verified supplier products extracted — funnel is empty"
        )
        return summary

    # --- Stages 2-4: LLM evaluation -> image gate -> export ---------------
    # Products are evaluated in scrape order until `target_count` ACCEPTs
    # are in hand (early stop). An ACCEPT only counts as "in hand" once its
    # gallery clears the 3-image gate, so when the image stage drops
    # candidates the loop keeps pulling from the remaining scrape order
    # instead of ending the run with viable products never evaluated.
    evaluated_pairs: List[tuple] = []
    next_index = 0
    while True:
        while next_index < len(products) and len(evaluated_pairs) < target_count:
            product = products[next_index]
            next_index += 1
            try:
                evaluation = await evaluator.evaluate(product)
            except Exception:
                summary.dropped_llm_validation_failed += 1
                logger.warning(
                    "LLM evaluation failed for %r (%s) (schema validation "
                    "exhausted retries or call error); continuing",
                    product.product_title, product.supplier_retail_url,
                    exc_info=True,
                )
                continue
            summary.evaluated += 1
            if evaluation.verdict == "ACCEPT":
                evaluated_pairs.append((evaluation, product))
                summary.accepted += 1
            else:
                summary.rejected += 1

        if evaluated_pairs:
            summary.exports.extend(
                await exporter.export_candidates(evaluated_pairs)
            )
            evaluated_pairs = []

        if len(summary.exports) >= target_count or next_index >= len(products):
            break

    summary.dropped_no_valid_images += getattr(
        exporter, "dropped_no_valid_images", 0
    )
    summary.skipped_duplicate += getattr(exporter, "skipped_duplicate", 0)
    summary.candidates_exported = len(summary.exports)
    return summary


class PipelineExtractionFailedError(Exception):
    """Every extractor in the chain was unconfigured, blocked, or empty."""


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description=(
            "Dropship Scout Agent: ingest live supplier listings "
            "(CJdropshipping / AliExpress), filter winners through "
            "the LLM viability gate, and export them to the Shopify "
            "workspace."
        ),
    )
    parser.add_argument(
        "--keyword",
        default="desk organizer",
        help="seed niche keyword to scout (default: %(default)s)",
    )
    parser.add_argument(
        "--target-count",
        type=_positive_int,
        default=3,
        help="number of ACCEPTed winning products to export (default: %(default)s)",
    )
    parser.add_argument(
        "--country",
        default=settings.TARGET_COUNTRY,
        help="target country for supplier discovery (default: %(default)s)",
    )
    parser.add_argument(
        "--extractor",
        choices=["auto"] + sorted(_EXTRACTOR_REGISTRY),
        default="auto",
        help="force one supplier extractor, or try them in configured order "
             "(default: %(default)s)",
    )
    return parser


def _print_summary(summary: PipelineSummary) -> None:
    print(
        f"\n[PIPELINE SUMMARY] keyword={summary.keyword!r} country={summary.country} "
        f"target={summary.target_count} engine={summary.extractor_engine or 'n/a'}"
    )
    print(
        f"  candidates_scraped={summary.candidates_scraped}  "
        f"candidates_exported={summary.candidates_exported}"
    )
    print(
        f"  dropped: llm_validation_failed={summary.dropped_llm_validation_failed}  "
        f"no_valid_images={summary.dropped_no_valid_images}  "
        f"rejected_by_viability_gate={summary.rejected}  "
        f"skipped_duplicate={summary.skipped_duplicate}"
    )
    if summary.exports:
        print(f"  exported {len(summary.exports)} product(s) to {summary.export_dir}:")
        for result in summary.exports:
            evaluation = result.evaluation
            pricing = ""
            if evaluation is not None:
                pricing = (
                    f"price_aud={evaluation.suggested_retail_aud:.2f} "
                    f"cogs_aud={evaluation.estimated_cogs_aud:.2f} "
                    f"margin_aud={evaluation.estimated_margin_aud:.2f} "
                    f"markup={evaluation.markup_multiplier:.1f}x"
                )
            print(
                f"    - {result.product_slug}: {result.product_title!r} "
                f"[{len(result.image_files)} image(s), "
                f"supplier={evaluation.supplier_name if evaluation else 'n/a'}] "
                f"{pricing}"
            )
    else:
        print(f"  no products exported to {summary.export_dir}")


def _funnel_exhausted_reason(summary: PipelineSummary) -> str:
    return (
        f"0 of {summary.target_count} target package(s) exported — the "
        f"funnel consumed {summary.candidates_scraped} verified supplier "
        f"product(s): {summary.dropped_llm_validation_failed} dropped for "
        f"failed LLM validation, {summary.dropped_no_valid_images} dropped "
        f"with fewer than 3 valid gallery images, {summary.rejected} "
        f"rejected by the viability gate, {summary.skipped_duplicate} "
        "skipped as already exported. This is a data-quality signal, "
        "not a crash — inspect which counter dominates and widen that "
        "funnel stage."
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    extractors: List[BaseSupplierExtractor] = []
    if args.extractor != "auto":
        extractor_cls = _EXTRACTOR_REGISTRY[args.extractor]
        extractors = [extractor_cls()]

    try:
        summary = asyncio.run(
            run_pipeline(
                keyword=args.keyword,
                target_count=args.target_count,
                country=args.country,
                extractors=extractors or None,
            )
        )
    except PipelineExtractionFailedError as exc:
        print(
            render_intervention_block(
                step="Supplier Extraction Stage",
                reason=str(exc),
                instructions=[
                    "Check that at least one supplier extractor is "
                    "configured: CJ_MCP_TOKEN (CJdropshipping MCP server) in "
                    ".env — the AliExpress Dropshipping Center engine needs "
                    "no credential of its own",
                    "Verify each configured credential manually against its "
                    "API (auth endpoint ping) before re-running",
                    "Re-run the pipeline after any manual fix: python -m "
                    f"src.main --keyword {args.keyword!r} --target-count "
                    f"{args.target_count}",
                ],
            )
        )
        return 1
    except DsCenterSessionExpiredError as exc:
        print(
            render_intervention_block(
                step="AliExpress Dropshipping Center Session",
                reason=str(exc),
                instructions=[
                    "Refresh the saved AliExpress login: python "
                    "scripts/generate_ali_session.py",
                    "Or run anonymously by removing the saved session file "
                    "(ALI_DS_STATE_PATH) — the DS Center answers these "
                    "calls without a login",
                    "Re-run the pipeline: python -m src.main --keyword "
                    f"{args.keyword!r} --target-count {args.target_count}",
                ],
            )
        )
        return 1
    except LLMConfigError as exc:
        print(
            render_intervention_block(
                step="LLM Evaluation Filter Configuration",
                reason=str(exc),
                instructions=[
                    "Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env "
                    "(e.g. an Ollama / OpenAI-compatible endpoint)",
                    "Verify the endpoint responds and the model name is valid "
                    "with a manual chat-completions call",
                    "Re-run the pipeline: python -m src.main --keyword "
                    f"{args.keyword!r} --target-count {args.target_count}",
                ],
            )
        )
        return 1
    except Exception:
        logger.exception("Unexpected error during pipeline run")
        return 2

    _print_summary(summary)
    if not summary.exports:
        # Funnel exhausted: every extracted product was dropped by one of
        # the gates — surface per CLAUDE.md §7 so the dominant drop reason
        # is actionable rather than silently swallowed.
        print(
            render_intervention_block(
                step="Pipeline Funnel Exhausted",
                reason=_funnel_exhausted_reason(summary),
                instructions=[
                    "Inspect the drop counters above to find the dominant "
                    "gate (LLM validation, image sourcing, or the viability "
                    "gate)",
                    "Try a broader seed keyword with more physical-product "
                    f"listings than {args.keyword!r}",
                    "Re-run the pipeline: python -m src.main --keyword "
                    f"{args.keyword!r} --target-count {args.target_count}",
                ],
            )
        )
        return 1
    if summary.candidates_exported < summary.target_count:
        print(
            f"\n[PARTIAL] Only {summary.candidates_exported} of "
            f"{summary.target_count} target packages landed — "
            f"{summary.dropped_no_valid_images} candidate(s) had fewer than "
            "3 valid gallery images. Consider another keyword or a larger "
            "batch."
        )
    print("\n[PIPELINE COMPLETE] Winning product packages are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())