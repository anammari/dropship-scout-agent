"""Step 3 (updated multi-step pipeline): LLM curation of Google Shopping rows.

The reasoning LLM turns the scraped Google Shopping AU rows into the
GOLD-STANDARD product list — but under the repo's anti-hallucination posture
the LLM is only ever allowed to *select and annotate*: its output references
rows by their exact `url` plus the three judgement fields it owns (`pillar`,
`compliance_note`, `unit_economics_note`). Every product fact in the
deliverable (name, url, price, merchant, demand evidence, keyword
attribution) is assembled by code from the verbatim scraped rows, so a
hallucinated or mistyped url can never become a product — it is dropped by
the code-side join (`curated url has no scraped source row`).

A gold product must carry ON-PAGE demand evidence: rows with neither a
rating nor a review count are filtered out BEFORE the LLM (operator
decision 2026-09-28), so `demand_evidence="not_available_from_source"` can
never reach the deliverable — the Step-2 Trends evidence stays in the
keyword file where it belongs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

import httpx
from pydantic import BaseModel, ValidationError

from src.config import settings
from src.extractors.google_shopping import ShoppingRow

logger = logging.getLogger(__name__)

PILLARS = ("curated_home", "self_care_rituals", "other")

TEMPERATURE = 0.3
#: The configured model (`deepseek-v4.x-flash:cloud`) is a *reasoning* model:
#: its completion budget is shared between a hidden `reasoning` chain-of-thought
#: and the visible `content` answer. A batch that is too large makes the model
#: spend the whole budget thinking and return empty `content` with
#: `finish_reason: "length"` (observed live 2026-09-28 at 60 rows / 8000
#: tokens). The budget is therefore generous and the batch small so the answer
#: always has room after the reasoning.
MAX_TOKENS = 16000
LLM_TIMEOUT_SECONDS = 900.0
ROWS_PER_CALL = 10

SYSTEM_MESSAGE = (
    "You are a senior e-commerce market research expert specializing in the "
    "Australian e-commerce market. You select GOLD-STANDARD product winners "
    "from scraped Google Shopping AU rows: on-demand, high value-proposition, "
    "inert physical goods purchased or sought recently by Australians."
)

USER_TEMPLATE = """### Hard rules

1. You reference rows ONLY by their exact `url` string, copied verbatim. Never
   invent, edit, or complete a url, name, price, or description — the row
   facts are attached by code and do not pass through you.
2. Select only rows that are real consumer products (physical goods), not
   guides, marketplaces, or category pages.
3. Strict boundaries — NO cosmetics, liquids, creams, soaps, bath salts,
   formulated products, or mineral-sourcing claims (no Dead Sea mud and the
   like). NO consumables, even as an accessory. Inert physical tools only. No
   celebrity/cultural-icon IP and no counterfeit-friendly branded goods.
4. Economics: prefer rows whose visible price can plausibly leave at least a
   2.5x markup or AUD $10.00+ gross margin over a landed supplier cost with
   standard AU packet shipping (7-12 business days). When the row gives no
   basis, set `unit_economics_note` to null — never estimate from imagination.
5. `pillar` is exactly one of `curated_home` (functional/aesthetic kitchen,
   preparation, dining/tabletop objects), `self_care_rituals` (non-electric,
   durable wellness, relaxation, bath accessories), or `other` (only when the
   row itself evidences high AU demand).
6. `compliance_note` is one short sentence on why the item is an AICIS-exempt
   inert accessory and IP-clean — only when true; otherwise leave the product
   out entirely.
7. Keep the strongest 1-3 rows per source keyword; skip duplicate listings of
   the same product.
8. Output strict JSON only — no markdown fence, no commentary, and always
   close the JSON (a truncated response is unusable):

   {"products": [{"url": "https://...", "pillar": "curated_home",
   "compliance_note": "...", "unit_economics_note": "... or null"}]}

### Rows (Google Shopping AU, scraped live)

{rows_json}"""


class GoldCurationError(Exception):
    """The curation call failed or its output failed validation."""


class GoldCurationConfigError(GoldCurationError):
    """LLM endpoint configuration is missing or incomplete (`.env` keys)."""


class GoldSelection(BaseModel):
    """One LLM judgement over one scraped row (referenced by verbatim url)."""

    url: str
    pillar: str
    compliance_note: str
    unit_economics_note: Optional[str] = None


class GoldStandardProduct(BaseModel):
    """One row of the Step-3 deliverable (facts are code-assembled)."""

    name: str
    url: str
    description: Optional[str] = None
    retail_price_text: Optional[str] = None
    pillar: str
    compliance_note: str
    demand_evidence: str
    unit_economics_note: Optional[str] = None
    source_keyword: str = ""


def _parse_products(body: str) -> List[dict]:
    """Parse the LLM's {"products": [...]} object, salvaging a truncated tail.

    Same failure mode as the keyword generator: the endpoint can cut a long
    response mid-JSON, so the tail is trimmed back to the last complete entry
    and closed. Anything unsalvageable raises.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", body.strip())
    start, end = text.find("{"), text.rfind("}")
    for candidate in (text, text[start : end + 1] if 0 <= start < end else None):
        if not candidate:
            continue
        try:
            products = json.loads(candidate)["products"]
            if isinstance(products, list):
                return products
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    cut = text
    for _ in range(len(text)):
        idx = cut.rfind("}")
        if idx < 0:
            break
        probe = cut[: idx + 1].rstrip().rstrip(",")
        try:
            return json.loads(f"{probe}\n    ]\n}}")["products"]
        except (json.JSONDecodeError, KeyError, TypeError):
            cut = cut[:idx]
    raise GoldCurationError(
        "LLM curation response could not be parsed as a products JSON "
        "object, even after truncation salvage. Response head: "
        f"{body[:300]!r}"
    )


def _prompt_row(row: ShoppingRow) -> dict:
    """The row as the LLM sees it (demand signals shown for judgement)."""
    return {
        "url": row.url,
        "title": row.title,
        "price": row.price_text,
        "merchant": row.merchant,
        "delivery": row.delivery_text,
        "rating": row.rating,
        "ratingCount": row.rating_count,
        "source_keyword": row.source_keyword,
    }


def _demand_evidence(row: ShoppingRow) -> str:
    """Code-assembled from the scraped row — the LLM never authors this.

    curate() filters rows with neither KPI out pre-LLM, so the final
    `not_available_from_source` branch only guards a direct call with a
    bare row; every product that reaches the deliverable carries at least
    a rating or a review count.
    """
    if row.rating is not None and row.rating_count:
        return (
            f"rating {row.rating:g}/5, {row.rating_count} reviews "
            "(Google Shopping AU)"
        )
    if row.rating is not None:
        return f"rating {row.rating:g}/5 (review count not reported)"
    if row.rating_count:
        return f"{row.rating_count} reviews (Google Shopping AU)"
    # The actor carries no purchased-recently KPI; say so rather than invent.
    return "not_available_from_source"


class GoldProductCurator:
    """Reasoning-LLM curation pass over scraped ShoppingRows."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        rows_per_call: int = ROWS_PER_CALL,
    ) -> None:
        self.rows_per_call = rows_per_call
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        if client is not None:
            # An injected client (tests) skips the config requirement; only
            # the model name is still required.
            self.client = client
            self.base_url = self.base_url or "http://llm.invalid"
            if not self.model:
                raise GoldCurationConfigError(
                    "LLM_MODEL is not configured (set LLM_MODEL in .env)"
                )
            return
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", self.base_url),
                ("LLM_API_KEY", self.api_key),
                ("LLM_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise GoldCurationConfigError(
                f"Missing required LLM configuration in .env: {', '.join(missing)}"
            )
        self.client = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)

    def _call_llm(self, user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": user_message},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise GoldCurationError(f"LLM transport error: {exc}") from exc
        if response.status_code != 200:
            raise GoldCurationError(
                f"LLM HTTP {response.status_code}: {response.text[:400]}"
            )
        data = response.json()
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise GoldCurationError(
                f"LLM response carries no completion content: {str(data)[:400]}"
            ) from exc
        if not content.strip():
            # Distinct from a JSON-parse failure below: the model produced
            # nothing to parse (usually a reasoning model exhausting
            # `max_tokens` on its chain-of-thought before answering).
            raise GoldCurationError(
                "LLM returned empty content "
                f"(finish_reason={finish_reason!r}); a reasoning model likely "
                f"exhausted max_tokens ({MAX_TOKENS}) thinking about the "
                f"batch. Response head: {str(data)[:400]!r}"
            )
        return content

    def curate(self, rows: List[ShoppingRow]) -> List[GoldStandardProduct]:
        """Select + annotate rows into gold-standard products (code-assembled).

        Rows carrying no rating/review KPI are dropped before the LLM: a gold
        product must cite on-page demand evidence, so
        `demand_evidence="not_available_from_source"` can never be emitted.
        """
        if not rows:
            raise GoldCurationError("no scraped rows to curate")
        # Demand-evidence filter (operator decision 2026-09-28): a row with
        # neither a rating nor a review count has no verifiable AU demand
        # signal and is filtered out pre-LLM, saving the reasoning tokens too.
        demand_rows = [
            row for row in rows if row.rating is not None or row.rating_count
        ]
        dropped_no_kpi = len(rows) - len(demand_rows)
        if dropped_no_kpi:
            logger.info(
                "demand-evidence filter: dropped %d row(s) with no "
                "rating/review KPI before the LLM", dropped_no_kpi,
            )
        if not demand_rows:
            raise GoldCurationError(
                "every scraped row lacks rating/review KPIs — no gold "
                "product could carry on-page demand evidence"
            )
        products: List[GoldStandardProduct] = []
        seen: set = set()
        dropped_at_join = 0
        for start in range(0, len(demand_rows), self.rows_per_call):
            batch = demand_rows[start : start + self.rows_per_call]
            batch_urls = {row.url.strip(): row for row in batch}
            user_message = USER_TEMPLATE.replace(
                "{rows_json}",
                json.dumps([_prompt_row(r) for r in batch], ensure_ascii=False),
            )
            # One retry per batch: a ~120-call production run meets the
            # reasoning model's flaky empty-content mode eventually, and a
            # single flaky batch must not void the whole run.
            try:
                content = self._call_llm(user_message)
            except GoldCurationError as exc:
                logger.warning("curation batch failed (%s); retrying once", exc)
                content = self._call_llm(user_message)
            for raw in _parse_products(content):
                except (TypeError, ValidationError) as exc:
                    dropped_at_join += 1
                    logger.warning(
                        "dropping uncoercible curation entry %r: %s",
                        str(raw)[:120], exc,
                    )
                    continue
                url = selection.url.strip()
                if selection.pillar not in PILLARS:
                    dropped_at_join += 1
                    logger.warning(
                        "dropping curated product %r: unknown pillar %r",
                        url, selection.pillar,
                    )
                    continue
                row = batch_urls.get(url)
                if row is None:
                    dropped_at_join += 1
                    # The anti-hallucination join: an LLM-invented or edited
                    # url never becomes a product.
                    logger.warning(
                        "dropping curated product %r: url has no scraped "
                        "source row", url,
                    )
                    continue
                if url in seen:
                    continue
                seen.add(url)
                products.append(
                    GoldStandardProduct(
                        name=row.title,
                        url=row.url,
                        description=row.description,
                        retail_price_text=row.price_text,
                        pillar=selection.pillar,
                        compliance_note=selection.compliance_note,
                        demand_evidence=_demand_evidence(row),
                        unit_economics_note=selection.unit_economics_note,
                        source_keyword=row.source_keyword,
                    )
                )
        if dropped_at_join:
            logger.warning(
                "curation join dropped %d entr%s with no exact scraped source",
                dropped_at_join, "y" if dropped_at_join == 1 else "ies",
            )
        logger.info(
            "curation: %d gold product(s) from %d row(s) with demand "
            "evidence (%d raw row(s), %d filtered pre-LLM)",
            len(products), len(demand_rows), len(rows), dropped_no_kpi,
        )
        return products