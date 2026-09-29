"""Step 5 — the keyword bank and its dual-supplier ingestion (plan §7).

The bank is the Step-4 deliverable (`outputs/step-4-gold-keywords.json`,
untracked): 50–70 gold-standard supplier search keywords, each typed with the
gold product it came from, its pillar, its role (broad/modifier) and the
broad term a modifier tightens.

Step 5 ingests EVERY bank keyword through BOTH supplier pipelines — the CJ
MCP server and the AliExpress Dropshipping Center — and exports what
survives into per-supplier folders of the gold-kernel tree:

    <OPTIMAL_EXPORT_DIR>/cjdropshipping/product-NN/…
    <OPTIMAL_EXPORT_DIR>/aliexpress/product-NN/…

**This is not `run_pipeline`'s fallback chain.** Auto mode stops at the first
engine that answers; the bank needs both engines to run for every keyword,
because the point of the step is to obtain the optimal product from each
supplier and let Step 6 rank them against each other. So each (keyword,
engine) leg is its own call into the existing `run_pipeline` with a
single-engine list, a shared evaluator and a per-engine exporter:

* an engine that is unconfigured/blocked/timeout for a leg is logged and
  skipped **for that leg only** — the other engine still runs that keyword;
* an engine that fails every keyword is recorded in the summary;
* only when **every** registered engine failed **every** keyword does
  ingestion raise, which the runner renders as the intervention block.

Every gate of the supplier core still applies untouched, because the legs run
the real extractors and the real exporter: CJ's commercial gate (§6.2), MCP
payload liveness gate (§5) and mandatory freight quote (§6.3); the DS
Center's winning-product gate (§6.1) and AU market pin; the LLM viability
gate, the margin floor, and the 3-image gallery gate before anything is
written.
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

# The two supplier engines the bank ingests through, in default order, and
# the subfolder each one writes into under OPTIMAL_EXPORT_DIR. The keys are
# the same supplier keys SUPPLIER_PRIORITY_ORDER and the CLI's --extractor
# use, so the registry in src.main needs no translation.
BANK_ENGINES = ("cjdropshipping", "aliexpress")
OPTIMAL_SUPPLIER_DIRS = {
    "cjdropshipping": "cjdropshipping",
    "aliexpress": "aliexpress",
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
    gold product it came from and stay in the bank file); `role`/`tightens`
    describe how the keyword sits against its broad term. Only `keyword`
    reaches a supplier query.
    """

    keyword: str
    product: str
    pillar: str
    role: str
    tightens: Optional[str]
    rationale: str


@dataclass
class BankIngestionSummary:
    """What one bank-ingestion run did, per engine and in total."""

    keywords_total: int = 0
    keywords_attempted: int = 0
    # Keywords for which NO engine returned a single verified supplier
    # product — nothing to evaluate, nothing to export.
    keywords_empty: int = 0
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
    bank_path = Path(path or settings.KEYWORD_BANK_PATH)
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
        tightens = row.get("tightens")
        keywords.append(
            BankKeyword(
                keyword=keyword,
                product=str(row.get("product") or ""),
                pillar=str(row.get("pillar") or ""),
                role=str(row.get("role") or ""),
                tightens=str(tightens).strip() if tightens else None,
                rationale=str(row.get("rationale") or ""),
            )
        )
    return keywords


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

    Numbering and the already-exported duplicate check are per exporter, so
    the two suppliers number their packages independently and a product
    landing from both suppliers is exported twice — intentionally, as two
    fulfilment options (§7.2). The supplier subfolder is created on the first
    write, so a run that exports nothing leaves no empty directory behind.
    """
    return {
        engine: CandidateExporter(
            export_dir=str(export_root / OPTIMAL_SUPPLIER_DIRS[engine])
        )
        for engine in engines
    }


async def ingest_keyword_bank(
    keywords: Optional[List[BankKeyword]] = None,
    target_per_keyword: Optional[int] = None,
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
    every keyword, and lets `LLMConfigError` / `DsCenterSessionExpiredError`
    from the leg propagate — both are boundary conditions the runner renders
    as interventions rather than per-keyword drops.
    """
    from src.main import PipelineExtractionFailedError, run_pipeline

    keywords = keywords if keywords is not None else load_keyword_bank()
    target = target_per_keyword or settings.BANK_TARGET_PER_KEYWORD
    country = country or settings.TARGET_COUNTRY
    extractor_factories = extractor_factories or _default_extractor_factories()
    engines = _resolve_engines(extractor_factories)

    export_root = Path(export_root or settings.OPTIMAL_EXPORT_DIR)
    exporters = exporters if exporters is not None else _default_exporters(
        export_root, engines
    )
    evaluator = evaluator or _build_default_evaluator()

    summary = BankIngestionSummary(
        keywords_total=len(keywords),
        per_engine={
            engine: {counter: 0 for counter in _FUNNEL_COUNTERS}
            | {"keywords_run": 0, "leg_failures": 0}
            for engine in engines
        },
        export_roots={
            engine: str(export_root / OPTIMAL_SUPPLIER_DIRS[engine])
            for engine in engines
        },
    )

    for keyword in keywords:
        summary.keywords_attempted += 1
        scraped_this_keyword = 0
        for engine in engines:
            counters = summary.per_engine[engine]
            extractor = extractor_factories[engine]()
            exporter = exporters[engine]
            before = {
                name: getattr(exporter, name, 0)
                for name in _EXPORTER_ACCUMULATED_COUNTERS
            }
            try:
                leg = await run_pipeline(
                    keyword=keyword.keyword,
                    target_count=target,
                    country=country,
                    extractors=[extractor],
                    evaluator=evaluator,
                    exporter=exporter,
                )
            except PipelineExtractionFailedError as exc:
                # Unconfigured / blocked / timed out for this leg only — the
                # other engine still runs this keyword (plan §7.1).
                counters["leg_failures"] += 1
                logger.warning(
                    "Bank leg %s/%r failed — skipping this leg: %s",
                    engine, keyword.keyword, exc,
                )
                continue
            counters["keywords_run"] += 1
            for counter in _FUNNEL_COUNTERS:
                if counter in _EXPORTER_ACCUMULATED_COUNTERS:
                    # From the EXPORTER, not the leg summary — see the note on
                    # _EXPORTER_ACCUMULATED_COUNTERS.
                    counters[counter] += (
                        getattr(exporter, counter, 0) - before[counter]
                    )
                else:
                    counters[counter] += getattr(leg, counter, 0)
            summary.exports.extend(leg.exports)
            scraped_this_keyword += leg.candidates_scraped
            logger.info(
                "Bank leg %s/%r: scraped=%d accepted=%d exported=%d",
                engine, keyword.keyword, leg.candidates_scraped,
                leg.accepted, leg.candidates_exported,
            )
        if scraped_this_keyword == 0:
            summary.keywords_empty += 1

    if engines and summary.keywords_attempted:
        if all(
            summary.per_engine[engine]["leg_failures"] == summary.keywords_attempted
            for engine in engines
        ):
            reasons = ", ".join(
                f"{engine} ({summary.per_engine[engine]['leg_failures']} leg(s))"
                for engine in engines
            )
            raise BankIngestionFailedError(
                f"Every registered bank engine failed every one of the "
                f"{summary.keywords_attempted} keyword(s): {reasons}. Nothing "
                "could be read from either supplier."
            )
    return summary


def _build_default_evaluator() -> Any:
    """The shared LLM filter; raises LLMConfigError on a bad `.env`."""
    from src.evaluators.llm_filter import LLMEvaluationFilter

    return LLMEvaluationFilter()
