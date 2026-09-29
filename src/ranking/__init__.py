"""Step 6 (updated multi-step pipeline): Jev product ranking.

Ranks the Step-5 gold-kernel packages (`optimal-dropship-candidates/…`,
written by the dual-supplier ingestion) against the Step-3 gold-standard
product list, using Jev — TypeSafe's System One model, hosted on OpenRouter.

Jev is a **product ranker here, never a keyword gate**: the retired two-tier
keyword gate (spike §3) is not rebuilt. The ranker emits a tiered report
(shortlist / review / disregard) and touches no package directory.

* `jev_client.py` — the System One transport and every judgement constant
  (question texts, level descriptions, pillar options, tier thresholds).
* `jev_product_ranker.py` — package collection, batch assembly, fan-in and
  the tiered report.
"""

from __future__ import annotations

from src.ranking.jev_client import (
    JevClient,
    JevConfigError,
    JevError,
)
from src.ranking.jev_product_ranker import (
    JevProductRanker,
    JevRankingError,
    RankedPackage,
)

__all__ = [
    "JevClient",
    "JevConfigError",
    "JevError",
    "JevProductRanker",
    "JevRankingError",
    "RankedPackage",
]
