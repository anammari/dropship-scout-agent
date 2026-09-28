"""Step 4 (updated multi-step pipeline): GOLD-STANDARD keyword generation.

The reasoning LLM turns the Step-3 GOLD-STANDARD product list into the 50–70
supplier search keywords — the "gold-standard keyword bank" — that Step 5
ingests through BOTH dropship supplier pipelines (CJdropshipping MCP and the
AliExpress Dropshipping Center, each with its existing gates) and that Step 6
later ranks the ingested products against.

The prompt is a tracked package resource (`gold_keyword_prompt.md`) whose
product table is a **template slot**: `{PRODUCT_TABLE}` is rendered at runtime
from the live Step-3 deliverable (`outputs/step-3-gold-standard-products.json`,
path from `settings.GOLD_PRODUCTS_PATH`), so the gold products are ground truth
the prompt file never hardcodes. The generator then runs the table in batches
(default 4 products per call). Batching exists because the endpoint truncates
long responses — the reasoning model's hidden thinking consumes a variable
share of the output budget, so a 70-keyword single response came back cut
mid-JSON (`finish_reason=length`). A response that is still truncated is
salvaged by trimming the JSON tail back to the last complete row rather than
losing the batch or spending another call.

Anti-hallucination posture: the LLM only authors keyword rows against the
product table it was handed, and every structural rule — broad/modifier
pairing, pillar membership, the AICIS banned-token boundary, the 50–70 band,
per-product coverage — is re-validated in code after the call. A pool that
fails any rule raises `KeywordGenerationError` instead of flowing on to
Step 5.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel, ValidationError

from src.config import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "gold_keyword_prompt.md"

MIN_KEYWORDS = 50
MAX_KEYWORDS = 70
DEFAULT_BATCH_SIZE = 4
#: The plan's Step-3 deliverable targets 8–20 products (§5.4), sized so the
#: 50–70 pool can cover every product at ≥3 each. The production Step-3 run
#: curated 263, which no 50–70 pool can cover, so the generator keeps the
#: strongest `max_products` of them (demand-ranked, pillar-balanced, deduped
#: by name) and reports the selection. The Step-3 deliverable itself is
#: untouched — it stays the full research record.
DEFAULT_MAX_PRODUCTS = 12
#: The pool size the adaptive per-product target aims at.
TARGET_TOTAL = 60
PER_PRODUCT_MIN_FLOOR = 2
PER_PRODUCT_MIN_CEILING = 9
TEMPERATURE = 0.6
#: The configured model (`deepseek-v4.x-flash:cloud`) is a *reasoning* model:
#: its completion budget is shared between a hidden `reasoning` chain-of-thought
#: and the visible `content` answer, so the budget must leave room for the
#: answer after the thinking (the sibling Step-3 curator hit empty content with
#: `finish_reason="length"` at 8,000 tokens on 2026-09-28).
MAX_TOKENS = 16000
LLM_TIMEOUT_SECONDS = 900.0

PILLARS = ("curated_home", "self_care_rituals", "other")
ROLES = ("broad", "modifier")
FIELDS = ("keyword", "product", "pillar", "role", "tightens", "rationale")

#: The rendered product table's columns. Every cell is copied verbatim from
#: the Step-3 deliverable — the LLM never authors a product fact.
TABLE_HEADER = (
    "| # | Product | Pillar | AU demand evidence | "
    "Physical attributes from the listing | Retail price |"
)
TABLE_SEPARATOR = "| --- | --- | --- | --- | --- | --- |"
TABLE_COLUMNS = ("name", "pillar", "demand_evidence", "attributes", "retail_price_text")

# The prompt's hard AICIS boundary (requirement 5) and commodity-replacement
# exclusion (requirement 6), enforced in code: any keyword carrying one of
# these substrings fails pool validation, so a violating keyword can never
# reach a supplier search.
BANNED_TOKENS = (
    "dead sea", "diatomaceous", "jade", "quartz", "crystal", "mud", "salt",
    "soap", "cream", "lotion", "serum", "gel", "shampoo bar", "supplement",
    "vitamin", "oil", "kmart", "bunnings", "coles", "woolworths", "big w",
    "toilet",
)

_DEMAND_RATING_RE = re.compile(r"rating\s+(\d+(?:\.\d+)?)\s*/\s*5", re.I)
_DEMAND_REVIEWS_RE = re.compile(r"([\d,]+)\s*reviews?", re.I)


class KeywordGenerationError(Exception):
    """A batch failed, or the merged pool broke a structural rule."""


class KeywordGenerationConfigError(KeywordGenerationError):
    """LLM endpoint configuration is missing or incomplete (`.env` keys)."""


class CandidateKeyword(BaseModel):
    """One validated row of the keyword bank (the prompt's output schema)."""

    keyword: str
    product: str
    pillar: Literal["curated_home", "self_care_rituals", "other"]
    role: Literal["broad", "modifier"]
    tightens: Optional[str] = None
    rationale: str


@dataclass
class PromptParts:
    """The prompt markdown split into its message parts.

    The user body is a TEMPLATE: it carries a `{PRODUCT_TABLE}` slot, which
    the generator substitutes with the table rendered from the live Step-3
    deliverable. No product is read out of the prompt file.
    """

    system: str
    user_template: str
    requirements: str


def load_prompt_parts(path: Path = PROMPT_PATH) -> PromptParts:
    """Split the prompt markdown into message parts (template + slot)."""
    text = path.read_text(encoding="utf-8")
    try:
        system = (
            text.split("## System message", 1)[1]
            .split("## User message", 1)[0]
            .strip()
        )
        user_template = text.split("## User message", 1)[1].split(
            "### Requirements", 1
        )[0]
        requirements = "### Requirements" + text.split("### Requirements", 1)[1]
    except IndexError as exc:
        raise KeywordGenerationError(
            f"prompt file {path.name} is missing its message markers: {exc}"
        ) from exc
    if "{PRODUCT_TABLE}" not in user_template:
        raise KeywordGenerationError(
            f"prompt file {path.name} carries no {{PRODUCT_TABLE}} slot in its "
            "user message"
        )
    return PromptParts(
        system=system,
        user_template=user_template.strip(),
        requirements=requirements,
    )


def _cell(value: object) -> str:
    """One table cell: the deliverable's value verbatim, `null` -> em dash."""
    if value is None:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    # A stray cell separator or newline would break the markdown table the
    # model reads, so they are collapsed rather than passed through.
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


def build_product_table(products: Sequence[Dict]) -> List[str]:
    """Render the gold deliverable's rows into the prompt's markdown table.

    Returns the header, the separator and one line per product. Every cell
    is copied verbatim from the deliverable's own fields (`null` -> `—`), so
    the table is evidence the LLM reads, never text it may rewrite.
    """
    lines = [TABLE_HEADER, TABLE_SEPARATOR]
    for index, product in enumerate(products, 1):
        # `description` is the listing's own physical-attribute text, but the
        # production Step-3 run left it null on every row; the compliance note
        # (present on every row) is the remaining listing-derived description
        # of the object, so it stands in rather than rendering a column of —.
        attributes = product.get("description") or product.get("compliance_note")
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    _cell(product.get("name")),
                    _cell(product.get("pillar")),
                    _cell(product.get("demand_evidence")),
                    _cell(attributes),
                    _cell(product.get("retail_price_text")),
                ]
            )
            + " |"
        )
    return lines


def parse_demand_evidence(text: Optional[str]) -> Tuple[int, float]:
    """(reviews, rating) parsed from a code-assembled demand-evidence string."""
    reviews = 0
    rating = 0.0
    if text:
        match = _DEMAND_REVIEWS_RE.search(str(text))
        if match:
            reviews = int(match.group(1).replace(",", ""))
        match = _DEMAND_RATING_RE.search(str(text))
        if match:
            rating = float(match.group(1))
    return reviews, rating


def load_gold_products(path: Optional[Path] = None) -> List[Dict]:
    """Read the Step-3 deliverable's products (fail closed, remediate).

    A missing, malformed or empty file is a hard error naming the fix —
    Step 4 has no ground truth without it.
    """
    resolved = Path(path) if path is not None else Path(settings.GOLD_PRODUCTS_PATH)
    if not resolved.exists():
        raise KeywordGenerationError(
            f"gold-standard product file not found: {resolved}; run Step 3 "
            "first (scripts/run_gold_standard_research.py)"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeywordGenerationError(
            f"gold-standard product file {resolved} could not be read as "
            f"JSON: {exc}; run Step 3 first "
            "(scripts/run_gold_standard_research.py)"
        ) from exc
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list) or not products:
        raise KeywordGenerationError(
            f"gold-standard product file {resolved} carries no products; run "
            "Step 3 first (scripts/run_gold_standard_research.py)"
        )
    named = [p for p in products if isinstance(p, dict) and str(p.get("name") or "").strip()]
    if not named:
        raise KeywordGenerationError(
            f"gold-standard product file {resolved} carries no named "
            "products; run Step 3 first "
            "(scripts/run_gold_standard_research.py)"
        )
    return named


def carries_banned_token(text: object) -> Optional[str]:
    """The first AICIS boundary token inside `text`, else None."""
    lowered = str(text or "").lower()
    return next((token for token in BANNED_TOKENS if token in lowered), None)


def select_gold_products(
    products: Sequence[Dict], limit: int = DEFAULT_MAX_PRODUCTS
) -> List[Dict]:
    """The strongest `limit` products: demand-ranked, pillar-balanced, deduped.

    Deterministic. Products whose own NAME carries an AICIS boundary token
    (jade/quartz/salt tools and the like) are not eligible: their honest
    supplier keyword is the banned token itself, so no keyword could both
    match the product and clear the code's fatal boundary rule. They stay in
    the Step-3 deliverable — Step 4 simply cannot name them. The rest are
    ordered by review volume, then rating, then name; listings naming the
    same product are collapsed to their strongest row; and the selection then
    round-robins across pillars so a capped table keeps every pillar
    represented (the plan targets 8–20 products; the production Step-3 run
    curated 263).
    """
    ranked: Dict[str, Dict] = {}
    for product in products:
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        if carries_banned_token(name):
            continue
        if name not in ranked:
            ranked[name] = product
    ordered = sorted(
        ranked.values(),
        key=lambda p: (
            -parse_demand_evidence(p.get("demand_evidence"))[0],
            -parse_demand_evidence(p.get("demand_evidence"))[1],
            str(p.get("name")),
        ),
    )
    if limit <= 0 or len(ordered) <= limit:
        return ordered
    by_pillar: Dict[str, List[Dict]] = defaultdict(list)
    for product in ordered:
        by_pillar[str(product.get("pillar"))].append(product)
    pillars = [p for p in PILLARS if by_pillar.get(p)]
    pillars += [p for p in by_pillar if p not in PILLARS]
    picked: List[Dict] = []
    while len(picked) < limit:
        advanced = False
        for pillar in pillars:
            pool = by_pillar.get(pillar) or []
            if pool:
                picked.append(pool.pop(0))
                advanced = True
                if len(picked) == limit:
                    break
        if not advanced:
            break
    return picked


def per_product_target(product_count: int) -> int:
    """Adaptive per-product keyword target: `ceil(60 / products)` in [3, 9].

    The floor is lifted above the clamp only when a small gold set could not
    otherwise reach the 50-keyword band the validator enforces (below six
    products) — the same instruction the LLM is given, so a pool that
    follows the note is a pool the code accepts.
    """
    if product_count <= 0:
        return 0
    target = min(
        PER_PRODUCT_MIN_CEILING, max(3, math.ceil(TARGET_TOTAL / product_count))
    )
    return max(target, math.ceil(MIN_KEYWORDS / product_count))


def default_per_product_min(product_count: int) -> int:
    """Validator's per-product floor: `max(2, 50 // products - 1)`."""
    if product_count <= 0:
        return PER_PRODUCT_MIN_FLOOR
    return max(PER_PRODUCT_MIN_FLOOR, MIN_KEYWORDS // product_count - 1)


def _direct_candidates(text: str) -> List[str]:
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def parse_llm_keywords(body: str) -> Tuple[List[dict], bool]:
    """Parse one batch response, salvaging a truncated JSON tail.

    Returns `(rows, salvaged)`. Salvage trims the text back to the last
    complete keyword row and closes the array — the response was cut by the
    output limit mid-array, so the surviving rows are valid and no extra
    LLM call is spent.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", body.strip())
    for candidate in _direct_candidates(text):
        try:
            return json.loads(candidate)["keywords"], False
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    cut = text
    for _ in range(len(text)):
        idx = cut.rfind("}")
        if idx < 0:
            break
        probe = cut[: idx + 1].rstrip().rstrip(",")
        try:
            return json.loads(f"{probe}\n    ]\n}}")["keywords"], True
        except (json.JSONDecodeError, KeyError, TypeError):
            cut = cut[:idx]
    raise KeywordGenerationError(
        "LLM response could not be parsed as a keywords JSON object, even "
        "after truncation salvage"
    )


def dedupe_keywords(rows: List[dict]) -> Tuple[List[dict], List[str]]:
    """Drop repeated keywords, keeping the first occurrence of each.

    The gold list can carry two listings of the same product type (the
    production Step-3 deliverable has two garlic presses), and the model then
    writes the same supplier search string for both. Step 5 runs each keyword
    once and the same keyword returns the same supplier products, so a repeat
    is redundant rather than useful — it is dropped here and the pool is
    re-validated in full, so any product left short of its floor still fails
    the run. Returns `(rows, dropped_keywords)`.
    """
    seen: set = set()
    kept: List[dict] = []
    dropped: List[str] = []
    for row in rows:
        keyword = row.get("keyword") if isinstance(row, dict) else None
        if keyword is None:
            kept.append(row)
            continue
        key = str(keyword)
        if key in seen:
            dropped.append(key)
            continue
        seen.add(key)
        kept.append(row)
    return kept, dropped


def validate_pool(
    rows: List[dict],
    products: List[str],
    min_keywords: int = MIN_KEYWORDS,
    max_keywords: int = MAX_KEYWORDS,
    per_product_min: Optional[int] = None,
) -> List[str]:
    """Structural validation of the merged pool.

    Returns human-readable problems; an empty list means the pool may flow
    to Step 5. Every rule mirrors a prompt requirement, so a pool the LLM
    was told to produce is also the pool the code accepts. `products` is the
    gold product-name set the table was built from, and `per_product_min`
    defaults to the adaptive floor for that set's size.
    """
    problems: List[str] = []
    if per_product_min is None:
        per_product_min = default_per_product_min(len(products))
    broad_terms = {
        str(r.get("keyword"))
        for r in rows
        if isinstance(r, dict)
        and r.get("role") == "broad"
        and r.get("keyword") is not None
    }
    for i, row in enumerate(rows):
        missing = [field for field in FIELDS if field not in row]
        if missing:
            problems.append(
                f"row {i} ({row.get('keyword')!r}) is missing fields {missing}"
            )
            continue
        if row["pillar"] not in PILLARS:
            problems.append(
                f"row {i} ({row['keyword']!r}) has unknown pillar {row['pillar']!r}"
            )
        if row["role"] not in ROLES:
            problems.append(
                f"row {i} ({row['keyword']!r}) has unknown role {row['role']!r}"
            )
        if row["role"] == "modifier":
            if row["tightens"] not in broad_terms:
                problems.append(
                    f"row {i} ({row['keyword']!r}) tightens a broad term that "
                    f"is not in the pool: {row['tightens']!r}"
                )
            elif row["tightens"] == row["keyword"]:
                problems.append(f"row {i} ({row['keyword']!r}) tightens itself")
        elif row["tightens"] not in (None, ""):
            problems.append(
                f"row {i} ({row['keyword']!r}) is broad but carries "
                f"tightens={row['tightens']!r}"
            )
        banned = carries_banned_token(row["keyword"])
        if banned:
            problems.append(
                f"row {i} ({row['keyword']!r}) carries banned token {banned!r} "
                "(AICIS boundary)"
            )

    duplicates = [
        keyword
        for keyword, count in Counter(str(r.get("keyword")) for r in rows).items()
        if count > 1
    ]
    if duplicates:
        problems.append(f"duplicate keywords in the pool: {duplicates}")

    if not min_keywords <= len(rows) <= max_keywords:
        problems.append(
            f"pool size {len(rows)} is outside the {min_keywords}-{max_keywords} band"
        )

    named = {str(r.get("product")) for r in rows}
    unknown = named - set(products)
    unmentioned = set(products) - named
    if unknown:
        problems.append(
            f"rows name products outside the gold product table: {sorted(unknown)}"
        )
    if unmentioned:
        problems.append(f"gold products with no keywords: {sorted(unmentioned)}")

    per_product = Counter(str(r.get("product")) for r in rows)
    thin = [
        (p, n) for p, n in sorted(per_product.items()) if n < per_product_min
    ]
    if thin:
        problems.append(
            f"products below the {per_product_min}-keyword minimum: {thin}"
        )
    return problems


class KeywordGenerator:
    """Runs the Step-4 gold-keyword prompt through the reasoning LLM."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        gold_products_path: Optional[Path] = None,
        min_per_product: Optional[int] = None,
        max_products: int = DEFAULT_MAX_PRODUCTS,
    ) -> None:
        self.batch_size = batch_size
        self.prompt = load_prompt_parts()
        self.min_per_product = min_per_product
        self.max_products = max_products
        # The LLM endpoint is settled FIRST so a bad `.env` fails fast, before
        # the (comparatively slow) gold deliverable read.
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        if client is not None:
            # An injected client (tests, alternative transports) skips the
            # config requirement; only the model name is still required.
            self.client = client
            self.base_url = self.base_url or "http://llm.invalid"
            if not self.model:
                raise KeywordGenerationConfigError(
                    "LLM_MODEL is not configured (set LLM_MODEL in .env)"
                )
        else:
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
                raise KeywordGenerationConfigError(
                    f"Missing required LLM configuration in .env: {', '.join(missing)}"
                )
            self.client = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)
        self.gold_products = select_gold_products(
            load_gold_products(gold_products_path), max_products
        )
        self.products: List[str] = [str(p["name"]).strip() for p in self.gold_products]
        self.table_lines = build_product_table(self.gold_products)[2:]
        self.per_product_target = per_product_target(len(self.products))

    def _call_llm(self, user_message: str) -> Tuple[str, Optional[str]]:
        """One chat-completions call; returns (content, finish_reason)."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.prompt.system},
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
            raise KeywordGenerationError(f"LLM transport error: {exc}") from exc
        if response.status_code != 200:
            raise KeywordGenerationError(
                f"LLM HTTP {response.status_code}: {response.text[:400]}"
            )
        data = response.json()
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise KeywordGenerationError(
                f"LLM response carries no completion content: {str(data)[:400]}"
            ) from exc
        if not content.strip():
            # Distinct from a JSON-parse failure below: the model produced
            # nothing to parse (usually a reasoning model exhausting
            # `max_tokens` on its chain-of-thought before answering).
            raise KeywordGenerationError(
                "LLM returned empty content "
                f"(finish_reason={finish_reason!r}); a reasoning model likely "
                f"exhausted max_tokens ({MAX_TOKENS}) thinking about the "
                f"batch. Response head: {str(data)[:400]!r}"
            )
        return content, finish_reason

    def _assemble_user_message(self, chunk: List[str]) -> str:
        count = len(chunk)
        total = len(self.products)
        target = self.per_product_target
        note = (
            f"\n\n**This call covers only the {count} products in the table "
            f"above**: produce **{target} keywords per product** "
            f"({target * count} in total). The full {MIN_KEYWORDS}-{MAX_KEYWORDS} "
            f"keyword pool across all {total} products is generated in "
            "batches, so do not attempt the others. **Keep the response as "
            "short as possible**: `rationale` must be at most 6 words, and "
            "omit any other commentary, so the JSON closes well within the "
            "output limit."
        )
        table = "\n".join([TABLE_HEADER, TABLE_SEPARATOR] + chunk)
        body = self.prompt.user_template.replace("{PRODUCT_TABLE}", table + note)
        return f"{body}\n\n{self.prompt.requirements}"

    def generate(self) -> List[CandidateKeyword]:
        """Generate and validate the full keyword bank (plan Step 4).

        Batches the gold product table through the reasoning LLM, merges the
        batch responses (salvaging any truncated one), validates the merged
        pool structurally, and returns the typed candidates. Raises
        `KeywordGenerationError` when a batch fails or the pool breaks any
        structural rule.
        """
        rows: List[dict] = []
        for start in range(0, len(self.table_lines), self.batch_size):
            chunk = self.table_lines[start : start + self.batch_size]
            number = start // self.batch_size + 1
            user_message = self._assemble_user_message(chunk)
            content, finish_reason = self._call_llm(user_message)
            if finish_reason == "length":
                logger.warning(
                    "keyword batch %d hit the output limit "
                    "(finish_reason=length); the JSON tail will be salvaged "
                    "if truncated",
                    number,
                )
            batch_rows, salvaged = parse_llm_keywords(content)
            if salvaged:
                logger.warning(
                    "keyword batch %d was truncated mid-JSON — salvaged %d "
                    "complete rows from the response tail",
                    number,
                    len(batch_rows),
                )
            logger.info("keyword batch %d: %d keywords", number, len(batch_rows))
            rows.extend(batch_rows)

        rows, dropped = dedupe_keywords(rows)
        if dropped:
            logger.warning(
                "dropped %d repeated keyword(s) the model wrote for more than "
                "one product (Step 5 ingests each keyword once): %s",
                len(dropped),
                dropped,
            )
        problems = validate_pool(
            rows, self.products, per_product_min=self.min_per_product
        )
        if problems:
            for problem in problems:
                logger.error("keyword pool rejected: %s", problem)
            raise KeywordGenerationError(
                "keyword pool failed validation:\n- " + "\n- ".join(problems)
            )
        logger.info(
            "keyword pool accepted: %d keywords across %d gold products",
            len(rows),
            len(self.products),
        )
        try:
            return [CandidateKeyword(**row) for row in rows]
        except ValidationError as exc:
            raise KeywordGenerationError(
                f"keyword rows failed schema coercion: {exc}"
            ) from exc
