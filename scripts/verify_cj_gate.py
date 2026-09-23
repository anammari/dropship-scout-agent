#!/usr/bin/env python
"""Inspect the CJ commercial gate's threshold distribution for keywords.

A read-only diagnostic for `MIN_CJ_LISTED_COUNT` (`CLAUDE.md` §6.2). For each
keyword it prints every search hit's dropshipper listing count, shows which
hits a real run would keep, and reports how many would survive at a range of
candidate floors — so a threshold can be tuned against real catalogue data
before it gates a live run.

Zero cost by construction: no `get_product_detail` expansion, no LLM call, no
image download, no package exported. It reads the same search tool a real run
reads, with the same warehouse and inventory filters and the same
`CJ_MAX_PRODUCTS` cap, so the distribution shown is the one the pipeline
actually sees.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/verify_cj_gate.py \\
        "coffee accessories" "kitchen gadgets"

With no keywords it samples a couple of generic ones. `--limit` overrides
`CJ_MAX_PRODUCTS` for the probe only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# Allow `python scripts/verify_cj_gate.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.pipeline.cj_mcp_client import (  # noqa: E402
    DEFAULT_WAREHOUSE_COUNTRY,
    CjMcpClient,
    CjMcpNotConfiguredError,
    extract_listed_count,
    extract_pid,
    extract_title,
)

logger = logging.getLogger("verify_cj_gate")

DEFAULT_KEYWORDS = ["garlic grater", "kitchen gadgets"]
# Floors worth reporting on every keyword: the configured one is highlighted,
# the rest bracket it so a tune up or down is an informed choice.
CANDIDATE_FLOORS = (50, 100, 150, 300, 500)


async def probe(client: CjMcpClient, keyword: str, limit: int) -> List[dict]:
    """The search hits for one keyword, exactly as a real run would see them."""
    hits = await client.search_products(
        keyword,
        limit=limit,
        warehouse_country=DEFAULT_WAREHOUSE_COUNTRY,
        global_warehouse=True,
        inventory_available=True,
    )
    return [hit for hit in hits if isinstance(hit, dict)]


def report(keyword: str, hits: List[dict], floor: int) -> None:
    """Print one keyword's listing-count distribution and floor outcomes."""
    counts = [(extract_listed_count(hit), hit) for hit in hits]
    known = [count for count, _ in counts if count is not None]
    unproven = len(counts) - len(known)

    print(f"\n=== {keyword!r} — {len(hits)} hit(s), floor {floor} ===")
    if not hits:
        print("  no hits returned (try another keyword)")
        return

    # Strongest first, matching how the gate ranks what it keeps.
    for count, hit in sorted(
        counts, key=lambda pair: pair[0] if pair[0] is not None else -1,
        reverse=True,
    ):
        pid = extract_pid(hit) or "<no pid>"
        title = extract_title(hit)[:44]
        if count is None:
            verdict = "DROP (unavailable)"
        elif count < floor:
            verdict = "DROP"
        else:
            verdict = "keep"
        shown = "n/a" if count is None else str(count)
        print(f"  {shown:>7}  {verdict:<18} {pid:<38} {title!r}")

    if known:
        print(
            f"\n  observed min={min(known)} max={max(known)} "
            f"median={sorted(known)[len(known) // 2]}"
        )
    if unproven:
        print(f"  {unproven} hit(s) reported no count — dropped as unproven")

    print("\n  survivors by candidate floor:")
    # The configured floor is always reported, wherever it sits — otherwise a
    # threshold outside the bracketing set (say 400) would be the one value
    # the operator most wants to see and the one the table omits.
    for candidate in sorted({*CANDIDATE_FLOORS, floor}):
        kept = sum(1 for count in known if count >= candidate)
        marker = "  <== configured" if candidate == floor else ""
        print(
            f"    MIN_CJ_LISTED_COUNT={candidate:<4} keeps "
            f"{kept}/{len(hits)} hit(s){marker}"
        )


async def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report the CJ listing-count distribution for keywords without "
            "costing a detail call, an LLM call, or an export."
        )
    )
    parser.add_argument(
        "keywords",
        nargs="*",
        default=None,
        help=f"keywords to probe (default: {', '.join(DEFAULT_KEYWORDS)})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "hits to probe per keyword (default: CJ_MAX_PRODUCTS = "
            f"{settings.CJ_MAX_PRODUCTS_PER_KEYWORD})"
        ),
    )
    args = parser.parse_args(argv)

    keywords = args.keywords or DEFAULT_KEYWORDS
    limit = args.limit or settings.CJ_MAX_PRODUCTS_PER_KEYWORD
    floor = settings.MIN_CJ_LISTED_COUNT

    client = CjMcpClient()
    if not client.configured:
        print(
            "CJ_MCP_TOKEN is not configured — set it in .env to probe the "
            "live catalogue.",
            file=sys.stderr,
        )
        return 1

    print(f"endpoint: {client.redacted_endpoint}")
    print(f"MIN_CJ_LISTED_COUNT = {floor}   limit = {limit} hit(s)/keyword")
    print("No detail calls, no LLM, no export — search only.")

    async with client:
        for keyword in keywords:
            report(keyword, await probe(client, keyword, limit), floor)

    print("\nTune with MIN_CJ_LISTED_COUNT in .env (blank = the default).")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        raise SystemExit(asyncio.run(main()))
    except CjMcpNotConfiguredError as exc:
        print(f"CJ MCP not configured: {exc}", file=sys.stderr)
        raise SystemExit(1)
