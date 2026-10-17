#!/usr/bin/env python
"""Step 3 runner: scrape Google Shopping AU for the Step-2 trend keywords
and curate the GOLD-STANDARD product list (The Morning Anthem pipeline).

HasData is the pipeline's EXCLUSIVE data provider — the Apify actor wrapper
and the `--source` flag were retired on the Morning Anthem branch. One
HasData Google Shopping request per keyword (10 credits each, the published
rate); one request returns the whole shopping grid for the query (~65 rows
observed live 2026-10-07), so `--num` is a LOCAL slice of that grid — it
bounds curation cost, never API spend.

The scrape is pinned to the AU market (`SHOPPING_GL`/`SHOPPING_DOMAIN`) so
demand signals and price benchmarks reflect Australian shopper behaviour and
AUD retail pricing (the store launches selling to Australia).

Then the reasoning LLM curates the rows (`src.evaluators.gold_curator`) and code
assembles the deliverable from the verbatim scraped rows — the LLM selects by
url, it never authors a product fact. Every curated product is finally
classified by the **Jev pillar gate** (`src/research/pillar_gate.py`) into one of
the five Morning Anthem pillars, or discarded; the gate runs last, and only for
that reason does it see each product's numeric price, rating and review count
when it decides. Both outputs default under
`outputs/` (git-ignored — production deliverables of the Morning Anthem
pipeline, never repo artefacts).

A run spends real HasData credits AND real LLM tokens. The planned envelope
(keywords x 10 credits) is printed BEFORE the first call and the run refuses
to start when it would exceed `HASDATA_GS_MAX_KEYWORDS` or the
`HASDATA_PRODUCTION_CAP` credit ceiling. Use `--limit` to pilot: `--limit 1`
is exactly 1 request (10 credits), the only pilot shape accepted without
`--allow-over-pilot-budget`. Curated gold products must carry on-page demand
evidence — rows with no rating/review KPI are filtered out before the LLM.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/run_gold_standard_research.py \
        [--limit 1 --dump-raw /tmp/step3_raw_rows.json]

The runner exits non-zero when nothing usable comes back — nothing is
written for a failed or empty run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

# Allow `python scripts/run_gold_standard_research.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import digest_path, settings  # noqa: E402
from src.evaluators.gold_curator import (  # noqa: E402
    GoldCurationError,
    GoldProductCurator,
    GoldStandardProduct,
)
from src.extractors.google_shopping_hasdata import (  # noqa: E402
    HasDataShoppingError,
    HasDataShoppingScraper,
    price_value_from_text,
)
from src.extractors.shopping_row import ShoppingRow  # noqa: E402
from src.main import render_intervention_block  # noqa: E402
from src.pipeline.atomic_write import write_text_atomic  # noqa: E402
from src.ranking.jev_client import JevConfigError  # noqa: E402
from src.research.pillar_gate import (  # noqa: E402
    PillarCandidate,
    PillarGate,
    to_gold_products,
)
from src.research.trend_gate import TrendGateError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: The Morning Anthem merchandising pillars, in digest order. Single source
#: of truth is src.keywords.generator.PILLARS — imported lazily here so the
#: digest and the validators can never drift apart.
_PILLAR_ORDER = (
    "brew_fuel",
    "sunrise_move",
    "mindful_awakening",
    "restful_reset",
    "morning_table",
)

#: The pilot spend ceiling: one keyword = one request = 10 credits. A pilot
#: run may not plan more than this without an explicit operator override.
PILOT_CREDIT_CAP = HasDataShoppingScraper.CREDITS_PER_REQUEST


def print_spend_plan(keywords: list[str], results_per_keyword: int) -> None:
    """The honest pre-spend envelope, in HasData credits.

    One request per keyword; the request count IS the keyword count, and the
    keyword count IS the spend. One request returns the whole grid per query
    (~65 rows observed), so the local row cap bounds LLM curation only, never
    HasData spend. The rate is read from config so the banner and the
    guardrail arithmetic cannot disagree.
    """
    requests = len(keywords)
    rate = settings.HASDATA_SHOPPING_CREDITS_PER_REQUEST
    credits = requests * rate
    print(
        f"planned HasData spend: {requests} keyword(s) x 1 request "
        f"({rate} credits) = {credits} credit(s), at the published Google "
        f"Shopping rate. One request returns the whole grid per query (~65 "
        f"rows observed); the local row cap of {results_per_keyword} bounds "
        f"LLM curation only, never HasData spend."
    )
    cap = settings.HASDATA_GS_MAX_KEYWORDS
    budget = settings.HASDATA_PRODUCTION_CAP
    trends = (
        settings.TRENDS_MAX_CALLS * settings.HASDATA_TRENDS_CREDITS_PER_CALL
    )
    print(
        f"guardrails: keyword cap {cap} (refuses above), production credit "
        f"cap {budget} of {settings.HASDATA_MONTHLY_BUDGET}/month free tier "
        f"(Step 2 trends budget {trends} + this scrape {credits} would total "
        f"{trends + credits})"
    )


def load_step2_keywords(path: Path) -> list[str]:
    """Read the Step-2 deliverable's keyword strings."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read Step-2 keyword file {path}: {exc}")
    keywords = [
        str(entry.get("keyword", "")).strip()
        for entry in payload.get("keywords", [])
        if isinstance(entry, dict) and str(entry.get("keyword", "")).strip()
    ]
    if not keywords:
        raise SystemExit(f"error: no keywords in {path} — run Step 2 first")
    return keywords


def write_markdown(
    path: Path, products: list[GoldStandardProduct]
) -> None:
    """Human digest: gold-standard products grouped by Morning Anthem pillar.

    Every product here has already passed the Jev pillar gate, so its pillar is
    one of the five — the grouping is the gate's verdict, not a suggestion.
    """
    per_pillar = Counter(p.pillar for p in products)
    lines = [
        "# Step 3 — GOLD-STANDARD winning products (The Morning Anthem, "
        "Google Shopping AU)",
        "",
        f"{len(products)} products curated from live Google Shopping AU rows "
        f"(HasData source) over the Step-2 trend keyword file, "
        f"annotated by `{settings.LLM_MODEL}`. **Not tracked.** "
        "**This file is the reference set Steps 4 and 6 measure against.**",
        "",
        f"Pillars: {dict(per_pillar)}",
        "",
    ]
    for pillar in _PILLAR_ORDER:
        group = [p for p in products if p.pillar == pillar]
        if not group:
            continue
        lines += [f"## {pillar} ({len(group)})", ""]
        for product in group:
            lines.append(f"### {product.name}")
            price = product.retail_price_text or "—"
            lines.append(f"- price: {price} · demand: {product.demand_evidence}")
            lines.append(f"- keyword: `{product.source_keyword}`")
            lines.append(f"- compliance: {product.compliance_note}")
            if product.unit_economics_note:
                lines.append(f"- economics: {product.unit_economics_note}")
            lines.append(f"- {product.url}")
            lines.append("")
    write_text_atomic(path, "\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 3: Google Shopping AU gold-standard product research "
                    "(HasData only).",
    )
    parser.add_argument(
        "--keywords",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_TREND_KEYWORDS_PATH),
        help="Step-2 deliverable to seed from",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only the first N keywords (pilot runs; keeps scrape spend tiny)",
    )
    parser.add_argument(
        "--allow-over-pilot-budget",
        action="store_true",
        help="allow a planned spend above the 10-credit pilot cap "
             f"({PILOT_CREDIT_CAP} credit = 1 keyword)",
    )
    parser.add_argument(
        "--num",
        type=int,
        default=None,
        help=(
            "LOCAL cap on rows kept per keyword — NOT an API parameter; one "
            f"request returns the whole grid whatever the slice (default "
            f"{settings.HASDATA_GS_MAX_RESULTS_PER_KEYWORD})"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(settings.MORNING_ANTHEM_GOLD_PRODUCTS_PATH),
        help="structured gold product destination",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="readable digest destination (default: outputs/md/ beside --output)",
    )
    parser.add_argument(
        "--dump-raw",
        type=Path,
        default=None,
        help="also dump the raw scraped rows here (spike evidence; /tmp)",
    )
    parser.add_argument(
        "--from-raw",
        type=Path,
        default=None,
        help="skip the scrape entirely and curate rows from a previous "
             "--dump-raw file (zero-credit curation replays/debugging)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    if args.from_raw:
        print(
            f"curating cached rows from {args.from_raw} — zero scrape spend, "
            "no run planned (the Step-2 keyword file is not read on this path)"
        )
    else:
        keywords = load_step2_keywords(args.keywords)
        if args.limit is not None:
            keywords = keywords[: args.limit]
        results_per_keyword = (
            args.num or settings.HASDATA_GS_MAX_RESULTS_PER_KEYWORD
        )
        # Fail closed BEFORE any credit moves: the keyword count is the
        # spend, so the caps are checked on the planned keyword list.
        if len(keywords) > settings.HASDATA_GS_MAX_KEYWORDS:
            parser.exit(
                1,
                f"error: {len(keywords)} keyword(s) exceeds the HasData "
                f"keyword cap HASDATA_GS_MAX_KEYWORDS="
                f"{settings.HASDATA_GS_MAX_KEYWORDS} — narrow the Step-2 "
                "list or raise the cap in .env\n",
            )
        planned_credits = (
            len(keywords) * settings.HASDATA_SHOPPING_CREDITS_PER_REQUEST
        )
        if planned_credits > settings.HASDATA_PRODUCTION_CAP:
            parser.exit(
                1,
                f"error: planned spend {planned_credits} credits exceeds the "
                f"production cap HASDATA_PRODUCTION_CAP="
                f"{settings.HASDATA_PRODUCTION_CAP} — narrow the Step-2 "
                "list or raise the cap in .env\n",
            )
        if (
            planned_credits > PILOT_CREDIT_CAP
            and not args.allow_over_pilot_budget
        ):
            parser.exit(
                1,
                f"error: planned spend {planned_credits} credits exceeds "
                f"the pilot cap ({PILOT_CREDIT_CAP} credit = 1 keyword). "
                "This is a pilot guardrail, not a hard limit — re-run with "
                "--allow-over-pilot-budget for a deliberate larger run, or "
                "use --limit 1 for a pilot.\n",
            )
        print_spend_plan(keywords, results_per_keyword)

    markdown_path = args.markdown or digest_path(args.output)
    try:
        scraper = None
        if args.from_raw:
            # Curate a previous scrape's dump — no scrape source is built.
            raw = json.loads(args.from_raw.read_text(encoding="utf-8"))
            rows = [ShoppingRow(**entry) for entry in raw]
            # A dump written before `price_value` existed carries `price_text`
            # but no numeric price, and the Step-3 price filter would drop every
            # such row. Rebuild the number from the saved display price so a
            # stored scrape still replays.
            rebuilt = 0
            for row in rows:
                if row.price_value is None:
                    value = price_value_from_text(row.price_text)
                    if value is not None:
                        row.price_value = value
                        rebuilt += 1
            if rebuilt:
                print(
                    f"  rebuilt price_value for {rebuilt} legacy row(s) from "
                    "their display price"
                )
            # The Step-2 file is irrelevant on this path; report the
            # keywords the rows themselves claim instead.
            keywords = sorted({row.source_keyword for row in rows})
        else:
            scraper = HasDataShoppingScraper(
                results_per_keyword=results_per_keyword
            )
            rows = scraper.scrape_keywords(keywords)
            if args.dump_raw:
                args.dump_raw.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(
                    args.dump_raw,
                    json.dumps(
                        [vars(row) for row in rows], indent=1, ensure_ascii=False
                    ),
                )
        curator = GoldProductCurator()
        products = curator.curate(rows)
        # The pillar gate runs LAST, on the curated products, so its decision
        # sees each row's numeric price and rating (operator decision
        # 2026-10-12). It is the pillar's single author.
        row_by_url = {row.url.strip(): row for row in rows}
        candidates = [
            PillarCandidate.from_product(product, row_by_url.get(product.url.strip()))
            for product in products
        ]
        gated = PillarGate().gate(candidates)
        products = to_gold_products(products, gated)
    except (
        HasDataShoppingError,
        GoldCurationError,
        TrendGateError,
    ) as exc:
        parser.exit(1, f"error: {exc}\n")
    except JevConfigError as exc:
        print(
            render_intervention_block(
                step="Jev Pillar Gate Configuration",
                reason=str(exc),
                instructions=[
                    "Set OPENROUTER_API_KEY in .env — the pillar gate uses the "
                    "same Jev credential as the Step-2 passes",
                    "Re-run: python scripts/run_gold_standard_research.py",
                ],
            )
        )
        return 1

    by_pillar = Counter(p.pillar for p in products)
    dropped = [row for row in gated if not row.kept]

    print(
        f"\nscraped {len(rows)} row(s) -> curated {len(gated)} -> "
        f"{len(products)} gold product(s) after the Jev pillar gate"
    )
    print(f"  pillars: {dict(by_pillar)}")
    if dropped:
        print(f"  discarded by the gate: {len(dropped)}")
        for row in dropped[:10]:
            print(f"    - {row.candidate.name!r}: {row.note}")
    if scraper and scraper.last_run:
        run = scraper.last_run
        print(f"run {run.get('source')}: ~{run.get('credits')} HasData credit(s)")

    if not products:
        # The gate kept nothing, so there is no gold list — and writing an empty
        # one would leave Steps 4+6 reading a file that looks like a result.
        # Checked BEFORE the writes, so a failed gate leaves no artefact behind.
        parser.exit(
            1,
            "error: the pillar gate kept zero products from "
            f"{len(gated)} curated candidate(s) — nothing was written. Inspect "
            "the gate's verdicts above (a run of `discard` means the scrape's "
            "products are outside the five pillars; `unclassified` means the "
            "Jev calls failed).\n",
        )

    payload = {
        "products": [p.model_dump() for p in products],
        "scraped_keywords": len(keywords),
        "scraped_rows": len(rows),
        "curated_products": len(gated),
        "pillar_gate": {
            "classified": sum(1 for row in gated if row.kept),
            "discarded": len(dropped),
            "by_pillar": dict(by_pillar),
        },
        "generated_by": settings.LLM_MODEL,
        # HasData is the pipeline's only shopping source; the field names the
        # source so a reader can tell which free tier a deliverable was
        # bought with.
        "source": "hasdata",
        "actor": "hasdata-google-shopping",
        "run": (scraper.last_run if scraper else {"from_raw": str(args.from_raw)}),
        "note": "Step 3 gold-standard winners; reference set for Steps 4+6; "
                "not tracked",
    }
    write_text_atomic(
        args.output, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    write_markdown(markdown_path, products)
    print(f"wrote {args.output} and {markdown_path}")


if __name__ == "__main__":
    main()
