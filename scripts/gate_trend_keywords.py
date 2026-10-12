#!/usr/bin/env python
"""Step 2 runner: gate the trend research's candidate strings through Jev.

Google Trends answers "what are people searching", not "what are people
buying" — so every candidate search string is classified by Jev (TypeSafe
System One via OpenRouter) as a real product search or not, and only product
searches reach Step 3. The gate is what stops a how-to, news or brand query
from costing a 10-credit HasData Google Shopping request.

The gated list overwrites the Step-2 deliverable (that is what Step 3 reads);
the pre-gate candidates are preserved beside it as ``…-raw.json`` with the
gate's verdict on each row, so a drop is always auditable.

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

REPO = Path(__file__).resolve().parents[1]


def _raw_sibling(output: Path) -> Path:
    """Where the pre-gate audit trail goes: `…-raw.json` beside the output."""
    return output.with_name(f"{output.stem}-raw{output.suffix}")


def _print_plan(count: int, batch_size: int) -> None:
    calls = (count + batch_size - 1) // batch_size if count else 0
    print(
        f"\n[STEP 2 GATE] {count} candidate string(s) classified by Jev in "
        f"{calls} System One call(s) of up to {batch_size} question(s)"
    )
    print(
        "  cost: OpenRouter USD (Jev), NOT HasData credits — the 250-credit "
        "production envelope is untouched by this step"
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 2: gate the trend research's candidate strings through Jev "
            "so only real product searches reach the Step-3 scrape."
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
        help="candidate strings per System One call (default: %(default)s)",
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
    _print_plan(len(candidates), batch_size)

    try:
        gate = TrendIntentGate(batch_size=batch_size)
        results = gate.gate(candidates)
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

    write_gated_deliverable(results, output, raw_path)
    _print_summary(results)
    print(f"\nwrote {output} (filtered) and {raw_path} (pre-gate audit trail)")

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
