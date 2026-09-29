#!/usr/bin/env python
"""Step 6 runner: rank every Step-5 gold-kernel package against the Step-3
gold-standard products with Jev (TypeSafe System One via OpenRouter).

Reads the gold-kernel tree Step 5 wrote —

    <OPTIMAL_EXPORT_DIR>/cjdropshipping/product-NN/…
    <OPTIMAL_EXPORT_DIR>/aliexpress/product-NN/…

— and the Step-3 deliverable (`outputs/step-3-gold-standard-products.json`),
asks Jev for a similarity score, a winning-value score and a pillar per
package in batches (default 4 packages per System One call), and writes a
tiered report:

    outputs/step-6-ranked-candidates.json   (the structured ranking)
    outputs/step-6-ranked-candidates.md     (the tier-grouped digest)

`rank_score = 0.6*similarity + 0.4*value` on a 1–5 scale; shortlist ≥
`JEV_SHORTLIST_MIN_SCORE`, review ≥ `JEV_REVIEW_MIN_SCORE`, else disregard.

**This step reports, it does not delete.** No package directory is moved or
removed — Step 7 (human) decides what to validate and link.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/rank_optimal_candidates.py \
        [--gold-products outputs/step-3-gold-standard-products.json] \
        [--export-root <OPTIMAL_EXPORT_DIR>] [--batch-size 4] \
        [--output outputs/step-6-ranked-candidates.json]

Exit codes: 0 complete, 1 requires human intervention, 2 unexpected error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List

# Allow `python scripts/rank_optimal_candidates.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.main import render_intervention_block  # noqa: E402
from src.ranking.jev_client import JevConfigError  # noqa: E402
from src.ranking.jev_product_ranker import (  # noqa: E402
    JevProductRanker,
    JevRankingError,
    RankedPackage,
    write_reports,
)

REPO = Path(__file__).resolve().parents[1]

DEFAULT_OUTPUT = Path("outputs/step-6-ranked-candidates.json")


def _engine_of(package_dir: str) -> str:
    """The supplier folder a package path sits under."""
    return Path(package_dir).parent.name


def _print_ranked(ranked: List[RankedPackage]) -> None:
    per_tier = Counter(package.tier for package in ranked)
    print(
        f"\n[STEP 6 RANKING SUMMARY] packages={len(ranked)} "
        f"shortlist={per_tier['shortlist']} review={per_tier['review']} "
        f"disregard={per_tier['disregard']}"
    )
    print(
        f"  thresholds: shortlist >= {settings.JEV_SHORTLIST_MIN_SCORE} · "
        f"review >= {settings.JEV_REVIEW_MIN_SCORE} "
        f"(rank = 0.6*similarity + 0.4*value, 1-5 scale)"
    )
    for package in ranked:
        notes = f" — {package.notes}" if package.notes else ""
        print(
            f"  [{package.tier:>9}] {package.rank_score:4.2f}  "
            f"sim={package.similarity_score if package.similarity_score is None else round(package.similarity_score, 2)} "
            f"val={package.value_score if package.value_score is None else round(package.value_score, 2)} "
            f"{package.pillar or '—':<16} {package.package_dir}{notes}"
        )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 6: rank the Step-5 gold-kernel packages against the Step-3 "
            "gold-standard products with Jev (TypeSafe System One)."
        ),
    )
    parser.add_argument(
        "--gold-products",
        type=Path,
        default=Path(settings.GOLD_PRODUCTS_PATH),
        help="Step-3 gold-standard products (default: GOLD_PRODUCTS_PATH)",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path(settings.OPTIMAL_EXPORT_DIR),
        help="Step-5 gold-kernel root (default: OPTIMAL_EXPORT_DIR)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.JEV_BATCH_SIZE,
        help=(
            "packages per System One call (default: JEV_BATCH_SIZE = "
            "%(default)s; three questions per package)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="structured ranking destination (default: %(default)s)",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="tiered digest destination (default: --output with .md)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    markdown_path = args.markdown or args.output.with_suffix(".md")

    try:
        ranker = JevProductRanker(
            gold_products_path=args.gold_products,
            export_root=args.export_root,
            batch_size=args.batch_size,
        )
        packages = ranker.collect_packages()
    except JevConfigError as exc:
        print(
            render_intervention_block(
                step="LLM Evaluation Filter Configuration",
                reason=str(exc),
                instructions=[
                    "Set OPENROUTER_API_KEY in .env to your OpenRouter key "
                    "(`sk-or-v1-…`) — it authorises the Jev / System One calls",
                    "Optional overrides: OPENROUTER_BASE_URL (default "
                    "https://openrouter.ai/api/v1) and JEV_MODEL (default "
                    "typesafe/jev-1.13)",
                    "Confirm the key works with a single call, then re-run: "
                    "python scripts/rank_optimal_candidates.py",
                ],
            )
        )
        return 1
    except JevRankingError as exc:
        print(
            render_intervention_block(
                step="Step 6 Ranking Intake",
                reason=str(exc),
                instructions=[
                    "Confirm the Step-3 deliverable exists and holds products: "
                    "outputs/step-3-gold-standard-products.json (re-run "
                    "scripts/run_gold_standard_research.py if not)",
                    "Confirm the Step-5 gold-kernel tree holds product-NN "
                    "packages: <OPTIMAL_EXPORT_DIR>/{cjdropshipping,"
                    "aliexpress}/ (re-run scripts/ingest_keyword_bank.py if not)",
                    "Re-run the ranking: python scripts/rank_optimal_candidates.py",
                ],
            )
        )
        return 1

    per_supplier: Dict[str, int] = Counter(_engine_of(p.package_dir) for p in packages)
    print(
        f"\n[STEP 6 INTAKE] {len(packages)} package(s): "
        + ", ".join(f"{engine}={count}" for engine, count in sorted(per_supplier.items()))
    )
    print(f"  gold products: {len(ranker.gold_products)} ({args.gold_products})")
    print(
        f"  batching: {args.batch_size} package(s) per System One call -> "
        f"{-(-len(packages) // args.batch_size)} call(s), 3 questions each"
    )
    print(f"  model: {settings.JEV_MODEL} via {settings.OPENROUTER_BASE_URL}")

    try:
        ranked = ranker.rank()
    except JevRankingError as exc:
        parser.exit(1, f"error: {exc}\n")

    _print_ranked(ranked)
    write_reports(
        ranked,
        args.output,
        markdown_path,
        gold_products_path=args.gold_products,
        export_root=args.export_root,
        batch_size=args.batch_size,
        shortlist_min_score=ranker.shortlist_min_score,
        review_min_score=ranker.review_min_score,
    )
    print(f"\nwrote {args.output} and {markdown_path}")
    print("\n[STEP 6 COMPLETE] Ranked candidates are ready for Step 7 (human).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[STEP 6 ABORTED] interrupted by the operator.")
        sys.exit(1)
    except Exception:
        logging.getLogger("rank_optimal_candidates").exception(
            "Unexpected error during Step 6 ranking"
        )
        sys.exit(2)
