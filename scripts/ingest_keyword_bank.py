#!/usr/bin/env python
"""Step 5 runner: ingest the Step-4 keyword bank through BOTH supplier
pipelines and export the optimal products to the gold-kernel tree.

Every bank keyword runs through both engines — CJdropshipping (MCP) and the
AliExpress Dropshipping Center — under each supplier's own gates, and what
survives the viability gate, the margin floor and the 3-image gallery gate is
written to:

    <OPTIMAL_EXPORT_DIR>/cjdropshipping/product-NN/…
    <OPTIMAL_EXPORT_DIR>/aliexpress/product-NN/…

Both trees are production deliverables, not repo artefacts, and stay
untracked.

Pacing is the point of the pilot flags: one CJ keyword costs up to
CJ_MAX_PRODUCTS product-detail calls plus a freight quote each, and one DS
Center keyword costs a search page plus a per-item record round trip and a
PDP harvest per survivor — so a full 60-keyword bank across two engines is a
long run. Pilot with `--limit 4 --target-per-keyword 1 --only aliexpress`.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
        [--keywords outputs/json/step-4-gold-keywords.json] \
        [--target-per-keyword 2] [--limit 4] \
        [--only {cjdropshipping,aliexpress}] [--export-root <dir>]

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
from src.extractors.aliexpress_ds import (  # noqa: E402
    DsCenterSessionExpiredError,
)
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


def _build_extractor_factories(engine: str):
    """The real extractor factories, optionally narrowed to one engine.

    Narrowing here (rather than an engines parameter on
    `ingest_keyword_bank`) keeps the single-leg pilot on the same code path
    as the production run: the module derives its engine list from the
    factories it is handed.
    """
    from src.main import _EXTRACTOR_REGISTRY

    keys = BANK_ENGINES if engine == "both" else (engine,)
    return {key: _EXTRACTOR_REGISTRY[key] for key in keys}


def _print_pilot_banner(
    count: int, target: int, engines: List[str], export_root: str
) -> None:
    print(
        f"\n[STEP 5 PILOT] {count} bank keyword(s) × {len(engines)} engine(s) "
        f"× target {target} package(s) per (keyword, engine) leg"
    )
    print(f"  engines: {', '.join(engines)}")
    print(f"  export root: {export_root}")
    print(
        "  pacing: CJ runs up to "
        f"{settings.CJ_MAX_PRODUCTS_PER_KEYWORD} detail call(s) + freight "
        "quote(s) per keyword; the DS Center runs a search page + one item "
        "record per hit + a PDP harvest per survivor. That is the pacing, "
        "not a hang."
    )


def _print_summary(summary: BankIngestionSummary, engines: List[str]) -> None:
    print(
        f"\n[BANK INGESTION SUMMARY] keywords={summary.keywords_total} "
        f"attempted={summary.keywords_attempted} "
        f"empty={summary.keywords_empty} exported={summary.total_exports}"
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
            f"duplicate={counters['skipped_duplicate']})")
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
        f"({legs}; {summary.keywords_empty} keyword(s) returned no supplier "
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
    factories = _build_extractor_factories(args.only)
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
                    "Check that at least one supplier engine is usable: "
                    "CJ_MCP_TOKEN in .env for CJdropshipping (the AliExpress "
                    "DS Center needs no credential), and that the network "
                    "allows both endpoints",
                    "Pilot one engine to isolate it: python "
                    "scripts/ingest_keyword_bank.py --limit 2 --only "
                    "aliexpress",
                    "Re-run the full ingestion once one leg answers: python "
                    "scripts/ingest_keyword_bank.py",
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
                    "(ALI_DS_STATE_PATH) — the DS Center answers these calls "
                    "without a login",
                    "Re-run the bank ingestion: python "
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
                    "tool: python scripts/ingest_keyword_bank.py --limit 1 "
                    "--only cjdropshipping",
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
            "Step 5: ingest the Step-4 keyword bank through BOTH supplier "
            "pipelines (CJ MCP + AliExpress DS Center) and export the "
            "survivors to the gold-kernel tree."
        ),
    )
    parser.add_argument(
        "--keywords",
        type=Path,
        default=Path(settings.KEYWORD_BANK_PATH),
        help="Step-4 keyword bank to ingest (default: KEYWORD_BANK_PATH)",
    )
    parser.add_argument(
        "--target-per-keyword",
        type=int,
        default=settings.BANK_TARGET_PER_KEYWORD,
        help=(
            "packages to export per (keyword, engine) leg (default: "
            "BANK_TARGET_PER_KEYWORD = %(default)s)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ingest only the first N bank keywords (pilot runs)",
    )
    parser.add_argument(
        "--only",
        choices=["both"] + list(BANK_ENGINES),
        default="both",
        help="restrict ingestion to one supplier engine (default: %(default)s)",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path(settings.OPTIMAL_EXPORT_DIR),
        help="gold-kernel intake root (default: OPTIMAL_EXPORT_DIR)",
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
