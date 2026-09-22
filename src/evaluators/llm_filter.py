"""LLM viability & marketing evaluator (Phase 4, rewritten per plan F.4).

Supplier-First pipeline: the input is a verified `RawSupplierProduct`
(real listing, real price, real gallery) — no ad copy, no invented COGS,
no guessed links. The LLM's entire job is:

- the ACCEPT/REJECT viability verdict for the Australian market,
- niche/problem/saturation classification,
- marketing payload: `marketing_ad_copy`, `key_features`,
  `shipping_notice_au`, `target_tags`,
- a `suggested_retail_aud` that clears the margin floor against the
  supplier's REAL listed cost.

Anti-hallucination contract (carried from CLAUDE.md §3.5):
- `supplier_name` / `supplier_retail_url` / `estimated_cogs_aud` /
  `cogs_estimation_basis` are never in the response model — the final
  `ProductCandidateEvaluation` is built by `ProductCandidateEvaluation.from_raw`
  in code, immediately after the call.
- The prompt never includes image URLs and never asks the model to
  produce one; all imagery flows from `RawSupplierProduct.image_urls`
  through `image_sourcing.py` deterministically.
- The LLM's arithmetic is never trusted: margin/markup are recomputed in
  code from the real supplier price, and an ACCEPT whose reconciled
  figures fail the margin floor (`MIN_MARKUP_MULTIPLIER` OR
  `MIN_MARGIN_AUD`) is downgraded to REJECT before the final model is
  constructed.
- Schema validation failures exhaust instructor's retry budget and
  propagate to the orchestrator, which counts them as
  `dropped_llm_validation_failed` — never exported.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

import instructor
from pydantic import ValidationError
from openai import AsyncOpenAI

from src.config import settings
from src.models import (
    ProductCandidateEvaluation,
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)

logger = logging.getLogger(__name__)


class LLMConfigError(Exception):
    """LLM endpoint configuration is missing or incomplete (`.env` keys)."""


_SYSTEM_PROMPT = """You are an expert dropshipping evaluator for a premium \
Australian store in the "Modern Arab-Aussie Lifestyle & Cultural Nostalgia" \
niche — home, hospitality and everyday-ceremony products that give \
Arabic-speaking Australians and their families a stronger sense of home, \
heritage, and generous hosting. Review the provided raw supplier product \
details (title, description, and the supplier's REAL dropshipping cost in \
AUD) and determine whether the product is viable. Reply with a structured \
verdict.

The cost you are given (`price_aud` + `shipping_cost_aud`) is the STRICT, \
landed dropshipping cost read from the AliExpress Dropshipping Center for \
the Australian market. Treat it as accurate and final: do not invent a \
cheaper basis, do not discount it, and do not assume a promotional or \
new-customer price underlies it.

Viability gates (adapted to real supplier data):
1. PROBLEM SOLVER OR EMOTIONAL TRIGGER: the product solves an active \
discomfort (ergonomics, clutter, daily friction) or serves a high-passion \
enthusiast niche (kitchen and home barista, hospitality and entertaining, \
tea and coffee ceremony, prayer and household ritual, pets, \
outdoor/fitness).
2. MARGIN VIABILITY (AUSTRALIAN PRICING): do NOT mechanically apply a \
fixed 3x-4x multiplier to the cost. Price the product at what it \
REALISTICALLY sells for in Australia in this niche — what a shopper would \
happily pay for a considered, well-presented premium item, not a Kmart \
commodity. Then check it clears the floor: `suggested_retail_aud` must be \
at least 2.5x the landed cost OR leave at least AUD $20 gross profit per \
unit. If the realistic Australian price cannot clear that floor, REJECT \
the product on this gate rather than inflating the price.
3. AUSTRALIAN LOGISTICAL FEASIBILITY: light and durable is best (under \
1.2 kg, no fragile untreated glass/ceramics, non-perishable, air-freight \
compliant — no loose battery hazmat restrictions). Infer physical traits \
from the title/description only; never invent specifications.
4. LOCAL SATURATION RESISTANCE: not an everyday commodity readily bought \
at Kmart, Target, Bunnings, Big W, or Woolworths.
5. DEMONSTRATION APPEAL: a clear visual problem-solution dynamic that can \
be communicated within 3 seconds of video or carousel imagery.

Verdict rules:
- A product failing TWO OR MORE points MUST be "REJECT".
- Passing 4 or 5 points (or 3 strong points with one weak) -> "ACCEPT".
- Do not ACCEPT listings that are not a shippable physical product (e.g. \
digital goods, spare parts with unclear fit, wholesale bundles).

If ACCEPT, write the marketing payload:
- `marketing_ad_copy`: compelling shopper-facing copy for the Australian \
market in this niche, grounded ONLY in the supplied product data.
- `key_features`: 3-5 concrete marketing bullets derived ONLY from the \
title/description — never invent specifications.
- `shipping_notice_au`: a realistic customer-facing shipping line for \
standard tracked international shipping to Australia (7-12 business days).
- `suggested_retail_aud`: the realistic Australian retail price described \
in gate 2, not a multiplier-derived figure.
- `target_tags`: shopper/shopify tags, and ALWAYS include "dropship".
- `niche_category`, `problem_solved`, `saturation_risk` as instructed.

HARD RULES:
- Do NOT output any URL, hyperlink, domain, or marketplace item ID \
anywhere — supplier data is attached programmatically, and any link you \
emit is by definition fabricated.
- All monetary fields in AUD.
"""


def _build_user_message(raw: RawSupplierProduct) -> str:
    """Render the verified supplier product as the evaluation payload.

    Deliberately contains NO image URLs — the LLM never sees, authors,
    edits, or "upgrades" media URLs.
    """
    payload = {
        "supplier_name": raw.supplier_name,
        "product_title": raw.product_title,
        "product_description": raw.product_description[:4000],
        "price_aud": raw.price_aud,
        "shipping_cost_aud": raw.shipping_cost_aud,
        "supplier_retail_url": raw.supplier_retail_url,
        "target_country": "AU",
    }
    return (
        "Evaluate this verified supplier product for the Australian "
        "dropshipping store:\n\n" + json.dumps(payload, indent=2)
    )


def build_messages(raw: RawSupplierProduct) -> List[dict]:
    """Chat messages for one evaluation (system gate + product payload)."""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(raw)},
    ]


def _landed_cogs(raw: RawSupplierProduct) -> float:
    return round(raw.price_aud + raw.shipping_cost_aud, 2)


def _reconcile(
    evaluation: ProvisionalProductEvaluation, raw: RawSupplierProduct
) -> ProvisionalProductEvaluation:
    """Recompute the margin math against the supplier's REAL cost.

    The LLM's verdict is trusted (it applies the viability gate), but its
    arithmetic is not: landed COGS is the actual listed price + shipping,
    so if its retail suggestion fails the margin floor (markup >=
    `MIN_MARKUP_MULTIPLIER` OR margin > `MIN_MARGIN_AUD`), the ACCEPT is
    downgraded to REJECT — on true numbers the product failed the gate,
    whatever the model claimed.
    """
    if evaluation.verdict != "ACCEPT":
        return evaluation
    cogs = _landed_cogs(raw)
    margin = round(evaluation.suggested_retail_aud - cogs, 2)
    markup = round(evaluation.suggested_retail_aud / cogs, 2) if cogs > 0 else 0.0
    if not (
        markup >= settings.MIN_MARKUP_MULTIPLIER
        or margin > settings.MIN_MARGIN_AUD
    ):
        logger.info(
            "Downgraded ACCEPT to REJECT for %r: reconciled margin %.2f AUD "
            "/ markup %.2fx fails the margin floor against the real listed "
            "cost %.2f AUD",
            raw.product_title, margin, markup, cogs,
        )
        evaluation.verdict = "REJECT"
    return evaluation


class LLMEvaluationFilter:
    """Streams verified supplier products through the LLM viability gate."""

    def __init__(
        self,
        client: Optional[Any] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> None:
        self.temperature = temperature
        self.max_retries = max_retries
        # An injected client (tests, alternative transports) skips config
        # resolution entirely; only the model name is still required.
        if client is not None:
            self.client = client
            self.model = model or settings.LLM_MODEL
            if not self.model:
                raise LLMConfigError(
                    "LLM_MODEL is not configured (set LLM_MODEL in .env)"
                )
            return

        base_url = base_url or settings.LLM_BASE_URL
        api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", base_url),
                ("LLM_API_KEY", api_key),
                ("LLM_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise LLMConfigError(
                f"Missing required LLM configuration in .env: {', '.join(missing)}"
            )

        # instructor patches the AsyncOpenAI client so `create(..., response_model=)`
        # returns a validated ProvisionalProductEvaluation instead of raw text.
        # NOTE: do NOT pass `max_retries` here — with instructor 1.16's v2
        # registry + openai 2.x, a constructor-level max_retries is injected
        # into every request kwargs AND bound by the patched create_fn,
        # producing "got multiple values for keyword argument 'max_retries'".
        # Schema-validation retries are set per call in `evaluate()` instead.
        self.client = instructor.from_openai(
            AsyncOpenAI(base_url=base_url, api_key=api_key),
        )

    async def evaluate(
        self, raw: RawSupplierProduct
    ) -> ProductCandidateEvaluation:
        """Evaluate one verified product; returns the FINAL structured verdict.

        Flow (plan F.4/F.5): the LLM authors the provisional marketing/
        viability fields; the margin math is reconciled against the real
        supplier cost; and `ProductCandidateEvaluation.from_raw` maps the
        programmatic supplier/COGS fields in code. Raises when the LLM
        call fails or the output stays schema-invalid after instructor's
        bounded retries — the orchestrator counts those as
        `dropped_llm_validation_failed`.
        """
        logger.info(
            "Evaluating product %r (%s, listed %.2f AUD)",
            raw.product_title, raw.supplier_name, raw.price_aud,
        )
        try:
            provisional: ProvisionalProductEvaluation = (
                await self.client.chat.completions.create(
                    model=self.model,
                    messages=build_messages(raw),
                    response_model=ProvisionalProductEvaluation,
                    temperature=self.temperature,
                    # instructor's own retry loop for schema-invalid model
                    # output (distinct from openai transport retries).
                    # Per-call because the constructor-level kwarg collides —
                    # see __init__ note.
                    max_retries=self.max_retries,
                )
            )
        except Exception as exc:
            # Log the offending raw LLM output when instructor exposes it,
            # for later prompt tuning — then let the orchestrator drop the
            # candidate.
            raw_completion = getattr(exc, "last_completion", None)
            if raw_completion is not None:
                logger.warning(
                    "LLM output failed schema validation after retries "
                    "(last completion: %s)",
                    raw_completion,
                )
            raise

        provisional = _reconcile(provisional, raw)

        # The final evaluation is constructed in code: supplier/cost fields
        # mapped from the raw product, margin math recomputed. A margin-gate
        # failure here (schema validator) can only be an ACCEPT the
        # reconcile step already downgraded — treat it as a REJECT fallback.
        try:
            final = ProductCandidateEvaluation.from_raw(provisional, raw)
        except ValidationError:
            provisional.verdict = "REJECT"
            logger.warning(
                "Final evaluation construction failed for %r after "
                "reconciliation — downgraded to REJECT",
                raw.product_title,
            )
            final = ProductCandidateEvaluation.from_raw(provisional, raw)

        logger.info(
            "Evaluated %r -> verdict=%s margin_aud=%.2f markup=%.2fx "
            "saturation=%s",
            raw.product_title, final.verdict, final.estimated_margin_aud,
            final.markup_multiplier, final.saturation_risk,
        )
        return final