#!/usr/bin/env python
"""Step 4 runner: build the supplier keyword bank from the Step-3 gold list.

**Exact title first.** Every gold product's primary keyword is its own title,
derived in code — the highest-precision query there is for finding the identical
product. The configured reasoning LLM contributes only the optional **fallback**
for a product whose exact title finds nothing: one tightly-specific variant of
that title, which the model may decline to write.

Produces the bank Step 5 feeds to CJdropshipping:

    <KEYWORD_BANK_PATH>  = outputs/json/morning-anthem-step-4-keyword-bank.json

That file is a production deliverable, not a repo artefact, and stays untracked.

A run spends real LLM tokens but **no HasData credits** — the gold list it reads
was bought in Step 3, and nothing here calls a scraping API. The runner prints
the product count, the expected bank size and the LLM-call estimate before the
first call, and writes nothing at all if the bank fails validation.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/generate_gold_keywords.py \
        [--gold-products outputs/json/morning-anthem-step-3-gold-products.json] \
        [--batch-size 4] [--max-products 150] \
        [--output outputs/json/morning-anthem-step-4-keyword-bank.json]

Exit codes: 0 complete, 1 requires human intervention (no eligible gold products,
or a bank that failed validation), 2 unexpected error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import List

# Allow `python scripts/generate_gold_keywords.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import digest_path, settings  # noqa: E402
from src.keywords.generator import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_PRODUCTS,
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
)
from src.main import render_intervention_block  # noqa: E402
from src.pipeline.atomic_write import write_text_atomic  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def print_plan(generator: KeywordGenerator) -> None:
    """What this run will ask for, before it spends a token."""
    products = len(generator.products)
    calls = -(-products // generator.batch_size)  # ceil
    print(
        f"\n[STEP 4] {products} eligible product(s) -> one exact keyword each "
        f"(derived in code) plus at most one fallback each"
    )
    print(
        f"  bank size: {products} exact + 0-{products} fallback "
        f"(~{products}-{2 * products} rows)"
    )
    print(
        f"  LLM: {calls} call(s) of {generator.batch_size} product(s), for the "
        "fallbacks only — the exact rows cost nothing"
    )
    print("  cost: LLM tokens only; no HasData credits are spent by this step")


def render_markdown(candidates: List[CandidateKeyword]) -> str:
    """Human digest: each product with its exact keyword and its fallback."""
    per_pillar = Counter(c.pillar for c in candidates)
    exact = [c for c in candidates if c.match == "exact"]
    fallbacks = [c for c in candidates if c.match == "tight_fallback"]
    lines = [
        "# Step 4 — the gold-standard keyword bank (The Morning Anthem)",
        "",
        f"{len(candidates)} supplier keyword(s) from {len(exact)} gold "
        f"product(s): **{len(exact)} exact-title** keyword(s) (derived in code "
        f"from each product's own title) and **{len(fallbacks)} fallback(s)** "
        "(model-authored, used only if the exact title finds nothing). "
        "**Not tracked.** This file is Step 5's keyword bank.",
        "",
        f"Pillars: {dict(per_pillar)}",
        "",
    ]
    by_product: dict = {}
    for candidate in candidates:
        by_product.setdefault(candidate.product, []).append(candidate)
    for product, rows in by_product.items():
        pillar = rows[0].pillar
        lines.append(f"### {product}")
        lines.append(f"- pillar: `{pillar}`")
        for row in rows:
            if row.match == "exact":
                lines.append(f"- **exact** — `{row.keyword}`")
            else:
                lines.append(f"- fallback — `{row.keyword}` ({row.rationale})")
        lines.append("")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Step 4: build the supplier keyword bank from the Step-3 gold list — "
            "each product's exact title first, plus one optional fallback."
        ),
    )
    parser.add_argument(
        "--gold-products",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_GOLD_PRODUCTS_PATH),
        help="Step-3 gold-standard products (default: MORNING_ANTHEM_GOLD_PRODUCTS_PATH)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="products per LLM call (default: %(default)s)",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=DEFAULT_MAX_PRODUCTS,
        help="cap on the product table (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_KEYWORD_BANK_PATH),
        help="the bank (default: MORNING_ANTHEM_KEYWORD_BANK_PATH)",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="readable digest destination (default: outputs/md/ beside --output)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        generator = KeywordGenerator(
            gold_products_path=args.gold_products,
            max_products=args.max_products,
            batch_size=args.batch_size,
        )
    except KeywordGenerationConfigError as exc:
        print(
            render_intervention_block(
                step="LLM Evaluation Filter Configuration",
                reason=str(exc),
                instructions=[
                    "Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env "
                    "(e.g. an Ollama / OpenAI-compatible endpoint)",
                    "Re-run: python scripts/generate_gold_keywords.py",
                ],
            )
        )
        return 1
    except KeywordGenerationError as exc:
        print(
            render_intervention_block(
                step="Step 4 Gold Products",
                reason=str(exc),
                instructions=[
                    "Run Step 3 first: python "
                    "scripts/run_gold_standard_research.py",
                    "Confirm the gold deliverable exists and holds products, "
                    "then re-run: python scripts/generate_gold_keywords.py",
                ],
            )
        )
        return 1

    print_plan(generator)

    try:
        candidates = generator.generate_bank()
    except KeywordGenerationError as exc:
        print(
            render_intervention_block(
                step="Step 4 Keyword Bank",
                reason=str(exc),
                instructions=[
                    "Read the validation problems above: a row naming an "
                    "unknown product or an exact keyword that does not match "
                    "its product's title is a code bug, not a model slip",
                    "Re-run once the cause is fixed: python "
                    "scripts/generate_gold_keywords.py",
                ],
            )
        )
        return 1

    markdown_path = args.markdown or digest_path(args.output)
    payload = {
        "keywords": [candidate.model_dump() for candidate in candidates],
        "generated_by": settings.LLM_MODEL,
        "source_products": len(generator.products),
        "strategy": "exact_title_first",
        "counts": {
            "exact": sum(1 for c in candidates if c.match == "exact"),
            "tight_fallback": sum(1 for c in candidates if c.match == "tight_fallback"),
        },
        "note": (
            "GOLD-STANDARD keyword bank for Step 5 CJdropshipping ingestion; "
            "not tracked"
        ),
    }
    write_text_atomic(
        args.output, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    write_text_atomic(markdown_path, render_markdown(candidates))

    exact = payload["counts"]["exact"]
    fallbacks = payload["counts"]["tight_fallback"]
    print(
        f"\n{len(candidates)} keyword(s) from {len(generator.products)} "
        f"product(s): {exact} exact + {fallbacks} fallback"
    )
    print(f"  pillars: {dict(Counter(c.pillar for c in candidates))}")
    print(f"wrote {args.output} and {markdown_path}")
    print("\n[STEP 4 COMPLETE] The keyword bank is ready for Step 5.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
