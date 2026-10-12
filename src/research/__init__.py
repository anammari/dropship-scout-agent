"""Step 2 — trend research: the candidate strings and their Jev intent gate.

The Morning Anthem's Step 2 turns Google Trends research into the supplier
search strings Step 3 scrapes. A Trends string is only usable if it is what a
shopper types to FIND a product, so every candidate is routed through Jev
(TypeSafe System One via OpenRouter) before it may cost a HasData credit.

* `trend_gate` — the Jev product-intent gate over the candidate strings.
* `trend_select` — the Jev quality selection that cuts the gated pool to N.
* `pillar_gate` — the Step-3 Jev pillar gate over curated gold products.
"""

from src.research.pillar_gate import (
    DISCARD_NOTE,
    PILLAR_GATE_OPTIONS,
    PILLAR_GATE_QUESTION,
    GatedProduct,
    PillarCandidate,
    PillarGate,
    attach_pillars,
)
from src.research.trend_gate import (
    INTENT_OPTIONS,
    TREND_GATE_QUESTION,
    GatedKeyword,
    TrendCandidate,
    TrendGateError,
    TrendIntentGate,
    load_trend_candidates,
    write_gated_deliverable,
)

__all__ = [
    "DISCARD_NOTE",
    "PILLAR_GATE_OPTIONS",
    "PILLAR_GATE_QUESTION",
    "GatedProduct",
    "PillarCandidate",
    "PillarGate",
    "attach_pillars",
    "INTENT_OPTIONS",
    "TREND_GATE_QUESTION",
    "GatedKeyword",
    "TrendCandidate",
    "TrendGateError",
    "TrendIntentGate",
    "load_trend_candidates",
    "write_gated_deliverable",
]
