#!/usr/bin/env python
"""Step 2 runner: gate the trend research's candidate strings, then select.

Two Jev passes over the Step-2 research:

1. **Gate** (product intent). Google Trends answers "what are people
   searching", not "what are people buying" — so every candidate is classified
   as a real product search or not. Only product searches go on.
2. **Select** (quality). The surviving pool is wider than the Step-3 scrape can
   afford (one Google Shopping request per keyword, 10 HasData credits each),
   so Jev scores every survivor and code cuts the best
   ``TREND_SELECT_TARGET`` of them — with a per-pillar floor, so a strong
   pillar cannot crowd a weaker one out of the launch.

The **selected** list is written to the Step-2 deliverable (that is what Step 3
reads); the full gated pool is preserved beside it as ``…-gated.json`` (the
re-run reserve, which cost nothing extra to keep), and the pre-gate candidates
as ``…-raw.json`` with the verdict on each row — so every drop and every
non-selection is auditable.

Jev is billed in USD by OpenRouter, NOT in HasData credits — a full research
pass costs fractions of a cent and does not touch the 1,000-credit HasData
envelope this pipeline budgets against.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/gate_trend_keywords.py \
        [--input outputs/json/morning-anthem-step-2-trend-keywords.json] \
        [--batch-size 8]

Exit codes: 0 complete, 1 requires human intervention (missing input file or
Jev credentials), 2 unexpected error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

# Allow `python scripts/gate_trend_keywords.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.main import render_intervention_block  # noqa: E402
from src.ranking.jev_client import JevConfigError  # noqa: E402
from src.research.trend_gate import (  # noqa: E402
    TrendGateError,
    TrendIntentGate,
    load_trend_candidates,
    write_gated_deliverable,
)
from src.research.trend_select import (  # noqa: E402
    TrendKeywordSelector,
    write_selected_deliverable,
)

REPO = Path(__file__).resolve().parents[1]


def _raw_sibling(output: Path) -> Path:
    """Where the pre-gate audit trail goes: `…-raw.json` beside the output."""
    return output.with_name(f"{output.stem}-raw{output.suffix}")


def _gated_sibling(output: Path) -> Path:
    """Where the full gated pool goes: `…-gated.json` beside the output.

    Kept because it is the re-run reserve: the pool already cost its HasData
    credits, so a re-run (or a different selection target) can be fed from it
    without paying for trends again.
    """
    return output.with_name(f"{output.stem}-gated{output.suffix}")


def _print_plan(
    count: int, gate_batch: int, target: int, select_batch: int
) -> None:
    gate_calls = (count + gate_batch - 1) // gate_batch if count else 0
    select_calls = (count + select_batch - 1) // select_batch if count else 0
    print(
        f"\n[STEP 2] {count} candidate string(s) through two Jev passes:"
        f"\n  gate:    {gate_calls} System One call(s) of up to {gate_batch}"
        f" question(s) — product intent"
        f"\n  select:  up to {select_calls} System One call(s) of up to"
        f" {select_batch} question(s) — quality score, then the best {target}"
    )
    print(
        "  cost: OpenRouter USD (Jev), NOT HasData credits — the "
        f"{settings.HASDATA_PRODUCTION_CAP}-credit production envelope is "
        "untouched by this step"
    )


def _print_summary(results) -> None:
    kept = [row for row in results if row.kept]
    print(
        f"\n[TREND GATE SUMMARY] candidates={len(results)} "
        f"product_search={len(kept)} dropped={len(results) - len(kept)}"
    )
    by_intent: dict = {}
    for row in results:
        key = row.intent or "unclassified"
        by_intent[key] = by_intent.get(key, 0) + 1
    for verdict, count in sorted(by_intent.items()):
        print(f"  {verdict}: {count}")
    if kept:
        print("  kept:")
        for row in kept:
            print(f"    - [{row.pillar}] {row.keyword!r}")


def _print_selection_summary(selected) -> None:
    picked = [row for row in selected if row.selected]
    print(
        f"\n[TREND SELECT SUMMARY] gated={len(selected)} "
        f"selected={len(picked)} reserve={len(selected) - len(picked)}"
    )
    by_pillar: dict = {}
    for row in picked:
        by_pillar[row.pillar] = by_pillar.get(row.pillar, 0) + 1
    for pillar, count in sorted(by_pillar.items()):
        print(f"  {pillar}: {count}")
    for row in picked:
        score = f"{row.quality_score:.2f}" if row.quality_score is not None else "n/a"
        print(f"    {row.rank:>2}. [{row.pillar}] {row.keyword!r} (quality {score})")
    unclassified = [row for row in picked if row.quality_score is None]
    if unclassified:
        print(
            f"  note: {len(unclassified)} selected row(s) had no usable Jev "
            "score — they filled remaining slots"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 2: gate the trend research's candidate strings through Jev "
            "so only real product searches go on, then select the best N for "
            "the Step-3 scrape."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_TREND_KEYWORDS_PATH),
        help=(
            "Step-2 candidate file to gate, overwritten in place with the "
            "filtered list (default: MORNING_ANTHEM_TREND_KEYWORDS_PATH)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="filtered destination (default: --input)",
    )
    parser.add_argument(
        "--raw",
        type=Path,
        default=None,
        help=(
            "pre-gate audit trail destination (default: `…-raw.json` beside "
            "the output)"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=(
            "candidate strings per System One call in the GATE pass "
            "(default: TREND_GATE_BATCH_SIZE)"
        ),
    )
    parser.add_argument(
        "--select-batch-size",
        type=int,
        default=None,
        help=(
            "candidate strings per System One call in the SELECT pass "
            "(default: TREND_SELECT_BATCH_SIZE)"
        ),
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help=(
            "how many keywords to select for the Step-3 scrape; this IS the "
            "Step-3 budget (default: TREND_SELECT_TARGET)"
        ),
    )
    parser.add_argument(
        "--min-per-pillar",
        type=int,
        default=None,
        help=(
            "per-pillar floor in the selection cut, so a strong pillar cannot "
            "crowd a weaker one out; 0 selects a pure best-of "
            "(default: TREND_SELECT_MIN_PER_PILLAR)"
        ),
    )
    parser.add_argument(
        "--skip-selection",
        action="store_true",
        help=(
            "gate only: leave every survivor in the deliverable instead of "
            "cutting to --target (the whole pool then costs Step-3 credits)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    output = args.output or args.input
    raw_path = args.raw or _raw_sibling(output)

    try:
        candidates = load_trend_candidates(args.input)
    except TrendGateError as exc:
        print(
            render_intervention_block(
                step="Step 2 Trend Keywords",
                reason=str(exc),
                instructions=[
                    "Run the Step-2 research through the HasData Google "
                    "Trends MCP and write its candidate strings to the file "
                    "above as {\"keywords\": [{keyword, pillar, "
                    "demand_evidence}, …]}",
                    "Tag every candidate with one of the five Morning Anthem "
                    "pillars: brew_fuel, sunrise_move, mindful_awakening, "
                    "restful_reset, morning_table",
                    "Re-run: python scripts/gate_trend_keywords.py",
                ],
            )
        )
        return 1

    batch_size = args.batch_size or settings.TREND_GATE_BATCH_SIZE
    select_batch = args.select_batch_size or settings.TREND_SELECT_BATCH_SIZE
    target = args.target if args.target is not None else settings.TREND_SELECT_TARGET
    min_per_pillar = (
        args.min_per_pillar
        if args.min_per_pillar is not None
        else settings.TREND_SELECT_MIN_PER_PILLAR
    )
    _print_plan(len(candidates), batch_size, target, select_batch)

    try:
        results = TrendIntentGate(batch_size=batch_size).gate(candidates)
    except JevConfigError as exc:
        print(
            render_intervention_block(
                step="Jev Gate Configuration",
                reason=str(exc),
                instructions=[
                    "Set OPENROUTER_API_KEY in .env (https://openrouter.ai/keys) "
                    "— the key is shared by the Step-2 product-intent gate, the "
                    "Step-3 pillar gate and the Step-6 ranking",
                    "Verify the key answers with a one-call probe before "
                    "re-running",
                    "Re-run: python scripts/gate_trend_keywords.py",
                ],
            )
        )
        return 1
    except TrendGateError as exc:
        parser.exit(1, f"error: {exc}\n")

    _print_summary(results)

    survivors = [row for row in results if row.kept]
    gated_path = _gated_sibling(output)
    if args.skip_selection:
        # The gate-only path keeps every survivor as the deliverable, so the
        # whole pool is what Step 3 will scrape (and pay for).
        write_gated_deliverable(results, output, raw_path)
        print(
            f"\nwrote {output} (all {len(survivors)} survivor(s)) and "
            f"{raw_path} (pre-gate audit trail)"
        )
    else:
        # The gated pool goes to the reserve path, NOT to Step 3's intake: if
        # the selection cannot be made, nothing must be left where Step 3 would
        # read it and spend credits on it.
        write_gated_deliverable(results, gated_path, raw_path)
        try:
            selected = TrendKeywordSelector(
                batch_size=select_batch,
                target=target,
                min_per_pillar=min_per_pillar,
            ).select(survivors)
        except JevConfigError as exc:
            print(
                render_intervention_block(
                    step="Jev Selection Configuration",
                    reason=str(exc),
                    instructions=[
                        "Set OPENROUTER_API_KEY in .env — the selection pass "
                        "uses the same Jev credential as the gate",
                        "Re-run: python scripts/gate_trend_keywords.py",
                    ],
                )
            )
            return 1
        picked = [row for row in selected if row.selected]
        if survivors and not picked:
            # Inverted tolerance: Jev scored nothing, so there is no basis for
            # the cut. Spending the Step-3 budget on an unranked pool is the
            # failure this pass prevents, so stop instead.
            print(
                render_intervention_block(
                    step="Step 2 Selection Failed",
                    reason=(
                        f"Jev returned no usable quality score for any of the "
                        f"{len(survivors)} gated keyword(s) — the selection "
                        "cannot be made, and scraping an unranked pool would "
                        f"spend {len(survivors)} x "
                        f"{settings.HASDATA_SHOPPING_CREDITS_PER_REQUEST} "
                        "HasData credits on no basis."
                    ),
                    instructions=[
                        "Check the Jev endpoint and key: the gate pass just "
                        "answered, so a selection-only failure usually means "
                        "rate limiting or an envelope change",
                        "Re-run: python scripts/gate_trend_keywords.py",
                        "To proceed unranked instead, re-run with "
                        "--skip-selection (the whole gated pool then becomes "
                        "Step 3's intake, and its budget)",
                    ],
                )
            )
            return 1

        write_selected_deliverable(selected, output, gated_path)
        _print_selection_summary(selected)
        print(
            f"\nwrote {output} ({len(picked)} selected of {len(survivors)}), "
            f"{gated_path} (the full gated pool) and {raw_path} (pre-gate "
            "audit trail)"
        )

    if not any(row.kept for row in results):
        print(
            render_intervention_block(
                step="Step 2 Funnel Exhausted",
                reason=(
                    f"0 of {len(results)} candidate string(s) were classified "
                    "as product searches — nothing is left for the Step-3 "
                    "scrape."
                ),
                instructions=[
                    "Read the per-verdict tally above: a dominant "
                    "`informational` or `news_or_brand` verdict means the "
                    "research gathered topic strings rather than shopping "
                    "strings",
                    "Re-run the Step-2 Trends research asking for product "
                    "searches (what a shopper types to buy an object)",
                    "Re-run: python scripts/gate_trend_keywords.py",
                ],
            )
        )
        return 1

    print("\n[STEP 2 COMPLETE] Product-search strings are ready for Step 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
