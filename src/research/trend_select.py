"""Step 2 — Jev selection of the best N trend keywords from the gated pool.

Step 2 produces more usable candidate strings than the Step-3 scrape can
afford (one Google Shopping request per keyword, 10 HasData credits each), so
the pool must be narrowed. That narrowing is a **decision**, not a judgement
call, so it is made by Jev (TypeSafe **System One**) — the same client the
product-intent gate, the Step-3 pillar gate and the Step-6 ranking use.

Why a decision model rather than an LLM here: the task is "rank a known set of
candidates on one axis and cut at N". A decision model is consistent run to
run, returns a *number* rather than prose, and takes a compact factual context
per candidate. So Jev scores and code cuts — the model never decides the count,
never writes a keyword, and never sees a candidate it could invent.

**The state carries everything the decision can use**, because a decision model
is only as good as its context: per candidate the query, its pillar, its Trends
demand evidence verbatim and the *kind* of evidence it is (a 5-year mean vs a
rising/related-query figure); once per call the brand line, the task, how many
are being selected and the five pillar descriptions.

**Two objectives, and only the first belongs to Jev.**

1. *Filter out non-products and irrelevant Trends* — that is the **gate**
   (`trend_gate.py`), which runs before this pass.
2. *Take the best N while still representing the researched pool* — this pass.
   A per-candidate score cannot see the pool, so **balance is not a judgement
   for the model**; it is structural, and imposed by code. Asking Jev to balance
   would also make the outcome depend on how candidates happen to fall into
   batches.

**The cut is deterministic code:**

1. work out each pillar's **quota** from the pool's own distribution
   (`_quotas`): the pillar's proportional share of the target, given a floor of
   `TREND_SELECT_MIN_PER_PILLAR` and never more than the pool actually holds, and
   reconciled to the target by largest remainder;
2. within each pillar, take the best-scoring candidates up to that quota;
3. fill any shortfall (a pillar the pool could not fill, or a failed batch) by
   score across the whole pool;
4. ties break by whether the candidate carries demand evidence, then by the
   keyword string — so the same pool always yields the same selection.

The result mirrors the researched demand: a pillar that supplied 40 of 60
candidates takes about 20 of the 30 slots rather than all 30, and no pillar that
supplied candidates is dropped. `--no-pillar-quota` restores a pure best-of.

A candidate whose batch failed twice scores as unclassified and ranks last, so
it is taken only if slots remain — the same containment the other Jev passes
apply. **The one exception is a pool where nothing scored at all**: then
nothing is selected and the caller raises the intervention, because spending
the Step-3 credits on an unranked pool is exactly the failure this pass exists
to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.config import settings
from src.keywords.generator import PILLARS
from src.ranking.jev_client import (
    PILLAR_OPTIONS,
    JevClient,
    JevError,
    answer_score,
)
from src.research.trend_gate import GatedKeyword, TrendGateError
from collections import Counter  # noqa: F401  (kept for callers importing it)

#: The question name each candidate carries; the batch fan-in keys off
#: `<index>__keyword_quality`.
TREND_SELECT_QUESTION = "keyword_quality"

#: Ordered worst -> best. ONE axis only — how good the string is as a
#: supplier-sourcing keyword for its pillar. Demand evidence is part of the
#: judgement, but a vague or brand-bound string cannot be rescued by it.
QUALITY_LEVELS: List[str] = [
    (
        "unlikely to find a supplier listing: too vague, a brand or retailer "
        "term, or no clear physical product"
    ),
    (
        "a plausible product search but weak as a sourcing keyword — broad, "
        "ambiguous, or describing a category rather than an object"
    ),
    "a usable sourcing keyword with moderate demand evidence",
    (
        "a strong sourcing keyword: specific, clearly a physical product, with "
        "good demand evidence"
    ),
    (
        "an excellent sourcing keyword: specific and physical, demand evidence "
        "strong and rising, squarely inside its pillar"
    ),
]

#: A failed batch is retried once before its candidates are left unclassified.
_MAX_ATTEMPTS = 2

_INSTRUCTIONS = (
    "Judge the search string at `candidates.{index}` as a SUPPLIER-SOURCING "
    "keyword: if we search `{catalogue}` with it, how likely are we to find "
    "this physical product, and how well does its demand evidence support "
    "listing it? Weigh specificity, whether it names an object rather than a "
    "category or a brand, and its Trends evidence. Ignore how it would read as "
    "marketing copy."
)


@dataclass
class ScoredKeyword:
    """One gated survivor plus Jev's quality score and the cut's verdict."""

    keyword: str
    pillar: str
    demand_evidence: str
    #: Jev's 1-5 level position (already shifted off its 0-based raw value), or
    #: None when the batch failed and the candidate is unclassified.
    quality_score: Optional[float] = None
    #: 1-based position in the selection order, or None when it was not ranked
    #: (it always is, so None only appears for an empty pool).
    rank: Optional[int] = None
    selected: bool = False
    note: str = ""


class TrendKeywordSelector:
    """Scores the gated pool with Jev and cuts the best N."""

    def __init__(
        self,
        client: Optional[JevClient] = None,
        batch_size: Optional[int] = None,
        target: Optional[int] = None,
        min_per_pillar: Optional[int] = None,
        pillar_quota: bool = True,
    ) -> None:
        self.batch_size = (
            batch_size
            if batch_size is not None
            else settings.TREND_SELECT_BATCH_SIZE
        )
        self.target = (
            target if target is not None else settings.TREND_SELECT_TARGET
        )
        self.min_per_pillar = (
            min_per_pillar
            if min_per_pillar is not None
            else settings.TREND_SELECT_MIN_PER_PILLAR
        )
        if self.batch_size < 1:
            raise TrendGateError(
                f"batch_size must be >= 1 (got {self.batch_size})"
            )
        if self.target < 1:
            raise TrendGateError(f"target must be >= 1 (got {self.target})")
        if self.min_per_pillar < 0:
            raise TrendGateError(
                f"min_per_pillar must be >= 0 (got {self.min_per_pillar})"
            )
        #: Proportional representation by default; False restores a pure
        #: best-of cut (an escape hatch, not the recommended shape).
        self.pillar_quota = pillar_quota
        # The client last: with an injected client this never touches `.env`.
        self.client = client if client is not None else JevClient()

    # --- scoring ----------------------------------------------------------

    def _questions(self, count: int) -> Dict[str, Dict[str, Any]]:
        """One score question per candidate in the batch."""
        return {
            f"{index}__{TREND_SELECT_QUESTION}": {
                "type": "score",
                "instructions": _INSTRUCTIONS.format(
                    index=index, catalogue="CJdropshipping"
                ),
                "criteria": list(QUALITY_LEVELS),
            }
            for index in range(count)
        }

    def _state(self, batch: Sequence[GatedKeyword]) -> Dict[str, Any]:
        """The shared state: the brand frame once, the candidates in full."""
        return {
            "brand": (
                "The Morning Anthem — a premium morning-lifestyle store "
                "launching to Australian shoppers in AUD; the supplier "
                "catalogue is CJdropshipping."
            ),
            "task": (
                "Rank each candidate search string by how good it is as a "
                "supplier-sourcing keyword for its pillar. The best "
                f"{self.target} will be searched against Google Shopping and "
                "then against the supplier catalogue."
            ),
            "pillars": {
                key: description
                for key, description in PILLAR_OPTIONS.items()
                if key in PILLARS
            },
            "candidates": [
                {
                    "index": index,
                    "query": row.keyword,
                    "pillar": row.pillar,
                    "trends_evidence": row.demand_evidence,
                    "evidence_kind": _evidence_kind(row.demand_evidence),
                }
                for index, row in enumerate(batch)
            ],
        }

    def _score_batch(
        self, batch: Sequence[GatedKeyword]
    ) -> Optional[Dict[str, dict]]:
        """One System One call, retried once; None when both attempts fail."""
        state = self._state(batch)
        questions = self._questions(len(batch))
        for _ in range(_MAX_ATTEMPTS):
            try:
                return self.client.decide(state, questions)
            except JevError:
                continue
        return None

    # --- the cut ----------------------------------------------------------

    def select(self, candidates: Sequence[GatedKeyword]) -> List[ScoredKeyword]:
        """Score every candidate, then cut the best ``target`` deterministically.

        Returns the selected rows first (in selection order), then the
        remainder — so a caller can write the two lists apart without
        re-deriving which is which.
        """
        scored: List[ScoredKeyword] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = list(candidates[start : start + self.batch_size])
            answers = self._score_batch(batch)
            for index, row in enumerate(batch):
                score: Optional[float] = None
                note = ""
                if answers is None:
                    note = (
                        "Jev batch call failed twice — unclassified; it can "
                        "only be selected if slots remain"
                    )
                else:
                    score = answer_score(
                        answers.get(f"{index}__{TREND_SELECT_QUESTION}")
                    )
                    if score is None:
                        note = "Jev returned no usable score"
                scored.append(
                    ScoredKeyword(
                        keyword=row.keyword,
                        pillar=row.pillar,
                        demand_evidence=row.demand_evidence,
                        quality_score=score,
                        note=note,
                    )
                )

        picked = _cut(
            scored,
            self.target,
            self.min_per_pillar,
            pillar_quota=self.pillar_quota,
        )

        # Inverted tolerance, the same rule §5/§6.2/§6.3 apply to stock,
        # seller proof and freight: if Jev scored NOTHING, "we couldn't tell"
        # is not an answer. Selecting arbitrary keywords here would spend the
        # whole Step-3 budget (300 credits at the launch sizing) on an unranked
        # pool, so nothing is selected and the runner raises the intervention.
        if picked and all(row.quality_score is None for row in picked):
            for row in picked:
                row.selected = False
                row.rank = None
        return picked


def _evidence_kind(evidence: str) -> str:
    """Which sort of Trends figure the evidence string carries.

    A decision model is only as good as its context: "5-year mean interest 3"
    and "rising query +150%" are not the same claim, and the state says which
    one each candidate is resting on.
    """
    lowered = (evidence or "").lower()
    if "rising" in lowered or "breakout" in lowered:
        return "rising query"
    if "related query" in lowered:
        return "related-query index"
    if "mean interest" in lowered:
        return "5-year mean interest"
    if not lowered:
        return "none recorded"
    return "other Trends figure"


def pillar_quotas(
    counts: Dict[str, int], target: int, min_per_pillar: int
) -> Dict[str, int]:
    """How many slots each pillar gets, in the pool's own proportions.

    Proportional representation, not a score race: a pillar that supplied 40 of
    60 candidates takes about 20 of 30 slots rather than all 30, and every
    pillar that supplied candidates keeps at least ``min_per_pillar`` of them
    (capped by what the pool holds). Shares are floored, the floor is applied,
    then the remaining slots go out by largest remainder — so the quotas sum to
    exactly ``target`` whenever the pool can fill it, and the same pool always
    yields the same quotas.
    """
    pillars = [p for p in PILLARS if counts.get(p, 0) > 0]
    if not pillars or target <= 0:
        return {}

    total = sum(counts[p] for p in pillars)
    ideal = {p: target * counts[p] / total for p in pillars}
    # Never promise a pillar more slots than it actually holds: when the target
    # is wider than the pool the proportional share overshoots the candidates.
    quota = {
        p: min(
            counts[p],
            max(int(ideal[p]), min(min_per_pillar, counts[p])),
        )
        for p in pillars
    }

    def headroom(p: str) -> int:
        return quota[p] - min(min_per_pillar, counts[p])

    # Trim over-allocation (the floor can overshoot a small target) from the
    # pillars not already at their own floor, largest first.
    while sum(quota.values()) > target:
        room = [p for p in pillars if headroom(p) > 0]
        if not room:
            break
        victim = max(room, key=lambda p: (quota[p], counts[p], -pillars.index(p)))
        quota[victim] -= 1

    # Hand the remaining slots out by largest fractional remainder, then by the
    # bigger pool, then in PILLARS order — a total order, so it is reproducible.
    while sum(quota.values()) < target:
        room = [p for p in pillars if quota[p] < counts[p]]
        if not room:
            break
        winner = max(
            room,
            key=lambda p: (ideal[p] - quota[p], counts[p], -pillars.index(p)),
        )
        quota[winner] += 1

    if sum(quota.values()) > target:
        # The floor is unreachable: min_per_pillar x pillars exceeds the target
        # and no pillar has candidates above its floor to trim from. Silently
        # exceeding the target would spend more Step-3 credits than budgeted,
        # and silently dropping the floor would break the representation
        # promise — so say so instead.
        raise TrendGateError(
            f"cannot select {target} keyword(s) with min_per_pillar="
            f"{min_per_pillar} across {len(pillars)} pillar(s) holding "
            f"candidates: the floor alone needs {sum(quota.values())} slot(s). "
            f"Lower min_per_pillar or raise the target."
        )
    return quota


def _cut(
    scored: List[ScoredKeyword],
    target: int,
    min_per_pillar: int,
    pillar_quota: bool = True,
) -> List[ScoredKeyword]:
    """Deterministically pick ``target`` rows, representing every pillar.

    Sorting is stable and total: score descending, then has-evidence before
    no-evidence, then the keyword string — so the same pool and the same scores
    always produce the same selection, whatever order Jev answered in.
    """
    ordered = sorted(
        scored,
        key=lambda row: (
            -(row.quality_score if row.quality_score is not None else -1.0),
            0 if row.demand_evidence else 1,
            row.keyword,
        ),
    )

    picked: List[ScoredKeyword] = []
    seen: set[int] = set()

    if pillar_quota:
        # 1) each pillar's share, taking its best rows up to the quota.
        counts: Dict[str, int] = {}
        for row in ordered:
            counts[row.pillar] = counts.get(row.pillar, 0) + 1
        quotas = pillar_quotas(counts, target, min_per_pillar)
        taken: Dict[str, int] = {}
        for row in ordered:
            if taken.get(row.pillar, 0) >= quotas.get(row.pillar, 0):
                continue
            picked.append(row)
            seen.add(id(row))
            taken[row.pillar] = taken.get(row.pillar, 0) + 1

    # 2) any shortfall by score across the whole pool — a pillar the pool could
    # not fill its quota for, a failed batch, or a pure best-of run.
    for row in ordered:
        if len(picked) >= target:
            break
        if id(row) not in seen:
            picked.append(row)
            seen.add(id(row))

    for position, row in enumerate(picked, start=1):
        row.selected = True
        row.rank = position
    for row in ordered:
        if id(row) not in seen:
            row.rank = None
    return picked + [row for row in ordered if id(row) not in seen]


def write_selected_deliverable(
    selected: Sequence[ScoredKeyword],
    output: Path,
    gated_path: Optional[Path] = None,
    reserve_count: Optional[int] = None,
) -> None:
    """Write the Step-3 intake (the selected keywords) and keep the pool.

    The Step-2 deliverable holds ONLY the keywords the scrape will pay for, so
    Step 3's credit envelope is exactly this list. The full gated pool is
    written beside it, because it already cost its HasData credits and is the
    re-run reserve: a later run (or a different ``--target``) can be fed from
    it without paying for trends again.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    picked = [row for row in selected if row.selected]
    output.write_text(
        json.dumps(
            {
                "keywords": [
                    {
                        "keyword": row.keyword,
                        "pillar": row.pillar,
                        "demand_evidence": row.demand_evidence,
                        "quality_score": row.quality_score,
                        "rank": row.rank,
                    }
                    for row in picked
                ],
                "generated_with": "HasData Google Trends MCP (geo=AU)",
                "gate": "jev_product_intent",
                "selection": "jev_keyword_quality",
                "gated_pool": len(selected),
                "selected": len(picked),
                "reserve": len(selected) - len(picked),
                "note": (
                    "Step 2 deliverable — Jev-gated and Jev-selected "
                    "product-search keywords; Step 3's intake; not tracked"
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    if gated_path is not None:
        gated_path = Path(gated_path)
        gated_path.parent.mkdir(parents=True, exist_ok=True)
        gated_path.write_text(
            json.dumps(
                {
                    "keywords": [
                        {
                            "keyword": row.keyword,
                            "pillar": row.pillar,
                            "demand_evidence": row.demand_evidence,
                            "quality_score": row.quality_score,
                            "selected": row.selected,
                            "rank": row.rank,
                            "note": row.note,
                        }
                        for row in selected
                    ],
                    "note": (
                        "The full Jev-gated pool with the selection verdict on "
                        "each row — the re-run reserve; not tracked"
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
