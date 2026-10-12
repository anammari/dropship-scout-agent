#!/usr/bin/env python
"""Step 5 runner: ingest the Step-4 keyword bank through the CJdropshipping
pipeline and export the optimal products to the Morning Anthem gold-kernel
tree.

Every bank PRODUCT runs through CJdropshipping (MCP) — The Morning Anthem's sole
supplier engine — under its own gates, and what survives the viability gate, the
margin floor and the 3-image gallery gate is written to:

    <MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/…

That tree is a production deliverable, not a repo artefact, and stays
untracked.

**A product's keywords run in order: its exact title first, then its fallback
only to fill whatever target slots the exact keyword left open.** A fallback is
therefore never paid for when the exact title already worked.

Pacing is the point of the pilot flags: one CJ keyword costs up to
CJ_MAX_PRODUCTS product-detail calls plus a freight quote each, so a full bank is
a long run. Pilot with `--limit 4 --target-per-product 1`.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
        [--keywords outputs/json/morning-anthem-step-4-keyword-bank.json] \
        [--target-per-product 2] [--limit 4] [--export-root <dir>]

Exit codes: 0 complete (a `[PARTIAL]` with fewer exports than the target
still exits 0), 1 requires human intervention, 2 unexpected error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow `python scripts/ingest_keyword_bank.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.evaluators.llm_filter import LLMConfigError  # noqa: E402
from src.main import (  # noqa: E402
    PipelineExtractionFailedError,
    render_intervention_block,
)
from src.pipeline.keyword_bank import (  # noqa: E402
    BANK_ENGINES,
    BankIngestionFailedError,
    BankIngestionSummary,
    KeywordBankError,
    ingest_keyword_bank,
    load_keyword_bank,
)

REPO = Path(__file__).resolve().parents[1]


def _build_extractor_factories() -> Dict[str, Any]:
    """The real extractor factories for the bank's registered engines."""
    from src.main import _EXTRACTOR_REGISTRY

    return {key: _EXTRACTOR_REGISTRY[key] for key in BANK_ENGINES}


def _print_pilot_banner(
    count: int, target: int, engines: List[str], export_root: str
) -> None:
    print(
        f"\n[STEP 5] {count} bank keyword(s) × {len(engines)} engine(s) × "
        f"target {target} package(s) per (product, engine) leg"
    )
    print(
        "  order: each product's exact title first; its fallback only for the "
        "slots the exact keyword left open"
    )
    print(f"  engines: {', '.join(engines)}")
    print(f"  export root: {export_root}")
    print(
        "  pacing: CJ runs up to "
        f"{settings.CJ_MAX_PRODUCTS_PER_KEYWORD} detail call(s) + freight "
        "quote(s) per keyword. That is the pacing, not a hang."
    )


def _print_summary(summary: BankIngestionSummary, engines: List[str]) -> None:
    print(
        f"\n[BANK INGESTION SUMMARY] keywords={summary.keywords_total} "
        f"products_attempted={summary.products_attempted} "
        f"products_empty={summary.products_empty} "
        f"exported={summary.total_exports}"
    )
    total: Dict[str, int] = {}
    for engine in engines:
        counters = summary.per_engine[engine]
        for key, value in counters.items():
            total[key] = total.get(key, 0) + value
        print(
            f"  {engine}: run={counters['keywords_run']} "
            f"failed_legs={counters['leg_failures']} "
            f"scraped={counters['candidates_scraped']} "
            f"evaluated={counters['evaluated']} "
            f"accepted={counters['accepted']} "
            f"rejected={counters['rejected']} exported="
            f"{counters['candidates_exported']} "
            f"(llm_failed={counters['dropped_llm_validation_failed']} "
            f"no_images={counters['dropped_no_valid_images']} "
            f"duplicate={counters['skipped_duplicate']} "
            f"fallbacks_skipped={counters['keywords_skipped']})")
        print(f"    -> {summary.export_roots[engine]}")
    print(
        f"  TOTAL: scraped={total.get('candidates_scraped', 0)} "
        f"evaluated={total.get('evaluated', 0)} "
        f"accepted={total.get('accepted', 0)} "
        f"rejected={total.get('rejected', 0)} "
        f"exported={total.get('candidates_exported', 0)}"
    )
    if summary.exports:
        print("  exported packages:")
        for result in summary.exports:
            print(f"    - {result.product_dir}: {result.product_title!r}")


def _exhausted_reason(summary: BankIngestionSummary) -> str:
    legs = ", ".join(
        f"{engine}: {counter['keywords_run']} leg(s) ran, "
        f"{counter['leg_failures']} failed"
        for engine, counter in summary.per_engine.items()
    )
    return (
        f"0 packages exported from {summary.keywords_attempted} bank "
        f"keyword(s) across {len(summary.per_engine)} engine(s) "
        f"({legs}; {summary.products_empty} product(s) returned no supplier "
        "product at all). Every gate held and every leg was empty or "
        "rejected — this is a data-quality signal, not a crash."
    )


async def _run(args: argparse.Namespace) -> int:
    try:
        keywords = load_keyword_bank(args.keywords)
    except KeywordBankError as exc:
        print(
            render_intervention_block(
                step="Step 5 Keyword Bank",
                reason=str(exc),
                instructions=[
                    "Generate the bank from the Step-3 gold products: "
                    "source .venv/bin/activate && python "
                    "scripts/generate_gold_keywords.py",
                    "Confirm the file exists and holds 50-70 keywords, then "
                    "re-run: python scripts/ingest_keyword_bank.py",
                ],
            )
        )
        return 1

    if args.limit is not None:
        keywords = keywords[: args.limit]
    factories = _build_extractor_factories()
    engines = list(factories)
    export_root = Path(args.export_root)
    _print_pilot_banner(len(keywords), args.target_per_keyword, engines, str(export_root))

    try:
        summary = await ingest_keyword_bank(
            keywords=keywords,
            target_per_keyword=args.target_per_keyword,
            extractor_factories=factories,
            export_root=export_root,
        )
    except BankIngestionFailedError as exc:
        print(
            render_intervention_block(
                step="Step 5 Supplier Ingestion",
                reason=str(exc),
                instructions=[
                    "Check that the CJdropshipping engine is usable: "
                    "CJ_MCP_TOKEN in .env, and that the network allows the "
                    "CJ MCP endpoint",
                    "Pilot one keyword to isolate it: python "
                    "scripts/ingest_keyword_bank.py --limit 1",
                    "Re-run the full ingestion once the leg answers: python "
                    "scripts/ingest_keyword_bank.py",
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
                    "Re-run the bank ingestion: python "
                    "scripts/ingest_keyword_bank.py",
                ],
            )
        )
        return 1
    except PipelineExtractionFailedError as exc:  # defensive: legs absorb it
        print(
            render_intervention_block(
                step="Step 5 Supplier Ingestion",
                reason=str(exc),
                instructions=[
                    "Re-run the bank ingestion after fixing the named engine: "
                    "python scripts/ingest_keyword_bank.py",
                ],
            )
        )
        return 1

    _print_summary(summary, engines)
    if not summary.exports:
        print(
            render_intervention_block(
                step="Step 5 Funnel Exhausted",
                reason=_exhausted_reason(summary),
                instructions=[
                    "Read the per-engine counters above: 0 scraped means the "
                    "supplier answered nothing for these keywords; a large "
                    "rejected count means the viability gate or the margin "
                    "floor held",
                    "Pilot a single keyword whose product is a plain physical "
                    "tool: python scripts/ingest_keyword_bank.py --limit 1",
                    "Re-run once a keyword is proven: python "
                    "scripts/ingest_keyword_bank.py",
                ],
            )
        )
        return 1

    print("\n[STEP 5 COMPLETE] Optimal supplier candidates are ready for Step 6.")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 5: ingest the Step-4 keyword bank through the "
            "CJdropshipping pipeline and export the survivors to the "
            "Morning Anthem gold-kernel tree."
        ),
    )
    parser.add_argument(
        "--keywords",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_KEYWORD_BANK_PATH),
        help=(
            "Step-4 keyword bank to ingest (default: "
            "MORNING_ANTHEM_KEYWORD_BANK_PATH)"
        ),
    )
    parser.add_argument(
        "--target-per-product",
        type=int,
        default=settings.BANK_TARGET_PER_PRODUCT,
        help=(
            "packages to export per (product, engine) leg — the product's "
            "exact keyword fills them first, its fallback only what is left "
            "(default: BANK_TARGET_PER_PRODUCT = %(default)s)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ingest only the first N bank keywords (pilot runs)",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_EXPORT_DIR),
        help="gold-kernel intake root (default: MORNING_ANTHEM_EXPORT_DIR)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[STEP 5 ABORTED] interrupted by the operator.")
        return 1
    except Exception:
        logging.getLogger("ingest_keyword_bank").exception(
            "Unexpected error during bank ingestion"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
