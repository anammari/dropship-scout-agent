#!/usr/bin/env python
"""Step 7 runner: clean the gold-kernel tree from the Step-6 ranking report.

**This deletes product packages, and deletion is irreversible.** A package holds
images downloaded from the supplier's CDN and a `metadata.json` assembled from a
paid HasData scrape, CJ round trips and LLM calls — so this runner is a **dry run
by default** and only deletes when told to:

    kept        every `shortlist` package
    kept        the top CLEANUP_REVIEW_KEEP (10) `review` packages by rank_score
    deleted     the remaining `review` packages
    deleted     every `disregard` package

Review the dry run before running it for real:

    # 1. see exactly what would go
    source .venv/bin/activate && python scripts/cleanup_candidates.py

    # 2. delete, once the plan looks right
    source .venv/bin/activate && python scripts/cleanup_candidates.py --apply

Nothing is written by this step — the Step-6 report already records every verdict,
including the ones cleanup acted on.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/cleanup_candidates.py \\
        [--report outputs/json/morning-anthem-step-6-ranked-candidates.json] \\
        [--export-root <dir>] [--review-keep 10] [--max-per-family 5] [--apply]

Exit codes: 0 complete (including a dry run), 1 requires human intervention (no
readable ranking report), 2 unexpected error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List

# Allow `python scripts/cleanup_candidates.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.main import render_intervention_block  # noqa: E402
from src.pipeline.cleanup import (  # noqa: E402
    CleanupError,
    CleanupReport,
    apply_cleanup,
    load_ranked_packages,
    plan_cleanup,
    summarize,
)

REPO = Path(__file__).resolve().parents[1]


def _print_plan(
    actions, supplier_root: Path, keep: int, apply: bool,
    max_per_family: int = 0,
) -> None:
    counts = summarize(actions)
    mode = "APPLYING DELETIONS" if apply else "DRY RUN — nothing will be deleted"
    print(f"\n[STEP 7] {mode}")
    print(f"  supplier root: {supplier_root}")
    print(f"  review cap:    keep the top {keep} by rank_score")
    print(
        "  family cap:    "
        + (f"at most {max_per_family} of one family, globally"
           if max_per_family > 0 else "disabled")
    )
    print(
        f"  plan: {counts['kept']} kept, {counts['deleted']} to delete, "
        f"{counts['refused']} refused/unknown (left alone)"
    )
    if counts["deleted"]:
        print("  to delete:")
        for action in actions:
            if action.delete:
                print(f"    - {action.package_dir}  [{action.tier}] {action.reason}")
    refused = [
        action
        for action in actions
        if not action.delete
        and ("refused" in action.reason or "unknown tier" in action.reason)
    ]
    if refused:
        print("  refused (NOT deleted):")
        for action in refused:
            print(f"    - {action.package_dir}  {action.reason}")


def _print_result(report, apply: bool) -> None:
    if not apply:
        print("\n  dry run complete — no directory was touched")
        return
    print(
        f"\n  deleted {len(report.deleted)} package(s), "
        f"{len(report.already_absent)} already absent, "
        f"{len(report.failed)} failed"
    )
    for path, error in report.failed:
        print(f"    ! {path}: {error}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 7: delete the packages the Step-6 ranking rejected — shortlist "
            "kept, review capped, disregard removed. Dry run unless --apply."
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_RANKED_CANDIDATES_PATH),
        help="Step-6 ranking report (default: MORNING_ANTHEM_RANKED_CANDIDATES_PATH)",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_EXPORT_DIR),
        help="exact dated batch root holding product-NN (default: "
             "MORNING_ANTHEM_EXPORT_DIR)",
    )
    parser.add_argument(
        "--review-keep",
        type=int,
        default=settings.CLEANUP_REVIEW_KEEP,
        help="how many `review` packages survive, by rank_score (default: %(default)s)",
    )
    parser.add_argument(
        "--max-per-family",
        type=int,
        default=settings.CLEANUP_MAX_PER_FAMILY,
        help=(
            "GLOBAL ceiling on one product family (e.g. dog gear), by rank_score. "
            "Overrides the shortlist keep; 0 disables (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually delete. Without this the run is a dry run: it prints the "
            "plan and removes nothing. Deletion is irreversible and a package "
            "costs HasData credits, CJ round trips and LLM calls to rebuild"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        packages = load_ranked_packages(args.report)
        actions = plan_cleanup(
            packages,
            export_root=args.export_root,
            review_keep=args.review_keep,
            max_per_family=args.max_per_family,
        )
    except CleanupError as exc:
        print(
            render_intervention_block(
                step="Step 7 Ranking Report",
                reason=str(exc),
                instructions=[
                    "Rank the gold kernel first: python "
                    "scripts/rank_optimal_candidates.py",
                    "Confirm the report exists and holds a 'packages' list, "
                    "then re-run: python scripts/cleanup_candidates.py",
                ],
            )
        )
        return 1

    supplier_root = args.export_root
    _print_plan(
        actions, supplier_root, args.review_keep, args.apply,
        max_per_family=args.max_per_family,
    )

    # Only `--apply` reaches the filesystem. The plan is always computed (so the
    # dry run can print it), but acting on it is what the flag is FOR — an
    # earlier version called apply unconditionally and deleted on a dry run.
    report = (
        apply_cleanup(actions, export_root=args.export_root)
        if args.apply
        else CleanupReport()
    )
    _print_result(report, args.apply)

    if report.failed:
        # A deletion that failed leaves a package the report says is gone, so it
        # is worth an intervention rather than a quiet line in the log.
        print(
            render_intervention_block(
                step="Step 7 Cleanup",
                reason=(
                    f"{len(report.failed)} package(s) could not be deleted; the "
                    "workspace does not match the ranking report."
                ),
                instructions=[
                    "Read the failure lines above — a permissions problem or a "
                    "file in use is the usual cause",
                    "Re-run once resolved: python scripts/cleanup_candidates.py "
                    "--apply",
                ],
            )
        )
        return 1

    if args.apply:
        print("\n[STEP 7 COMPLETE] The workspace holds only the packages still in play.")
    else:
        print(
            "\n[STEP 7 DRY RUN] Nothing was deleted. Re-run with --apply once the "
            "plan above looks right."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
