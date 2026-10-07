#!/usr/bin/env python
"""Step 3 runner: scrape Google Shopping AU for the Step-2 keywords and
curate the GOLD-STANDARD product list.

The scrape source is selectable (`--source`) and both sources are drop-in
peers handing the SAME `ShoppingRow` list to the same curator:

* `apify` (default) — ONE batched Apify actor run (pay-per-result) carrying
  every keyword. Spend is per-result, so `--limit` is the pilot lever and a
  hard USD ceiling (`APIFY_GS_MAX_CHARGE_USD`) rides the run options.
* `hasdata` — one HasData Google Shopping request per keyword (10 credits
  each, the published rate), for operators spending HasData's free tier
  rather than Apify credit. One request returns the whole shopping grid for
  the query (~65 rows observed live 2026-10-07), so `--num` is a LOCAL slice
  of that grid — it bounds curation cost, never API spend.

Then the reasoning LLM curates the rows (`src.evaluators.gold_curator`) and
code assembles the deliverable from the verbatim scraped rows — the LLM
selects by url, it never authors a product fact. Both outputs default under
`outputs/` (git-ignored — production deliverables of the updated pipeline,
never repo artefacts).

A run spends real scrape credit AND real LLM tokens. The planned envelope
(keywords x ~40 observed rows/keyword, with the source's own cost unit) is
printed BEFORE the first call. Use `--limit` to pilot: on the Apify path the
actor returns a full ~40-row SERP page per keyword regardless of `num`
(observed live 2026-09-28), so `--limit 2` is ~80 rows (~$0.28); on the
HasData path `--limit 2` is exactly 2 requests (20 credits), because the
request count IS the keyword count. Curated gold products must carry on-page
demand evidence — rows with no rating/review KPI are filtered out before the
LLM.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/run_gold_standard_research.py \
        [--limit 2 --dump-raw /tmp/step3_raw_rows.json]

    # Same step, HasData credits instead of Apify credit
    source .venv/bin/activate && python scripts/run_gold_standard_research.py \
        --source hasdata --limit 2 --dump-raw /tmp/step3_raw_rows.json

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
from src.extractors.google_shopping import (  # noqa: E402
    GoogleShoppingError,
    GoogleShoppingScraper,
    ShoppingRow,
)
from src.extractors.google_shopping_hasdata import (  # noqa: E402
    HasDataShoppingError,
    HasDataShoppingScraper,
)

REPO = Path(__file__).resolve().parents[1]

#: Observed actor pricing (README, 2026-09-28) — for the estimate line only.
EST_USD_PER_RESULT = 0.0035

#: Rows the Apify actor returns per keyword regardless of `num` (observed
#: 2026-09-28) — the honest multiplier for the Apify spend envelope.
OBSERVED_ROWS_PER_KEYWORD = 40

#: The scrape sources this runner can drive, in the order the help text and
#: the banner describe them. `apify` stays the default so an existing
#: invocation is unchanged by this module gaining a second source.
SOURCES = ("apify", "hasdata")


def build_scraper(source: str, results_per_keyword: int):
    """Construct the selected scrape source (never builds both)."""
    if source == "hasdata":
        return HasDataShoppingScraper(results_per_keyword=results_per_keyword)
    return GoogleShoppingScraper(results_per_keyword=results_per_keyword)


def source_label(source: str) -> str:
    """How the run banner and the digest name the active source."""
    if source == "hasdata":
        return "hasdata-google-shopping"
    return settings.APIFY_GS_ACTOR


def print_spend_plan(source: str, keywords: list[str], results_per_keyword: int) -> None:
    """The honest pre-spend envelope, in the active source's own cost unit.

    The two sources bill differently — Apify per RESULT (and the actor
    over-delivers ~40 rows/keyword whatever `num` says), HasData per REQUEST
    (one grid per keyword, ~65 rows, whatever the local cap) — so a single
    shared cost model would misstate one of them.
    """
    if source == "hasdata":
        requests = len(keywords)
        credits = requests * HasDataShoppingScraper.CREDITS_PER_REQUEST
        print(
            f"planned HasData spend: {requests} keyword(s) x 1 request "
            f"({HasDataShoppingScraper.CREDITS_PER_REQUEST} credits) = "
            f"{credits} credit(s), at the published Google Shopping rate. "
            f"One request returns the whole grid per query (~65 rows "
            f"observed); the local row cap of {results_per_keyword} bounds "
            f"LLM curation only, never HasData spend."
        )
        return
    planned = len(keywords) * OBSERVED_ROWS_PER_KEYWORD
    print(
        f"planned Apify spend: {len(keywords)} keyword(s) x "
        f"~{OBSERVED_ROWS_PER_KEYWORD} rows/page (observed; num floor is 10) "
        f"= up to ~{planned} result(s), ~${planned * EST_USD_PER_RESULT:.2f} "
        f"at ${EST_USD_PER_RESULT:.4f}/result"
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
    path: Path, products: list[GoldStandardProduct], source: str
) -> None:
    """Human digest: gold-standard products grouped by pillar."""
    per_pillar = Counter(p.pillar for p in products)
    lines = [
        "# Step 3 — GOLD-STANDARD winning products (Google Shopping AU)",
        "",
        f"{len(products)} products curated from live Google Shopping AU rows "
        f"(source `{source_label(source)}`) over the Step-2 keyword file, "
        f"annotated by `{settings.LLM_MODEL}`. **Not tracked.** "
        "**This file is the reference set Steps 4 and 6 measure against.**",
        "",
        f"Pillars: {dict(per_pillar)}",
        "",
    ]
    for pillar in ("curated_home", "self_care_rituals", "other"):
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
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 3: Google Shopping gold-standard product research.",
    )
    parser.add_argument(
        "--keywords",
        type=Path,
        default=REPO / "outputs" / "json" / "step-2-search-keywords.json",
        help="Step-2 deliverable to seed from",
    )
    parser.add_argument(
        "--source",
        choices=SOURCES,
        default="apify",
        help=(
            "scrape source: `apify` (default; pay-per-result actor) or "
            "`hasdata` (10 credits per keyword, HasData free tier)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only the first N keywords (pilot runs; keeps scrape spend tiny)",
    )
    parser.add_argument(
        "--num",
        type=int,
        default=None,
        help=(
            "results per keyword — Apify: the actor's closed-set `num` "
            f"(default {settings.APIFY_GS_MAX_RESULTS_PER_KEYWORD}); "
            "HasData: a LOCAL cap on the returned grid, no API effect "
            f"(default {settings.HASDATA_GS_MAX_RESULTS_PER_KEYWORD})"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(settings.GOLD_PRODUCTS_PATH),
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
             "--dump-raw file (zero-credit curation replays/debugging); the "
             "dump is source-agnostic, so a HasData dump replays too",
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
        results_per_keyword = args.num or (
            settings.HASDATA_GS_MAX_RESULTS_PER_KEYWORD
            if args.source == "hasdata"
            else settings.APIFY_GS_MAX_RESULTS_PER_KEYWORD
        )
        print_spend_plan(args.source, keywords, results_per_keyword)

    markdown_path = args.markdown or digest_path(args.output)
    try:
        scraper = None
        if args.from_raw:
            # Curate a previous scrape's dump — no scrape source is built.
            raw = json.loads(args.from_raw.read_text(encoding="utf-8"))
            rows = [ShoppingRow(**entry) for entry in raw]
            # The Step-2 file is irrelevant on this path; report the
            # keywords the rows themselves claim instead.
            keywords = sorted({row.source_keyword for row in rows})
        else:
            scraper = build_scraper(args.source, results_per_keyword)
            rows = scraper.scrape_keywords(keywords)
            if args.dump_raw:
                args.dump_raw.parent.mkdir(parents=True, exist_ok=True)
                args.dump_raw.write_text(
                    json.dumps([vars(row) for row in rows], indent=1, ensure_ascii=False)
                )
        curator = GoldProductCurator()
        products = curator.curate(rows)
    except (
        GoogleShoppingError,
        HasDataShoppingError,
        GoldCurationError,
    ) as exc:
        parser.exit(1, f"error: {exc}\n")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "products": [p.model_dump() for p in products],
        "scraped_keywords": len(keywords),
        "scraped_rows": len(rows),
        "generated_by": settings.LLM_MODEL,
        # The scrape source, recorded so a reader can tell which free tier a
        # deliverable was bought with. `actor` keeps its meaning for the
        # Apify path and names the source for the HasData one.
        "source": args.source,
        "actor": source_label(args.source),
        "run": (scraper.last_run if scraper else {"from_raw": str(args.from_raw)}),
        "note": "Step 3 gold-standard winners; reference set for Steps 4+6; "
                "not tracked",
    }
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_markdown(markdown_path, products, args.source)

    print(f"\nscraped {len(rows)} row(s) -> curated {len(products)} gold product(s)")
    if scraper and scraper.last_run:
        run = scraper.last_run
        if "credits" in run:
            cost_note = f"~{run['credits']} HasData credit(s)"
        else:
            usage = run.get("usage_total_usd")
            cost_note = (
                f"actor usage ${usage}" if usage is not None
                else "actor usage not reported"
            )
        print(f"run {run.get('run_id') or run.get('source')}: {cost_note}")
    print(f"wrote {args.output} and {markdown_path}")
    if not products:
        parser.exit(1, "error: curation produced zero products; nothing written that Steps 4+6 could use\n")


if __name__ == "__main__":
    main()