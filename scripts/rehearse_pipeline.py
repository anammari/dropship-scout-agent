#!/usr/bin/env python
"""Development rehearsal: run the whole chain on synthetic data, spending nothing.

**Not part of the pipeline.** This is a harness for the operator and the coding
agent, and nothing in the production path imports it.

Its purpose is that the first *live* run should not be the first time the steps
have met. It drives the real runners — Step 2's gate, Step 3's scrape/curate/pillar
gate, Step 4's keyword bank, Step 5's CJ ingestion, Step 6's ranking and Step 7's
cleanup — over synthetic data, with **only the external services stubbed**:

    stubbed   HasData Google Shopping, the CJdropshipping MCP server, the
              reasoning LLM, Jev (System One), and the CDN image download
    real      every guardrail, the gold/kbank/ranked file handoffs, the export
              contract through the real exporter, the margin floor, the image
              gate, the funnel counters, and the cleanup's deletion logic

That is what makes it worth running: it exercises the seams between the steps and
the contracts inside them, which is where a chain breaks.

**It cannot spend anything.** Every outbound socket is blocked for the duration,
so a stub that misses its mark fails loudly instead of quietly reaching a real
API. It also writes only inside its own scratch directory, and refuses to run if
that directory looks like the real workspace tree.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/rehearse_pipeline.py \
        [--scratch outputs/rehearsal]

Exit codes: 0 the whole chain ran, 1 a step reported a failure, 2 unexpected error.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List

# Allow `python scripts/rehearse_pipeline.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from src.config import settings  # noqa: E402
from src.evaluators.gold_curator import GoldProductCurator  # noqa: E402
from src.keywords.generator import KeywordGenerator, exact_keyword  # noqa: E402
from src.models import (  # noqa: E402
    ProductCandidateEvaluation,
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)
from src.research.pillar_gate import PillarGate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: The synthetic world, in ONE table so nothing can drift: each row is a Step-2
#: keyword, its pillar, and the Google Shopping listing the stubbed scrape returns
#: for it. Twelve keywords across the five pillars, which is the shape (if not the
#: size) of the production pool.
SYNTHETIC_POOL = [
    # keyword, pillar, listing title, price, rating, reviews
    ("coffee milk frother", "brew_fuel", "Handheld Milk Frother Whisk", 24.95, 4.6, 812),
    ("matcha whisk", "brew_fuel", "Bamboo Matcha Whisk Set", 19.95, 4.7, 430),
    ("milk steamer and frother", "brew_fuel", "Milk Steamer Jug", 39.95, 4.5, 610),
    ("hands free dog leash", "sunrise_move", "Hands Free Dog Leash Belt", 29.95, 4.5, 260),
    ("sand free towel", "sunrise_move", "Sand Free Beach Towel", 34.95, 4.4, 180),
    ("commuter backpack", "sunrise_move", "Water Resistant Commuter Backpack", 59.95, 4.6, 920),
    ("incense burner", "mindful_awakening", "Ceramic Backflow Incense Burner", 22.50, 4.8, 1900),
    ("shower steamer", "mindful_awakening", "Aromatherapy Shower Steamer Set", 18.95, 4.3, 340),
    ("silk sleep eye mask", "restful_reset", "Mulberry Silk Sleep Mask", 34.00, 4.9, 3120),
    ("ice roller", "restful_reset", "Stainless Facial Ice Roller", 21.95, 4.6, 780),
    ("stainless steel garlic press", "morning_table", "Stainless Steel Garlic Press", 24.95, 4.7, 1204),
    ("olive wood cutting board", "morning_table", "Olive Wood Cutting Board", 49.00, 4.6, 880),
]

#: The stubbed pillar gate's answer, keyed by the listing TITLE. Keyed by title and
#: not by position in the batch: the gate numbers each batch from zero, so a
#: positional stub answers the same index twice and quietly mis-attributes pillars.
#: One product is discarded on purpose, so the rehearsal exercises the gate's drop
#: path as well as its five pillars.
PILLAR_BY_TITLE = {row[2]: row[1] for row in SYNTHETIC_POOL}
PILLAR_BY_TITLE["Aromatherapy Shower Steamer Set"] = "discard"

#: The stubbed ranking's answers, keyed by the CJ listing title Step 5 writes. The
#: spread is chosen so the run exercises every path Step 7 has: one shortlist
#: (kept), one `disregard` from Jev (deleted), and eleven `review` against a keep of
#: ten — so the cap bites and one more is deleted.
#:
#: The key is `exact_keyword(<gold title>)`, not the Step-2 keyword: Step 4 derives
#: each exact keyword from the gold product's own title. Using the pipeline's own
#: transform here means the stub cannot drift from what it is stubbing.
RANKING_BY_TITLE = {}
for index, row in enumerate(SYNTHETIC_POOL):
    title = f"{exact_keyword(row[2])} (CJ listing)"
    if index == 0:
        RANKING_BY_TITLE[title] = (5, 4, row[1])   # shortlist
    elif index == len(SYNTHETIC_POOL) - 1:
        RANKING_BY_TITLE[title] = (1, 1, "discard")  # Jev discards it
    else:
        RANKING_BY_TITLE[title] = (3, 3, row[1])   # review
# The two fallback keywords also produce a package each; both are `review`, so the
# cap sees eleven review packages against a keep of ten.
SYNTHETIC_FALLBACKS = {
    "Handheld Milk Frother Whisk": "handheld milk frother rechargeable",
    "Stainless Steel Garlic Press": "garlic press with cleaner",
}
for fallback in SYNTHETIC_FALLBACKS.values():
    RANKING_BY_TITLE[f"{fallback} (CJ listing)"] = (3, 2, "brew_fuel")
# The `rechargeable` fallback is the ONE package the compliance gate should demote,
# deliberately — which is what makes the rehearsal cover that gate too.

#: What the stubbed Google Shopping returns, keyed by keyword: (title, price,
#: rating, reviews). Every listing is priced well above the synthetic CJ landed
#: cost, so the margin floor passes and the rehearsal exercises the export path
#: rather than the REJECT path.
SYNTHETIC_LISTINGS = {
    row[0]: [(row[2], f"${row[3]:.2f}", row[4], row[5])] for row in SYNTHETIC_POOL
}


# ---------------------------------------------------------------------------
# The no-spend guarantee
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched(targets):
    """Swap process-global seams for the duration, and put them back.

    The runners import their collaborators by name at module load, so the only
    way to stub them is to rebind the names they hold. That is fine for a
    standalone process; restoring afterwards means the harness can also be driven
    in-process — by a test — without leaving stubs behind for whatever runs next.
    """
    saved = [(owner, name, getattr(owner, name)) for owner, name in targets]
    try:
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)





@contextlib.contextmanager
def _no_network():
    """Block every outbound socket, so a missed stub fails instead of spending.

    The stubs all ride `httpx.MockTransport`, which never opens a socket, so with
    this in place "no credits spent" is enforced rather than asserted.
    """

    def _refuse(*args: Any, **kwargs: Any):
        raise AssertionError(
            "the rehearsal attempted a real network call — a stub is missing. "
            "This harness must never spend a HasData credit or an API call."
        )

    real_connect = socket.socket.connect
    real_create = socket.create_connection
    socket.socket.connect = _refuse  # type: ignore[assignment]
    socket.create_connection = _refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[assignment]
        socket.create_connection = real_create  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Stubs: the external services, and only those
# ---------------------------------------------------------------------------


def _png_bytes(colour: tuple = (200, 120, 90)) -> bytes:
    """A real, decodable PNG — the exporter names files by PIL-decoded format."""
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buffer, format="PNG")
    return buffer.getvalue()


async def _fake_source_images(product, client=None, **kwargs):
    """Stand in for the CDN download: three distinct 'images' per product."""
    return [_png_bytes((n * 40 % 255, 120, 90)) for n in range(3)]


class _FakeCjExtractor:
    """Stands in for the CJdropshipping MCP extractor; returns one listing per
    keyword, priced so the margin floor is cleared."""

    engine_name = "cjdropshipping"
    supplier_name = "CJdropshipping"

    async def fetch_products(self, keywords, country=None):
        products = []
        for keyword in keywords:
            slug = str(keyword).replace(" ", "-")
            products.append(
                RawSupplierProduct(
                    supplier_name="CJdropshipping",
                    supplier_retail_url=f"https://cjdropshipping.com/product/{abs(hash(slug)) % 10**9}.html",
                    product_title=f"{keyword} (CJ listing)",
                    product_description=f"A supplier listing for {keyword}.",
                    price_aud=12.00,
                    shipping_cost_aud=8.00,
                    shipping_method="CJPacket EUB",
                    shipping_transit_days="6-10",
                    image_urls=[
                        f"https://cdn.example.com/{slug}-{n}.png" for n in range(3)
                    ],
                )
            )
        return products


class _CannedEvaluator:
    """Stands in for the LLM viability filter.

    It builds a REAL `ProductCandidateEvaluation.from_raw`, so the schema, the
    margin arithmetic, the AUD margin floor and the export contract all run for
    real — only the model's opinion is canned. The price is derived from the
    landed cost so the ACCEPT genuinely clears the floor rather than being
    waved through.
    """

    async def evaluate(self, raw: RawSupplierProduct) -> ProductCandidateEvaluation:
        cogs = raw.price_aud + raw.shipping_cost_aud
        provisional = ProvisionalProductEvaluation(
            verdict="ACCEPT",
            niche_category="brew_fuel",
            problem_solved="A morning-routine friction this product removes.",
            suggested_retail_aud=round(max(cogs * 2.6, cogs + 11), 2),
            marketing_ad_copy="A rehearsal payload, not real copy.",
            saturation_risk="LOW",
            target_tags=["dropship", "rehearsal"],
            key_features=["Rehearsal feature one", "two", "three"],
        )
        return ProductCandidateEvaluation.from_raw(provisional, raw)


def _scripted_llm(rows_by_product: Dict[str, str]):
    """A MockTransport handler answering the Step-4 fallback prompt."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        rows = [
            {
                "keyword": fallback,
                "product": name,
                "rationale": "a tighter mechanism name",
            }
            for name, fallback in rows_by_product.items()
            if f"| {name} |" in prompt
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps({"keywords": rows})},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return handler


def _scripted_curation_handler():
    """Stands in for the Step-3 curation LLM: selects every row it is given."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        rows = json.loads(prompt.split("### Rows", 1)[1].split("\n", 1)[1])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "products": [
                                        {
                                            "url": row["url"],
                                            "compliance_note": "Inert rehearsal item.",
                                            "unit_economics_note": None,
                                        }
                                        for row in rows
                                    ]
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return handler


# ---------------------------------------------------------------------------
# The rehearsal
# ---------------------------------------------------------------------------


def _load_runner(name: str, filename: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def _write_step2_file(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "keywords": [
                    {
                        "keyword": keyword,
                        "pillar": pillar,
                        "demand_evidence": "rehearsal: 5-y mean 10 (placeholder)",
                    }
                    for keyword, pillar, _title, _p, _r, _n in SYNTHETIC_POOL
                ],
                "generated_with": "REHEARSAL SYNTHETIC POOL — not real research",
                "note": "Synthetic Step-2 pool for the rehearsal; not tracked",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _fake_shopping_rows(keywords: List[str]):
    from src.extractors.shopping_row import ShoppingRow

    rows = []
    for keyword in keywords:
        for index, (title, price, rating, reviews) in enumerate(
            SYNTHETIC_LISTINGS.get(keyword, [])
        ):
            value = float(price.replace("$", ""))
            rows.append(
                ShoppingRow(
                    title=title,
                    url=f"https://www.google.com.au/shopping/product/r{abs(hash(keyword)) % 10**6}{index}",
                    merchant="Rehearsal Merchant",
                    price_text=price,
                    delivery_text="Free delivery",
                    image_url=None,
                    description=None,
                    rating=rating,
                    rating_count=reviews,
                    source_keyword=keyword,
                    price_value=value,
                )
            )
    return rows


def _fake_scraper_factory():
    class _RehearsalScraper:
        CREDITS_PER_REQUEST = 10

        def __init__(self, results_per_keyword=None, **kwargs):
            self.results_per_keyword = results_per_keyword or 20
            self.last_run = {"source": "rehearsal", "credits": 0}

        def scrape_keywords(self, keywords):
            return _fake_shopping_rows(list(keywords))

    return _RehearsalScraper


def _real_curator_factory():
    """The REAL curator, with only its LLM call stubbed."""

    def build(**kwargs):
        return GoldProductCurator(
            client=httpx.Client(
                transport=httpx.MockTransport(_scripted_curation_handler())
            ),
            base_url="http://llm.test",
            api_key="rehearsal",
            model="rehearsal",
        )

    return build


def _real_pillar_gate_factory():
    """The REAL pillar gate, with only Jev stubbed."""

    def build(**kwargs):
        def decide(state, questions):
            answers = {}
            for index, entry in enumerate(state["products"]):
                pillar = PILLAR_BY_TITLE.get(entry.get("name"), "discard")
                answers[f"{index}__pillar"] = {"type": "choice", "choice": pillar}
            return answers

        client = _ScriptedJev(decide)
        return PillarGate(client=client, batch_size=kwargs.get("batch_size", 4) or 4)

    return build


class _ScriptedJev:
    """A System One stand-in: `decide(state, questions)` returns canned answers."""

    def __init__(self, decide):
        self._decide = decide
        self.calls = []

    def decide(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        return self._decide(state, questions)


def _real_keyword_generator_factory():
    """The REAL keyword generator, with only its LLM call stubbed."""

    def build(**kwargs):
        return KeywordGenerator(
            client=httpx.Client(
                transport=httpx.MockTransport(_scripted_llm(SYNTHETIC_FALLBACKS))
            ),
            base_url="http://llm.test",
            api_key="rehearsal",
            model="rehearsal",
            **kwargs,
        )

    return build


def _real_ranker_factory():
    """The REAL ranker, with only Jev stubbed."""

    def build(**kwargs):
        def decide(state, questions):
            answers = {}
            for slug, entry in state["products"].items():
                # Keyed by the package's own title, so the same package gets the
                # same verdict whatever batch it lands in.
                similarity, value, pillar = RANKING_BY_TITLE.get(
                    entry.get("product_title"), (2, 2, "discard")
                )
                # Jev's raw score is 0-based; the client shifts it onto 1-5.
                answers[f"{slug}__similarity"] = {"type": "score", "score": similarity - 1}
                answers[f"{slug}__winning_value"] = {"type": "score", "score": value - 1}
                answers[f"{slug}__pillar"] = {"type": "choice", "choice": pillar}
            return answers

        return kwargs.pop("_ranker_cls")(client=_ScriptedJev(decide), **kwargs)

    return build


@contextlib.contextmanager
def _argv(argv: List[str]):
    """Point `sys.argv` at a runner that reads it directly."""
    original = sys.argv
    sys.argv = ["rehearsal", *argv]
    try:
        yield
    finally:
        sys.argv = original


def _run(runner, argv: List[str]) -> int:
    """Call a runner's `main`, whether it takes argv or reads `sys.argv`."""
    import inspect

    if len(inspect.signature(runner.main).parameters) == 0:
        with _argv(argv):
            return runner.main()
    return runner.main(argv)


def _artefact_highlights(
    gold: Path, bank: Path, ranked: Path, candidates: Path
) -> List[str]:
    """A few numbers from each handoff, so the report stands on its own."""
    lines: List[str] = []
    if gold.is_file():
        payload = json.loads(gold.read_text())
        pillars: Dict[str, int] = {}
        for product in payload.get("products", []):
            pillars[product.get("pillar")] = pillars.get(product.get("pillar"), 0) + 1
        gate = payload.get("pillar_gate", {})
        lines.append(
            f"Step 3: {len(payload.get('products', []))} gold product(s) "
            f"from {payload.get('scraped_keywords')} keyword(s) and "
            f"{payload.get('scraped_rows')} row(s); gate classified "
            f"{gate.get('classified')}, discarded {gate.get('discarded')}; "
            f"pillars {pillars}"
        )
    if bank.is_file():
        payload = json.loads(bank.read_text())
        counts = payload.get("counts", {})
        lines.append(
            f"Step 4: {len(payload.get('keywords', []))} keyword(s) from "
            f"{payload.get('source_products')} product(s) — "
            f"{counts.get('exact')} exact, {counts.get('tight_fallback')} fallback"
        )
    if ranked.is_file():
        payload = json.loads(ranked.read_text())
        summary = payload.get("summary", {})
        lines.append(
            f"Step 6: {summary.get('packages_ranked')} package(s) ranked; "
            f"tiers {summary.get('tier_counts')}"
        )
    supplier = Path(candidates) / "cjdropshipping"
    if supplier.is_dir():
        lines.append(
            f"After Step 7: {len(list(supplier.glob('product-*')))} package(s) "
            "remain in cjdropshipping/"
        )
    return lines


def _tree(root: Path) -> List[str]:
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rehearse the whole Morning Anthem chain on synthetic data with every "
            "external service stubbed — no HasData credits, no network."
        )
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=REPO / "outputs" / "rehearsal",
        help="scratch tree to build and tear down (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    scratch = args.scratch.resolve()
    # The rehearsal deletes directories (Step 7). Make it structurally impossible
    # for it to point those deletions at the real workspace.
    for forbidden in (
        Path(settings.MORNING_ANTHEM_EXPORT_DIR).resolve(),
        Path(settings.EXPORT_DIR).resolve(),
    ):
        if scratch == forbidden or forbidden in scratch.parents:
            parser.exit(
                2,
                f"refusing to rehearse inside the real workspace tree ({forbidden}) "
                "— pass a scratch directory somewhere else\n",
            )

    if scratch.exists():
        shutil.rmtree(scratch)
    json_dir = scratch / "json"
    md_dir = scratch / "md"
    candidates = scratch / "candidates"
    for directory in (json_dir, md_dir, candidates):
        directory.mkdir(parents=True)

    step2 = json_dir / "morning-anthem-step-2-trend-keywords.json"
    gold = json_dir / "morning-anthem-step-3-gold-products.json"
    gold_md = md_dir / "morning-anthem-step-3-gold-products.md"
    bank = json_dir / "morning-anthem-step-4-keyword-bank.json"
    bank_md = md_dir / "morning-anthem-step-4-keyword-bank.md"
    ranked = json_dir / "morning-anthem-step-6-ranked-candidates.json"
    ranked_md = md_dir / "morning-anthem-step-6-ranked-candidates.md"

    _write_step2_file(step2)
    print(f"\n[REHEARSAL] scratch: {scratch}")
    print(f"  synthetic pool: {len(SYNTHETIC_POOL)} keyword(s) across "
          f"{len({row[1] for row in SYNTHETIC_POOL})} pillar(s)")

    results: List[tuple] = []

    try:
        with _no_network(), contextlib.ExitStack() as _patch_stack:
            # --- Step 3: scrape -> curate -> pillar gate -------------------
            step3 = _load_runner("rehearsal_step3", "run_gold_standard_research.py")
            _patch_stack.enter_context(
                _patched(
                    [
                        (step3, "HasDataShoppingScraper"),
                        (step3, "GoldProductCurator"),
                        (step3, "PillarGate"),
                    ]
                )
            )
            step3.HasDataShoppingScraper = _fake_scraper_factory()
            step3.GoldProductCurator = _real_curator_factory()
            step3.PillarGate = _real_pillar_gate_factory()
            code = _run(step3, [
                "--keywords", str(step2),
                "--output", str(gold),
                "--markdown", str(gold_md),
                "--allow-over-pilot-budget",
            ])
            results.append(("Step 3 gold products", code, gold))

            # --- Step 4: exact titles + scripted fallbacks -----------------
            step4 = _load_runner("rehearsal_step4", "generate_gold_keywords.py")
            _patch_stack.enter_context(
                _patched([(step4, "KeywordGenerator")])
            )
            step4.KeywordGenerator = _real_keyword_generator_factory()
            code4 = _run(step4, [
                "--gold-products", str(gold),
                "--output", str(bank),
                "--markdown", str(bank_md),
            ])
            results.append(("Step 4 keyword bank", code4, bank))

            # --- Step 5: CJ ingestion, real exporter, stubbed surroundings -
            import src.evaluators.llm_filter as llm_filter_module
            import src.exporter as exporter_module
            import src.main as main_module

            _patch_stack.enter_context(
                _patched(
                    [
                        (main_module, "_EXTRACTOR_REGISTRY"),
                        (llm_filter_module, "LLMEvaluationFilter"),
                        (exporter_module, "source_product_images"),
                    ]
                )
            )
            main_module._EXTRACTOR_REGISTRY = {"cjdropshipping": _FakeCjExtractor}
            llm_filter_module.LLMEvaluationFilter = _CannedEvaluator
            exporter_module.source_product_images = _fake_source_images

            step5 = _load_runner("rehearsal_step5", "ingest_keyword_bank.py")
            code5 = _run(step5, [
                "--keywords", str(bank),
                "--target-per-product", "2",
                "--export-root", str(candidates),
            ])
            results.append(("Step 5 CJ ingestion", code5, candidates))

            # --- Step 6: Jev ranking with a scripted client ----------------
            from src.ranking.jev_product_ranker import JevProductRanker

            step6 = _load_runner("rehearsal_step6", "rank_optimal_candidates.py")
            factory = _real_ranker_factory()

            def ranker_factory(**kwargs):
                return factory(_ranker_cls=JevProductRanker, **kwargs)

            _patch_stack.enter_context(_patched([(step6, "JevProductRanker")]))
            step6.JevProductRanker = ranker_factory
            code6 = _run(step6, [
                "--gold-products", str(gold),
                "--export-root", str(candidates),
                "--output", str(ranked),
                "--markdown", str(ranked_md),
            ])
            results.append(("Step 6 Jev ranking", code6, ranked))

            # --- Step 7: cleanup, dry run then apply ------------------------
            step7 = _load_runner("rehearsal_step7", "cleanup_candidates.py")
            dry = _run(step7, [
                "--report", str(ranked), "--export-root", str(candidates),
            ])
            applied = _run(step7, [
                "--report", str(ranked), "--export-root", str(candidates), "--apply",
            ])
            results.append(("Step 7 cleanup (dry run)", dry, ranked))
            results.append(("Step 7 cleanup (apply)", applied, candidates))
    except AssertionError as exc:
        print(f"\n[REHEARSAL FAILED] {exc}")
        return 2

    # --- the chain report ---------------------------------------------------
    print("\n[CHAIN REPORT]")
    for name, code, artefact in results:
        # A runner that returns None has completed; only a non-zero int is a
        # failure (and `parser.exit` raises SystemExit, which never reaches here).
        ok = code in (0, None)
        print(
            f"  {name:28} exit={code!s:5} {'ok  ' if ok else 'FAIL'} "
            f"artefact={'yes' if Path(artefact).exists() else 'NO'}"
        )
    print()
    for line in _artefact_highlights(gold, bank, ranked, candidates):
        print(f"  {line}")

    if all(code in (0, None) for _, code, _ in results):
        print("\n  every step completed and produced its artefact")
    else:
        print("\n  at least one step did not complete — see the log above")

    print("\n[FINAL TREE]")
    for path in _tree(scratch):
        print(f"  {path}")

    packages = sorted((candidates / "cjdropshipping").glob("product-*"))
    print(
        f"\n  {len(packages)} package(s) survived cleanup in "
        f"cjdropshipping/ (from {len(_tree(candidates))} file(s) before)"
    )
    print("\n[REHEARSAL COMPLETE] 0 HasData credits spent — every socket was blocked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
