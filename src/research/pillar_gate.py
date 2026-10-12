"""Step 3 — the Jev pillar gate: classify curated gold products into the pillars.

The last stage of Step 3. After the Google Shopping scrape and the LLM's
select-by-url curation, every surviving product is classified by Jev (TypeSafe
**System One** via OpenRouter) into exactly one of The Morning Anthem's five
merchandising pillars — or `discard`, when it fits none of them or fails the
store's boundaries. A discarded product never reaches the gold list.

**Placement is deliberate (operator decision 2026-10-12): the gate runs AFTER
curation, not before the scrape.** A decision model is only as good as its
context, and a pillar is not an honest question without *commercial* context:
"is this a Restful Reset product?" reads differently when the row carries a
price and 4.7/5 from 1,200 reviews than when it carries a bare title. So the
cheap stages run first and this one sees enriched rows.

**What the state carries.** Per product: the name, description, the retail
price as BOTH its display string and its number, the rating, the review count,
the demand-evidence line and the source keyword. Numbers in particular — a
decision model should reason over `4.7` and `89.95`, not parse them out of text.
Once per call: the brand line, the task, and the five pillar descriptions.

The curator's own pillar guess is deliberately **not** passed: it would bias the
classification this gate exists to make, which is why the curator no longer
assigns one (`src/evaluators/gold_curator.py`).

**What it authors.** Only the pillar key. Every product fact — name, url, price,
merchant, demand evidence — was already code-assembled from the scraped rows by
the curator, and the gate echoes those fields through untouched.

**Failure containment**, the same shape as the Step-2 passes: a failed call
retries once, then its batch's products are dropped with an explicit note rather
than the run aborting. One `discard` is a normal verdict, not an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from src.config import settings
from src.evaluators.gold_curator import GoldStandardProduct, PILLARS
from src.extractors.shopping_row import ShoppingRow
from src.ranking.jev_client import (
    PILLAR_OPTIONS,
    JevClient,
    JevError,
    answer_choice,
)
from src.research.trend_gate import TrendGateError

logger = logging.getLogger(__name__)

#: The question name each product carries; the batch fan-in keys off
#: `<index>__pillar`.
PILLAR_GATE_QUESTION = "pillar"

#: `discard` means more at this stage than it does to the Step-6 ranker, so the
#: gate owns its own wording: a Step-3 product can fail the pillars by simply not
#: being a Morning Anthem product, which is the common case here.
DISCARD_NOTE = (
    "fits none of the five Morning Anthem pillars, or fails the store's "
    "boundaries — a cosmetic, consumable or supplement, celebrity or "
    "cultural-icon IP, or an item that cannot be air-freighted to Australia"
)

#: The criteria map the choice question offers: the five pillars (descriptions
#: reused from the one journey-wide source of truth, `PILLAR_OPTIONS`) plus this
#: stage's own `discard`.
PILLAR_GATE_OPTIONS: Dict[str, str] = {
    **{key: PILLAR_OPTIONS[key] for key in PILLARS},
    "discard": DISCARD_NOTE,
}

_INSTRUCTIONS = (
    "Classify the product at `products.{index}` into exactly ONE of The Morning "
    "Anthem's five merchandising pillars, by matching what the product actually "
    "is against each pillar's product lines. Use its price and its "
    "rating/review evidence as context for how the product is positioned and how "
    "proven it is. Choose `discard` only when the product fits none of the five "
    "pillars, or fails the store's boundaries. `discard` is not a fallback for "
    "uncertainty — pick the closest pillar when the product plausibly belongs to "
    "one."
)

#: A failed batch is retried once before its products are dropped.
_MAX_ATTEMPTS = 2


@dataclass
class PillarCandidate:
    """One curated product plus the row facts the gate classifies it with.

    Everything here comes from the scraped row or the code-assembled product —
    nothing is authored for the model.
    """

    url: str
    name: str
    description: Optional[str] = None
    retail_price_text: Optional[str] = None
    price_value: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    demand_evidence: str = ""
    source_keyword: str = ""

    @classmethod
    def from_product(
        cls,
        product: GoldStandardProduct,
        row: Optional[ShoppingRow] = None,
    ) -> "PillarCandidate":
        """Build a candidate from the curated product, enriched by its row.

        The numbers (`price_value`, `rating`, `review_count`) live on the scraped
        row rather than the product, so the row is looked up by url — the same
        join key the curator used. A product whose row has gone missing still
        yields a candidate: its text fields are enough to classify, and the
        numbers simply stay None rather than being guessed.
        """
        return cls(
            url=product.url,
            name=product.name,
            description=product.description,
            retail_price_text=product.retail_price_text
            or (row.price_text if row else None),
            price_value=row.price_value if row else None,
            rating=row.rating if row else None,
            review_count=row.rating_count if row else None,
            demand_evidence=product.demand_evidence,
            source_keyword=product.source_keyword,
        )


@dataclass
class GatedProduct:
    """One candidate and the gate's verdict on it."""

    candidate: PillarCandidate
    #: One of the five pillar keys, or None when the gate discarded it or could
    #: not classify it.
    pillar: Optional[str] = None
    kept: bool = False
    note: str = ""


class PillarGate:
    """Classifies curated gold products into the five Morning Anthem pillars."""

    def __init__(
        self,
        client: Optional[JevClient] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.batch_size = (
            batch_size if batch_size is not None else settings.PILLAR_GATE_BATCH_SIZE
        )
        if self.batch_size < 1:
            raise TrendGateError(
                f"batch_size must be >= 1 (got {self.batch_size})"
            )
        # The client last: with an injected client this never touches `.env`.
        self.client = client if client is not None else JevClient()

    # --- batching ---------------------------------------------------------

    def _questions(self, count: int) -> Dict[str, Dict[str, Any]]:
        """One choice question per product in the batch."""
        return {
            f"{index}__{PILLAR_GATE_QUESTION}": {
                "type": "choice",
                "instructions": _INSTRUCTIONS.format(index=index),
                "criteria": dict(PILLAR_GATE_OPTIONS),
            }
            for index in range(count)
        }

    def _state(self, batch: Sequence[PillarCandidate]) -> Dict[str, Any]:
        """The shared state: the brand frame once, then each product in full."""
        return {
            "brand": (
                "The Morning Anthem — a premium morning-lifestyle store "
                "launching to Australian shoppers in AUD."
            ),
            "task": (
                "Assign every product to exactly one of the five pillars, or "
                "`discard` it if it fits none."
            ),
            "pillars": {key: PILLAR_OPTIONS[key] for key in PILLARS},
            "products": [
                {
                    "index": index,
                    "name": candidate.name,
                    "description": candidate.description,
                    "price_text": candidate.retail_price_text,
                    # Numbers, so the model reasons rather than parses.
                    "price_value": candidate.price_value,
                    "rating": candidate.rating,
                    "review_count": candidate.review_count,
                    "demand_evidence": candidate.demand_evidence,
                    "source_keyword": candidate.source_keyword,
                }
                for index, candidate in enumerate(batch)
            ],
        }

    def _call_with_retry(
        self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, dict]]:
        """One System One call, retried once; None when both attempts fail."""
        last: Optional[Exception] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self.client.decide(state, questions)
            except JevError as exc:
                last = exc
                logger.warning(
                    "Pillar gate batch call failed (attempt %d/%d): %s",
                    attempt, _MAX_ATTEMPTS, exc,
                )
        logger.error(
            "Pillar gate batch dropped after %d attempt(s): %s",
            _MAX_ATTEMPTS, last,
        )
        return None

    # --- the gate ---------------------------------------------------------

    def gate(self, candidates: Sequence[PillarCandidate]) -> List[GatedProduct]:
        """Classify every candidate; order is preserved."""
        results: List[GatedProduct] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = list(candidates[start : start + self.batch_size])
            answers = self._call_with_retry(
                self._state(batch), self._questions(len(batch))
            )
            for index, candidate in enumerate(batch):
                if answers is None:
                    results.append(
                        GatedProduct(
                            candidate=candidate,
                            pillar=None,
                            kept=False,
                            note=(
                                "Jev batch call failed twice — the product was "
                                "not classified and is dropped"
                            ),
                        )
                    )
                    continue
                verdict = answer_choice(
                    answers.get(f"{index}__{PILLAR_GATE_QUESTION}")
                )
                kept = verdict in PILLARS
                results.append(
                    GatedProduct(
                        candidate=candidate,
                        pillar=verdict if kept else None,
                        kept=kept,
                        note=(
                            ""
                            if kept
                            else (
                                "pillar discard"
                                if verdict == "discard"
                                else f"unclassified ({verdict or 'no verdict'})"
                            )
                        ),
                    )
                )

        kept = sum(1 for row in results if row.kept)
        if results:
            logger.info(
                "Pillar gate: %d/%d product(s) classified into a pillar",
                kept, len(results),
            )
        return results


def attach_pillars(
    products: Sequence[GoldStandardProduct],
    results: Sequence[GatedProduct],
) -> List[GoldStandardProduct]:
    """Copy each kept product's pillar onto the product, and return the kept set.

    The pillar is the gate's to write and the product's to carry: this is the one
    place the two meet, so a caller cannot accidentally write a pillar the gate
    did not issue.
    """
    by_url = {row.candidate.url: row for row in results}
    kept: List[GoldStandardProduct] = []
    for product in products:
        result = by_url.get(product.url)
        if result is None or not result.kept or result.pillar is None:
            continue
        product.pillar = result.pillar
        kept.append(product)
    return kept
