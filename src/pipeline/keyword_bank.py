"""Step 5 — the keyword bank and its CJ-only ingestion (plan §7).

The bank is the Step-4 deliverable
(`outputs/json/morning-anthem-step-4-keyword-bank.json`, untracked):
gold-standard supplier search keywords, each typed with the gold product it
came from, its pillar, and its match kind (`exact` verbatim title or
`tight_fallback`). The loader is size-agnostic — it validates structure,
not a bank-size band.

Step 5 ingests EVERY bank keyword through the CJdropshipping MCP pipeline —
The Morning Anthem's sole supplier engine (the AliExpress Dropshipping
Center extractor was retired on this branch) — and exports what survives
into the gold-kernel tree:

    <MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/…

The per-(keyword, engine) leg is its own call into the existing
`run_pipeline` with a single-engine list, a shared evaluator and a
per-engine exporter:

* an engine that is unconfigured/blocked/timeout for a leg is logged and
  skipped **for that leg only**;
* only when **every** registered engine failed **every** keyword does
  ingestion raise, which the runner renders as the intervention block.

Every gate of the supplier core still applies untouched, because the legs run
the real extractor and the real exporter: CJ's commercial gate (§6.2), MCP
payload liveness gate (§5) and mandatory freight quote (§6.3, quoted to
Australia from China at launch); the LLM viability gate, the margin floor,
and the 3-image gallery gate before anything is written.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.config import settings
from src.exporter import CandidateExporter, ExportResult
from src.keywords.generator import carries_banned_token

logger = logging.getLogger(__name__)

# The supplier engine the bank ingests through (CJdropshipping is The Morning
# Anthem's sole supplier), and the subfolder it writes into under
# MORNING_ANTHEM_EXPORT_DIR. The key is the same supplier key
# SUPPLIER_PRIORITY_ORDER and the CLI's --extractor use, so the registry in
# src.main needs no translation.
BANK_ENGINES = ("cjdropshipping",)
OPTIMAL_SUPPLIER_DIRS = {
    "cjdropshipping": "cjdropshipping",
}

# PipelineSummary counters folded into the per-engine funnel. Kept as an
# explicit list so a new counter on PipelineSummary cannot silently vanish
# from the bank summary.
_FUNNEL_COUNTERS = (
    "candidates_scraped",
    "evaluated",
    "accepted",
    "rejected",
    "dropped_llm_validation_failed",
    "dropped_no_valid_images",
    "skipped_duplicate",
    "candidates_exported",
)

# Counters `run_pipeline` copies off the EXPORTER rather than computing per
# call. The bank reuses one exporter across an engine's legs (so duplicate
# detection spans the whole bank, which is the point), and the exporter's
# attributes are cumulative — so a leg's PipelineSummary carries the running
# total, not that leg's delta. The empty-funnel path is worse: `run_pipeline`
# returns before it copies them at all, so that leg reports 0. The bank
# therefore reads the EXPORTER's own counter before and after each leg and
# folds the difference, which sums to the exporter's real total and cannot go
# negative.
_EXPORTER_ACCUMULATED_COUNTERS = ("dropped_no_valid_images", "skipped_duplicate")


class KeywordBankError(Exception):
    """The keyword bank is missing, malformed, or unusable — fail closed."""


class BankIngestionFailedError(KeywordBankError):
    """Every registered engine failed every keyword — nothing could be read."""


@dataclass
class BankKeyword:
    """One row of the Step-4 keyword bank.

    `product` and `pillar` are provenance (they tie the keyword back to the
    gold product it came from and stay in the bank file); `match` records
    whether the keyword is the product's own title, code-derived (`exact`), or a
    tightly-specific LLM-authored variant (`tight_fallback`). Only `keyword`
    reaches a supplier query.
    """

    keyword: str
    product: str
    pillar: str
    match: str = "exact"
    rationale: str = ""


@dataclass
class BankIngestionSummary:
    """What one bank-ingestion run did, per engine and in total."""

    #: The bank's size, for reporting.
    keywords_total: int = 0
    #: Products iterated. The unit of work is the PRODUCT, not the keyword: a
    #: product's exact keyword runs first and its fallback only to fill whatever
    #: target slots the exact one left open.
    products_attempted: int = 0
    #: Products for which NO engine returned a single verified supplier product —
    #: nothing to evaluate, nothing to export.
    products_empty: int = 0
    per_engine: Dict[str, Dict[str, int]] = field(default_factory=dict)
    exports: List[ExportResult] = field(default_factory=list)
    export_roots: Dict[str, str] = field(default_factory=dict)

    @property
    def total_exports(self) -> int:
        return len(self.exports)


def load_keyword_bank(path: Optional[Path] = None) -> List[BankKeyword]:
    """Read the Step-4 deliverable as typed bank keywords.

    Fail closed (plan §7.1): a missing or malformed file, an empty pool, a
    non-string or blank keyword, and a keyword carrying a banned AICIS token
    each raise `KeywordBankError` with remediation text rather than letting a
    bad keyword reach a supplier query. The banned-token check is defence in
    depth — the Step-4 generator's pool validator already enforces it, so a
    hit here means the bank file was hand-edited or generated elsewhere.
    """
    bank_path = Path(path or settings.MORNING_ANTHEM_KEYWORD_BANK_PATH)
    try:
        raw = bank_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeywordBankError(
            f"Keyword bank not found at {bank_path} ({exc}). Generate it "
            "first: source .venv/bin/activate && python "
            "scripts/generate_gold_keywords.py"
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise KeywordBankError(
            f"Keyword bank at {bank_path} is not valid JSON ({exc}). "
            "Re-generate it with scripts/generate_gold_keywords.py — a "
            "hand-edited bank is not a supported input."
        ) from exc

    rows = payload.get("keywords") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise KeywordBankError(
            f"Keyword bank at {bank_path} must be a JSON list of keywords or "
            'an object with a "keywords" list (got '
            f"{type(payload).__name__})."
        )
    if not rows:
        raise KeywordBankError(
            f"Keyword bank at {bank_path} is empty — there is nothing to "
            "ingest. Re-generate it with scripts/generate_gold_keywords.py."
        )

    keywords: List[BankKeyword] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise KeywordBankError(
                f"Keyword bank row {index} at {bank_path} is "
                f"{type(row).__name__}, not an object."
            )
        keyword = row.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            raise KeywordBankError(
                f"Keyword bank row {index} at {bank_path} has no usable "
                f"'keyword' string (got {keyword!r})."
            )
        keyword = keyword.strip()
        banned = carries_banned_token(keyword)
        if banned is not None:
            raise KeywordBankError(
                f"Keyword bank row {index} ({keyword!r}) carries the banned "
                f"AICIS token {banned!r} — the AICIS boundary is enforced on "
                "the bank as well as in the generator. Re-generate the bank: "
                "python scripts/generate_gold_keywords.py"
            )
        match = str(row.get("match") or "exact").strip().lower()
        if match not in MATCH_ORDER:
            match = "exact"
        keywords.append(
            BankKeyword(
                keyword=keyword,
                product=str(row.get("product") or ""),
                pillar=str(row.get("pillar") or ""),
                match=match,
                rationale=str(row.get("rationale") or ""),
            )
        )
    # The DESCENT LADDER, highest-precision rung first: the product's own
    # title, then the model's tight fallbacks, then the code-derived sub-terms,
    # then the pillar's head term. Each rung runs only while the product still
    # has export slots open, so a product that matches on its title never pays
    # for a broader search, and one that the exact title cannot find still gets
    # a chance at every level down to the category word.
    keywords.sort(key=lambda kw: MATCH_ORDER.index(kw.match))
    return keywords


#: The ladder's rungs, highest precision first. `keyword_bank` sorts by this
#: order, so a product's rows always run title -> fallback -> sub-term -> head.
MATCH_ORDER = ("exact", "tight_fallback", "sub_term", "head_term")


def _by_product(keywords: Sequence[BankKeyword]) -> List[tuple]:
    """Group the bank rows by product, preserving order within each group.

    The unit of ingestion is the PRODUCT: its exact keyword runs first and its
    fallback only fills whatever target slots the exact one left open, so the
    fallback is never paid for when the exact title already worked. `product` is
    the join key the generator wrote verbatim, and the loader has already sorted
    exact rows ahead of fallbacks, so each group arrives exact-first.
    """
    groups: Dict[str, List[BankKeyword]] = {}
    for keyword in keywords:
        groups.setdefault(keyword.product, []).append(keyword)
    return list(groups.items())


def _resolve_engines(factories: Dict[str, Callable[[], Any]]) -> List[str]:
    """Registered bank engines, in SUPPLIER_PRIORITY_ORDER then default order.

    Deriving the order from settings keeps the bank consistent with the
    operator's configured chain; an injected factory the order does not name
    is still run (appended in BANK_ENGINES order) so a test or a pilot
    `--only` leg cannot be silently dropped.
    """
    ordered = [key for key in settings.SUPPLIER_PRIORITY_ORDER if key in factories]
    for key in BANK_ENGINES:
        if key in factories and key not in ordered:
            ordered.append(key)
    return ordered


def _default_extractor_factories() -> Dict[str, Callable[[], Any]]:
    from src.main import _EXTRACTOR_REGISTRY  # local: avoids import cycles

    return {
        key: registry_cls
        for key, registry_cls in _EXTRACTOR_REGISTRY.items()
        if key in BANK_ENGINES
    }


def _default_exporters(export_root: Path, engines: Sequence[str]) -> Dict[str, Any]:
    """One exporter per engine, each bound to its own supplier subfolder.

    Numbering and the already-exported duplicate check are per exporter. The
    subfolder is created on the first write, so a run that exports nothing
    leaves no empty directory behind.
    """
    return {
        engine: CandidateExporter(
            export_dir=str(export_root / OPTIMAL_SUPPLIER_DIRS[engine])
        )
        for engine in engines
    }


async def ingest_keyword_bank(
    keywords: Optional[List[BankKeyword]] = None,
    target_per_product: Optional[int] = None,
    country: Optional[str] = None,
    evaluator: Optional[Any] = None,
    exporters: Optional[Dict[str, Any]] = None,
    extractor_factories: Optional[Dict[str, Callable[[], Any]]] = None,
    export_root: Optional[Path] = None,
) -> BankIngestionSummary:
    """Ingest every bank keyword through every registered supplier engine.

    Per (keyword, engine) leg this calls the existing `run_pipeline` with a
    single-engine list: same extractor, same gates, same exporter contract as
    the general intake — nothing about the supplier core is forked here.

    `evaluator` is one shared instance across every leg (the LLM filter is
    stateless per call, and building it once means a bad LLM `.env` fails
    fast on the first leg rather than after supplier quota is spent).

    Raises `BankIngestionFailedError` when every registered engine failed
    every product, and lets `LLMConfigError` from the leg propagate — both
    are boundary conditions the runner renders as interventions rather than
    per-product drops.

    **The product is the unit.** Its exact-title keyword runs first and its
    fallback only fills whatever target slots the exact keyword left open, so a
    fallback is never paid for when the exact title already worked. Each engine
    gets its own pass at a product's keywords, since they write to separate
    supplier folders.
    """
    from src.main import PipelineExtractionFailedError, run_pipeline

    keywords = keywords if keywords is not None else load_keyword_bank()
    target = target_per_product or settings.BANK_TARGET_PER_PRODUCT
    country = country or settings.TARGET_COUNTRY
    extractor_factories = extractor_factories or _default_extractor_factories()
    engines = _resolve_engines(extractor_factories)

    export_root = Path(export_root or settings.MORNING_ANTHEM_EXPORT_DIR)
    exporters = exporters if exporters is not None else _default_exporters(
        export_root, engines
    )
    evaluator = evaluator or _build_default_evaluator()

    summary = BankIngestionSummary(
        keywords_total=len(keywords),
        per_engine={
            engine: {counter: 0 for counter in _FUNNEL_COUNTERS}
            | {"keywords_run": 0, "leg_failures": 0, "keywords_skipped": 0}
            for engine in engines
        },
        export_roots={
            engine: str(export_root / OPTIMAL_SUPPLIER_DIRS[engine])
            for engine in engines
        },
    )

    for product, group in _by_product(keywords):
        summary.products_attempted += 1
        scraped_for_product = 0
        for engine in engines:
            counters = summary.per_engine[engine]
            exported_for_product = 0
            for keyword in group:
                remaining = target - exported_for_product
                if remaining <= 0:
                    # The product's exact keyword(s) already filled the target
                    # for this engine, so its fallback is not worth a supplier
                    # search — that is the point of running the exact title first.
                    counters["keywords_skipped"] += 1
                    logger.info(
                        "Bank leg %s/%r skipped: %r already filled this "
                        "product's %d slot(s)",
                        engine, product, keyword.keyword, target,
                    )
                    continue
                extractor = extractor_factories[engine]()
                exporter = exporters[engine]
                before = {
                    name: getattr(exporter, name, 0)
                    for name in _EXPORTER_ACCUMULATED_COUNTERS
                }
                try:
                    leg = await run_pipeline(
                        keyword=keyword.keyword,
                        target_count=remaining,
                        country=country,
                        extractors=[extractor],
                        evaluator=evaluator,
                        exporter=exporter,
                    )
                except PipelineExtractionFailedError as exc:
                    # Unconfigured / blocked / timed out for this leg only.
                    counters["leg_failures"] += 1
                    logger.warning(
                        "Bank leg %s/%r failed — skipping this leg: %s",
                        engine, keyword.keyword, exc,
                    )
                    continue
                counters["keywords_run"] += 1
                for counter in _FUNNEL_COUNTERS:
                    if counter in _EXPORTER_ACCUMULATED_COUNTERS:
                        # From the EXPORTER, not the leg summary — see the note
                        # on _EXPORTER_ACCUMULATED_COUNTERS.
                        counters[counter] += (
                            getattr(exporter, counter, 0) - before[counter]
                        )
                    else:
                        counters[counter] += getattr(leg, counter, 0)
                summary.exports.extend(leg.exports)
                scraped_for_product += leg.candidates_scraped
                exported_for_product += leg.candidates_exported
                logger.info(
                    "Bank leg %s/%r (match=%s): scraped=%d accepted=%d "
                    "exported=%d (%d/%d of this product's target)",
                    engine, keyword.keyword, keyword.match,
                    leg.candidates_scraped, leg.accepted,
                    leg.candidates_exported, exported_for_product, target,
                )
        if scraped_for_product == 0:
            summary.products_empty += 1

    if engines and summary.products_attempted:
        if all(
            summary.per_engine[engine]["keywords_run"] == 0
            and summary.per_engine[engine]["leg_failures"] > 0
            for engine in engines
        ):
            reasons = ", ".join(
                f"{engine} ({summary.per_engine[engine]['leg_failures']} leg(s))"
                for engine in engines
            )
            raise BankIngestionFailedError(
                f"Every registered bank engine failed every one of the "
                f"{summary.products_attempted} product(s): {reasons}. Nothing "
                "could be read from the supplier."
            )
    return summary


def _build_default_evaluator() -> Any:
    """The shared LLM filter; raises LLMConfigError on a bad `.env`."""
    from src.evaluators.llm_filter import LLMEvaluationFilter

    return LLMEvaluationFilter()
