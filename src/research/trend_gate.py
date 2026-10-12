"""Step 2 — the Jev product-intent gate over the Morning Anthem trend strings.

Google Trends answers "what are people searching", not "what are people
buying": the same series carries how-to queries, news topics, brand
controversies and service/digital goods alongside genuine product searches. A
string that is not a product search costs a Step-3 Google Shopping request
(10 HasData credits) and pollutes the gold set with things no supplier could
ever fulfil, so every candidate is classified before any credit moves.

The gate is Jev (TypeSafe **System One** via OpenRouter) — the same client the
Step-3 pillar gate and the Step-6 ranking use (`src.ranking.jev_client`), so
all three share one transport, one error taxonomy and one batching idiom:

* one `decide()` call carries `TREND_GATE_BATCH_SIZE` candidate strings;
* each candidate gets ONE `choice` question, `<i>__product_intent`, over a
  four-way closed set (`INTENT_OPTIONS`): only `product_search` survives;
* a failed call retries once; a second failure drops that batch's strings with
  an explicit note, so one bad call never voids a whole research pass;
* a missing `OPENROUTER_API_KEY` raises `JevConfigError`, which the runner
  renders as the intervention block (the step gates on the operator's
  credential);
* a survivor is ALSO screened against the keyword engine's banned-token
  boundary (`carries_banned_token`), because Jev rightly reads a
  retailer-qualified query ("milk frother kmart") as a product search — it is
  one — while that string could never become a bank keyword. The screen is
  deterministic and free, so it runs on every survivor.

Anti-hallucination posture: Jev contributes **a classification and nothing
else**. The keyword string, its pillar tag and its Trends demand evidence are
carried through verbatim from the Step-2 research file — the gate can drop a
string but can never author or edit one, exactly as the Step-3 curator may
only select rows by url and the Step-6 ranker may only score a package.

Cost: Jev is billed in USD by OpenRouter (not HasData credits), at the
vendor's published input rate — a full research pass of a few dozen strings is
fractions of a cent, and it is what keeps the HasData envelope for the
scrapes that actually need it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.config import settings
from src.keywords.generator import PILLARS, carries_banned_token
from src.ranking.jev_client import (
    JevClient,
    JevError,
    answer_choice,
)

logger = logging.getLogger(__name__)

#: The question name each candidate carries; the batch fan-in keys off
#: `<index>__product_intent`.
TREND_GATE_QUESTION = "product_intent"

#: The closed verdict set. `product_search` is the only surviving class: the
#: Morning Anthem sources physical goods, so an informational, news/brand or
#: service/digital string has no supplier listing behind it.
INTENT_OPTIONS: Dict[str, str] = {
    "product_search": (
        "the query is what a shopper types to FIND and BUY a physical product "
        "(a tool, gadget, accessory or other shippable object)"
    ),
    "informational": (
        "a how-to, explainer, comparison or research query — the searcher "
        "wants to know something, not to buy an object"
    ),
    "news_or_brand": (
        "a news topic, event, person, celebrity, or a brand name or "
        "controversy rather than a generic product"
    ),
    "service_or_digital": (
        "a service, app, software, course, booking, subscription or other "
        "digital good — nothing a supplier could ship"
    ),
}

#: The per-candidate instruction. The candidate is addressed by its index into
#: the call's shared state, per TypeSafe's guidance to reference `state` by
#: backticked paths.
_INTENT_INSTRUCTIONS = (
    "Classify the search query at `candidates.{index}`. Decide what the "
    "person typing it wants: an object to buy, information, news about a "
    "person or brand, or a service/digital good. Judge the query's own intent "
    "only — not whether the product would sell well."
)

#: A failed batch is retried once before its strings are dropped.
_MAX_ATTEMPTS = 2


class TrendGateError(Exception):
    """The Step-2 keyword file is missing, malformed, or unusable."""


@dataclass
class TrendCandidate:
    """One candidate string from the Step-2 Trends research.

    All three fields are verbatim from the research file: the gate classifies
    the string, it never authors or edits it.
    """

    keyword: str
    pillar: str
    demand_evidence: str = ""


@dataclass
class GatedKeyword:
    """One candidate and the gate's verdict on it."""

    keyword: str
    pillar: str
    demand_evidence: str
    #: Jev's verdict key, or None when the batch that carried this candidate
    #: failed outright (the string is dropped either way).
    intent: Optional[str] = None
    kept: bool = False
    note: str = ""


def load_trend_candidates(path: Optional[Path] = None) -> List[TrendCandidate]:
    """Read the Step-2 research file's candidate strings (fail closed).

    A missing or malformed file, an empty list, a row that is not an object, a
    blank or non-string keyword, and a pillar outside the five Morning Anthem
    keys each raise `TrendGateError` with remediation text — the gate's input
    is the research record, and a bad one must not quietly yield a short list
    that looks like a successful filter.
    """
    source = Path(path or settings.MORNING_ANTHEM_TREND_KEYWORDS_PATH)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrendGateError(
            f"Step-2 trend keyword file not found at {source} ({exc}). Run the "
            "Step-2 research through the HasData Google Trends MCP first and "
            "write its candidates there."
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise TrendGateError(
            f"Step-2 trend keyword file at {source} is not valid JSON ({exc})."
        ) from exc

    rows = payload.get("keywords") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise TrendGateError(
            f"Step-2 trend keyword file at {source} must be a JSON list or an "
            f'object with a "keywords" list (got {type(payload).__name__}).'
        )
    if not rows:
        raise TrendGateError(
            f"Step-2 trend keyword file at {source} is empty — there is "
            "nothing to gate."
        )

    candidates: List[TrendCandidate] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TrendGateError(
                f"Step-2 row {index} at {source} is {type(row).__name__}, "
                "not an object."
            )
        keyword = row.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            raise TrendGateError(
                f"Step-2 row {index} at {source} has no usable 'keyword' "
                f"string (got {keyword!r})."
            )
        pillar = str(row.get("pillar") or "").strip()
        if pillar not in PILLARS:
            raise TrendGateError(
                f"Step-2 row {index} ({keyword.strip()!r}) has pillar "
                f"{pillar!r}, which is not one of the five Morning Anthem "
                f"pillars {list(PILLARS)}. Re-tag the research row."
            )
        candidates.append(
            TrendCandidate(
                keyword=keyword.strip(),
                pillar=pillar,
                demand_evidence=str(row.get("demand_evidence") or "").strip(),
            )
        )
    return candidates


class TrendIntentGate:
    """Classifies candidate trend strings as product searches (or not)."""

    def __init__(
        self,
        client: Optional[JevClient] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.batch_size = (
            batch_size if batch_size is not None else settings.TREND_GATE_BATCH_SIZE
        )
        if self.batch_size < 1:
            raise TrendGateError(
                f"batch_size must be >= 1 (got {self.batch_size})"
            )
        # The client last: with an injected client this never touches `.env`,
        # so tests need no credential. Without one, JevConfigError propagates
        # for the runner to render as an intervention.
        self.client = client if client is not None else JevClient()

    # --- batching ---------------------------------------------------------

    def _questions(self, count: int) -> Dict[str, Dict[str, Any]]:
        """One choice question per candidate in the batch."""
        return {
            f"{index}__{TREND_GATE_QUESTION}": {
                "type": "choice",
                "instructions": _INTENT_INSTRUCTIONS.format(index=index),
                "criteria": dict(INTENT_OPTIONS),
            }
            for index in range(count)
        }

    def _call_with_retry(
        self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, dict]]:
        """One System One call, retried once; None when both attempts fail.

        A dead call must not void the whole research pass — the caller drops
        only this batch's strings, exactly as the Step-6 ranker degrades a
        single bad batch to `disregard` rather than aborting.
        """
        last: Optional[Exception] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self.client.decide(state, questions)
            except JevError as exc:
                last = exc
                logger.warning(
                    "Trend gate batch call failed (attempt %d/%d): %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )
        logger.error(
            "Trend gate batch dropped after %d attempt(s): %s",
            _MAX_ATTEMPTS, last,
        )
        return None

    # --- the gate ---------------------------------------------------------

    def gate(self, candidates: Sequence[TrendCandidate]) -> List[GatedKeyword]:
        """Classify every candidate; the result carries each verdict.

        Order is preserved, so a caller can zip the input and output.
        """
        results: List[GatedKeyword] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = list(candidates[start : start + self.batch_size])
            state = {
                "candidates": [
                    {
                        "index": index,
                        "query": candidate.keyword,
                        "pillar": candidate.pillar,
                        "trends_evidence": candidate.demand_evidence,
                    }
                    for index, candidate in enumerate(batch)
                ],
                "pillars": list(PILLARS),
            }
            answers = self._call_with_retry(state, self._questions(len(batch)))
            for index, candidate in enumerate(batch):
                if answers is None:
                    results.append(
                        GatedKeyword(
                            keyword=candidate.keyword,
                            pillar=candidate.pillar,
                            demand_evidence=candidate.demand_evidence,
                            intent=None,
                            kept=False,
                            note=(
                                "Jev batch call failed twice — the string was "
                                "not classified and is dropped"
                            ),
                        )
                    )
                    continue
                verdict = answer_choice(
                    answers.get(f"{index}__{TREND_GATE_QUESTION}")
                )
                kept = verdict == "product_search"
                note = "" if kept else f"intent {verdict or 'unclassified'}"
                if kept:
                    # Defence in depth, and the pilot is what proved it is
                    # needed: Jev rightly reads a retailer- or brand-qualified
                    # query ("milk frother kmart") as a product search, because
                    # it IS one — but the keyword engine's own banned-token
                    # boundary forbids that string, so scraping it would spend
                    # a Step-3 credit on a keyword the bank could never carry.
                    # Deterministic and free, so it runs on every survivor.
                    banned = carries_banned_token(candidate.keyword)
                    if banned is not None:
                        kept = False
                        note = f"banned token {banned!r}"
                results.append(
                    GatedKeyword(
                        keyword=candidate.keyword,
                        pillar=candidate.pillar,
                        demand_evidence=candidate.demand_evidence,
                        intent=verdict,
                        kept=kept,
                        note=note,
                    )
                )

        kept = sum(1 for row in results if row.kept)
        if results:
            logger.info(
                "Trend gate: %d/%d candidate string(s) are product searches",
                kept, len(results),
            )
        return results


def write_gated_deliverable(
    results: Sequence[GatedKeyword],
    output: Path,
    raw_path: Optional[Path] = None,
    generated_with: str = "HasData Google Trends MCP (geo=AU)",
) -> None:
    """Write the filtered Step-2 deliverable, preserving the raw list.

    The gated file is what Step 3 reads, so it holds only surviving strings;
    the pre-gate list is kept beside it (``…-raw.json``) as the audit trail for
    what the gate dropped and why.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    kept = [
        {
            "keyword": row.keyword,
            "pillar": row.pillar,
            "demand_evidence": row.demand_evidence,
            "intent": row.intent,
        }
        for row in results
        if row.kept
    ]
    output.write_text(
        json.dumps(
            {
                "keywords": kept,
                "generated_with": generated_with,
                "gate": "jev_product_intent",
                "candidates_gated": len(results),
                "candidates_dropped": len(results) - len(kept),
                "note": (
                    "Step 2 deliverable — Jev-gated product-search strings; "
                    "not tracked"
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    if raw_path is not None:
        raw_path = Path(raw_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(
                {
                    "keywords": [
                        {
                            "keyword": row.keyword,
                            "pillar": row.pillar,
                            "demand_evidence": row.demand_evidence,
                            "intent": row.intent,
                            "kept": row.kept,
                            "note": row.note,
                        }
                        for row in results
                    ],
                    "note": (
                        "Pre-gate Step-2 candidates with the gate's verdict on "
                        "each; the audit trail behind the filtered file; not "
                        "tracked"
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
