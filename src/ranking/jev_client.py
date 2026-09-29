"""Step 6 — the Jev (TypeSafe System One) client and its judgement constants.

System One, in TypeSafe's own words, "evaluates one state against many
independent typed questions". One `state` (a string, object or array of text)
is shared by every question in the call, and each question is a caller-keyed
entry of one of three types:

* `noul`   — a yes/no probability in [0, 1] ("near 1 is a strong yes, near 0 a
             strong no, near 0.5 uncertain").
* `choice` — pick one option from a caller-supplied map; the answer carries
             `choice`, `probabilities` and `confidence`.
* `score`  — a position along an ORDERED list of level descriptions; the
             answer carries `score`, `legend`, `probabilities` and `confidence`.

Envelope (verified against the vendor docs 2026-09-27 and re-confirmed by a
live one-call probe on 2026-09-29):

    POST {OPENROUTER_BASE_URL}/systemone
    Authorization: Bearer <OPENROUTER_API_KEY>
    { "model": "typesafe/jev-1.13",
      "state": <string | object | array>,
      "questions": { "<id>": { "type", "instructions", "criteria" }, ... } }

    -> { "answers": { "<id>": <typed answer>, ... },
         "usage": { "input_tokens", "output_tokens", "cost" },
         "model", "id", "provider" }

References:
* https://docs.typesafe.ai/concepts/system-one
* https://docs.typesafe.ai/concepts/state
* https://docs.typesafe.ai/primitives
* https://docs.typesafe.ai/primitives/advanced
* https://openrouter.ai/docs/guides/community/jev
* https://openrouter.ai/docs/api/api-reference/systemone/submit-a-system-one-request

Probe findings that shaped this module (2026-09-29, one call, USD 0.000031):

1. The response is exactly the documented envelope — `answers` sits at the
   top level, with no wrapper. `decide()` is therefore the single parse seam
   if TypeSafe ever nests it.
2. **`score` is a 0-BASED position.** For a 5-entry criteria list the returned
   `score` lands in [0, 4] and the `legend` keys run `"0".."4"`, so the first
   level scores 0, not 1. `answer_score()` shifts the position by
   `JEV_SCORE_INDEX_SHIFT` (= 1) onto the 1–5 scale the tier thresholds
   (`SHORTLIST_MIN_SCORE` / `REVIEW_MIN_SCORE`, plan §8.0/§8.3) are written
   for — so `SHORTLIST_MIN_SCORE = 4.0` means "close match or better", which
   is the plan's intent, rather than "literally the maximum level".
3. Scores are genuinely fractional (a probe answer came back `3.24`, between
   levels 3 and 4) and `confidence` is a separate, independent number.
4. Batching is native and cheap: TypeSafe cites ~13 questions in one call as
   ~11.5x cheaper and ~9.6x faster than separate calls. `JEV_BATCH_SIZE`
   (default 4) is sized to that envelope at three questions per product.

Every judgement the ranker applies lives in this module as an importable
constant — question texts, the similarity and value level lists, the pillar
options, and the two tier thresholds — so the operator edits judgement in one
place rather than hunting through the ranker.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import httpx

from src.config import settings

#: System One calls are a single synchronous round trip; the ranker issues one
#: per batch, so a 60 s ceiling matches the keywords generator's posture.
JEV_TIMEOUT_SECONDS = 60.0

#: Jev reports a score POSITION that is 0-based along the criteria list; the
#: tier thresholds are written on a 1–5 scale, so parsed scores are shifted by
#: this much (probe finding 2, module docstring).
JEV_SCORE_INDEX_SHIFT = 1.0

# --- Judgement constants (edit judgement here, nowhere else) ---------------

#: Similarity levels, ordered worst -> best. The top level is the updated
#: pipeline's bar: a near-identical match to a proven gold-standard product's
#: kind/material/mechanism/use, not merely "a product in the same aisle".
SIMILARITY_LEVELS: List[str] = [
    "unrelated product category",
    "same broad category but a different mechanism",
    "same product type with notable differences in material, mechanism or use",
    "close match to a gold-standard product in kind and use",
    (
        "identical or near-identical match to a gold-standard product's kind, "
        "material, mechanism and use"
    ),
]

#: Winning-value levels, ordered worst -> best. Margin/markup are the real
#: figures carried in a package's `metadata.json`; demand evidence is what the
#: gold reference records. The bar wants BOTH: strong AU demand evidence AND
#: strong gated economics, never one propping up the other.
VALUE_LEVELS: List[str] = [
    "no sale evidence and weak economics",
    "weak evidence and marginal economics",
    "moderate demand evidence or acceptable economics",
    "good demand evidence and strong gated economics",
    "strong AU demand evidence plus strong gated economics",
]

#: Pillar options for the per product choice question, keyed by the pillar
#: value used across the pipeline; `discard` is a real verdict, not an error.
PILLAR_OPTIONS: Dict[str, str] = {
    "curated_home": "home, kitchen or lifestyle goods for the modern day to day",
    "self_care_rituals": "personal care, grooming and wellbeing ritual tools",
    "other": "a genuine product that fits neither named pillar",
    "discard": (
        "fails the store's boundaries: a cosmetic or consumable, celebrity or "
        "cultural-icon IP, or an item that cannot be air-freighted to Australia"
    ),
}

#: Per product question templates. `{slug}` is substituted by the ranker with
#: the package's state key; instructions reference the shared state by
#: backticked paths, per TypeSafe's guidance.
QUESTIONS: Dict[str, Dict[str, Any]] = {
    "similarity": {
        "type": "score",
        "instructions": (
            "Compare `products.{slug}` with every product in `gold_reference`. "
            "How closely does it match the kind, material, mechanism and use "
            "of the closest gold-standard product? Judge product equivalence "
            "only — not price, demand or marketing."
        ),
        "criteria": SIMILARITY_LEVELS,
    },
    "winning_value": {
        "type": "score",
        "instructions": (
            "Judge the commercial value of `products.{slug}` from its own "
            "suggested retail price, landed cost and projected margin, weighed "
            "against the demand evidence recorded for comparable products in "
            "`gold_reference`. Strong margin alone is not demand: a product "
            "with no demand evidence for its type scores low however healthy "
            "its margin."
        ),
        "criteria": VALUE_LEVELS,
    },
    "pillar": {
        "type": "choice",
        "instructions": (
            "Classify `products.{slug}` into exactly one pillar. Choose "
            "`discard` when it fails the store's boundaries — a cosmetic or "
            "consumable, a product carrying celebrity or cultural-icon IP, or "
            "an item that cannot be air-freighted to Australia."
        ),
        "criteria": PILLAR_OPTIONS,
    },
}

#: Tier boundaries on `rank_score` (1–5 scale). The runtime values are
#: env-driven (`JEV_SHORTLIST_MIN_SCORE` / `JEV_REVIEW_MIN_SCORE`, plan §9);
#: these module constants expose the same values for imports and tests.
SHORTLIST_MIN_SCORE: float = settings.JEV_SHORTLIST_MIN_SCORE
REVIEW_MIN_SCORE: float = settings.JEV_REVIEW_MIN_SCORE


class JevError(Exception):
    """A System One call failed, or its response was unusable."""


class JevConfigError(JevError):
    """Jev endpoint configuration is missing or incomplete (`.env` keys)."""


class JevClient:
    """Sync OpenRouter System One client (TypeSafe `jev-1.13`).

    Same posture as the keywords generator: a plain `httpx.Client`, the
    endpoint settled in `__init__` so a bad `.env` fails fast, and an injected
    client for tests / alternative transports that skips the config
    requirement (only the model name is still required).
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.OPENROUTER_API_KEY
        self.base_url = (base_url or settings.OPENROUTER_BASE_URL).rstrip("/")
        self.model = model or settings.JEV_MODEL
        if client is not None:
            self.client = client
            self.base_url = self.base_url or "http://jev.invalid"
            if not self.model:
                raise JevConfigError(
                    "JEV_MODEL is not configured (set JEV_MODEL in .env)"
                )
        else:
            missing = [
                name
                for name, value in (
                    ("OPENROUTER_BASE_URL", self.base_url),
                    ("OPENROUTER_API_KEY", self.api_key),
                    ("JEV_MODEL", self.model),
                )
                if not value
            ]
            if missing:
                raise JevConfigError(
                    "Missing required Jev configuration in .env: "
                    f"{', '.join(missing)} (set OPENROUTER_API_KEY to your "
                    "OpenRouter key when Step 6 starts)"
                )
            self.client = httpx.Client(timeout=JEV_TIMEOUT_SECONDS)

    def decide(
        self, state: Any, questions: Mapping[str, Mapping[str, Any]]
    ) -> Dict[str, dict]:
        """One System One call; returns the `answers` map keyed by question id.

        Raises `JevError` on a transport failure, a non-200 status, a
        non-JSON body, or a response that carries no `answers` object — the
        single parse seam if the vendor ever changes the envelope.
        """
        payload = {"model": self.model, "state": state, "questions": dict(questions)}
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ahmadammari/dropship-scout-agent",
            "X-Title": "Dropship Scout Agent",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.client.post(
                f"{self.base_url}/systemone", headers=headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise JevError(f"System One transport error: {exc}") from exc
        if response.status_code != 200:
            raise JevError(
                f"System One HTTP {response.status_code}: {response.text[:400]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise JevError(
                f"System One returned a non-JSON body: {response.text[:400]!r}"
            ) from exc
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise JevError(
                f"System One response carries no `answers` object: {str(data)[:400]}"
            )
        return answers

    # --- Typed question builders (the §8.0 schema, assembled for callers) ---

    @staticmethod
    def score(instructions: str, levels: List[str]) -> Dict[str, Any]:
        """A score question: an ordered list of level descriptions."""
        return {
            "type": "score",
            "instructions": instructions,
            "criteria": list(levels),
        }

    @staticmethod
    def choice(instructions: str, options: Dict[str, str]) -> Dict[str, Any]:
        """A choice question: a map of option key -> one-line description."""
        return {
            "type": "choice",
            "instructions": instructions,
            "criteria": dict(options),
        }

    @staticmethod
    def noul(
        instructions: str, definitions: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """A yes/no question; `definitions` optionally clarifies true/false."""
        question: Dict[str, Any] = {"type": "noul", "instructions": instructions}
        if definitions:
            question["criteria"] = dict(definitions)
        return question


# --- Answer readers (share the +1 score normalization; one place) ----------


def answer_score(answer: Any) -> Optional[float]:
    """The 1–5 position from a score answer, or None if unparsable.

    Jev's raw `score` is 0-based along the criteria list (probe finding 2), so
    this shifts it by `JEV_SCORE_INDEX_SHIFT` onto the 1–5 scale the tier
    thresholds read.
    """
    if not isinstance(answer, dict):
        return None
    raw = answer.get("score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) + JEV_SCORE_INDEX_SHIFT


def answer_choice(answer: Any) -> Optional[str]:
    """The selected option key from a choice answer, or None if unparsable."""
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    if isinstance(choice, str) and choice:
        return choice
    return None


def answer_noul(answer: Any) -> Optional[float]:
    """The yes-probability from a noul answer, or None if unparsable."""
    if not isinstance(answer, dict):
        return None
    raw = answer.get("noul")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)
