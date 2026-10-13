"""Step 4 (Morning Anthem pipeline): the gold-standard keyword bank.

Turns the Step-3 gold list into the **supplier search keywords** Step 5 feeds to
CJdropshipping — The Morning Anthem's sole supplier.

**The strategy is exact-title first.** Each gold product's PRIMARY keyword is its
own title, transformed into a search string by deterministic code
(`exact_keyword`) — not written by a model, because copying a string is not a job
for one, and a code-derived string cannot hallucinate. That exact string is the
highest-precision query that exists for finding the identical product.

**The model's only job is the optional fallback.** For a product whose exact
title finds nothing, one tightly-specific variant of that title is useful: the
same physical object named by its mechanics, material, form factor or size
(`stainless steel garlic press with cleaner`), never a broader category
(`kitchen tools`) and never a different product. At most ONE per product, and
plenty of products get none — so the bank is roughly one row per product, plus
the fallbacks that earned their place.

What this deletes from the earlier design, and why: the 50–70 per-pool band, the
chunked pools, the adaptive per-product target, the merged bank band, and the
broad/modifier pairing with its `tightens` links. All of them existed to spread a
*generated* pool across products it could not otherwise name. When the primary
keyword is the product's own title, uniqueness is free, coverage is one row per
product, and a band has nothing left to enforce.

What it keeps: the prompt template with its `{PRODUCT_TABLE}` slot, the
demand-ranked / title-diverse / pillar-balanced product selection, the AICIS
banned-token boundary, the batched calls with JSON-tail salvage, and the
neediest-first duplicate collapse.

Anti-hallucination posture: every product fact is code-assembled from the Step-3
deliverable, every exact keyword is code-derived, and the model's fallback rows
are re-validated in code (word bounds, punctuation, banned tokens, and a
requirement that a fallback share a concrete word with the product's own title).
A validation problem that a *model* can cause drops that row; a problem only code
can cause fails the run.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "gold_keyword_prompt.md"

#: Products per LLM call. The endpoint truncates long responses (the reasoning
#: model's hidden thinking shares the completion budget), so the table is sent in
#: batches; a truncated response is salvaged rather than paid for again.
DEFAULT_BATCH_SIZE = 4
#: A safety bound on the product table, not a tuning knob: the gold list is
#: expected to be ~55 rows, so this only bites if a run produces something wild.
DEFAULT_MAX_PRODUCTS = 150
#: How many distinct products one Step-2 `source_keyword` may contribute to the
#: table. The per-topic cap that replaced the one-product-per-source collapse:
#: a saturated topic should still yield its several genuinely different products,
#: but not the whole table.
MAX_PER_SOURCE_KEYWORD = 3
#: A fallback keyword is supplier search vocabulary, 2–7 words.
MIN_FALLBACK_WORDS = 2
MAX_FALLBACK_WORDS = 7
#: The safety bound on an exact keyword's length. A title longer than this is
#: listing-data stuffing rather than a search string, so it is TRUNCATED to this
#: many words (and never below a content word) instead of costing the product its
#: keyword.
#:
#: Why 20 and not a tight bound like 8: a real supplier search wants the product's
#: own title, and titles of 8-15 words are ordinary, not pathological — truncating
#: those would throw away the attributes that make the query precise, which is the
#: exact keyword's entire purpose (the FALLBACK is where a shorter, mechanics-named
#: string belongs). A bound of 20 therefore only touches genuinely stuffed titles,
#: and truncation is strictly better than the alternative it replaces, which was
#: dropping the product from the bank entirely.
MAX_EXACT_WORDS = 20
#: Words a truncated title must not end on: a query ending in "with" or "and"
#: reads as a cut-off sentence and matches worse than one ending on an object word.
_TRAILING_FUNCTION_WORDS = frozenset(
    {
        "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or",
        "the", "to", "with",
    }
)
TEMPERATURE = 0.6
#: The configured model is a *reasoning* model whose chain-of-thought shares the
#: completion budget, so the budget must leave room for the answer after the
#: thinking (observed live: an oversized batch returned empty content with
#: `finish_reason="length"`).
MAX_TOKENS = 16000
LLM_TIMEOUT_SECONDS = 900.0

#: The Morning Anthem merchandising pillars. Single source of truth for the
#: pillar keys used across the pipeline — the Step-3 curator and the Step-6 Jev
#: ranker import this tuple — so every gold product and keyword carries exactly
#: one of these keys.
PILLARS = (
    "brew_fuel",
    "sunrise_move",
    "mindful_awakening",
    "restful_reset",
    "morning_table",
)
MATCHES = ("exact", "tight_fallback")
FIELDS = ("keyword", "product", "pillar", "match", "rationale")

#: The rendered product table's columns. Every cell is copied verbatim from
#: the Step-3 deliverable — the LLM never authors a product fact.
TABLE_HEADER = (
    "| # | Product | Pillar | AU demand evidence | "
    "Physical attributes from the listing | Retail price |"
)
TABLE_SEPARATOR = "| --- | --- | --- | --- | --- | --- |"
TABLE_COLUMNS = ("name", "pillar", "demand_evidence", "attributes", "retail_price_text")

# The hard AICIS boundary and commodity-replacement exclusion, enforced in code
# as well as in the prompt: a keyword carrying one of these substrings can never
# reach a supplier search. The retail-saturation tokens are The Morning Anthem's
# mass-retail set (the launch is Australia-first, so the AU big-box names stay;
# the global names guard the later expansion).
BANNED_TOKENS = (
    "dead sea", "diatomaceous", "jade", "quartz", "crystal", "mud", "salt",
    "soap", "cream", "lotion", "serum", "gel", "shampoo bar", "supplement",
    "vitamin", "oil", "kmart", "bunnings", "coles", "woolworths", "big w",
    "toilet", "walmart", "target", "amazon basics", "ikea",
)

_DEMAND_RATING_RE = re.compile(r"rating\s+(\d+(?:\.\d+)?)\s*/\s*5", re.I)
_DEMAND_REVIEWS_RE = re.compile(r"([\d,]+)\s*reviews?", re.I)

#: Words too short or too generic to prove a fallback is about the same object
#: as its product's title.
_STOPWORD_RE = re.compile(r"[a-z0-9]{3,}")


class KeywordGenerationError(Exception):
    """A batch failed, or the merged bank broke a structural rule."""


class KeywordGenerationConfigError(KeywordGenerationError):
    """LLM endpoint configuration is missing or incomplete (`.env` keys)."""


class CandidateKeyword(BaseModel):
    """One validated row of the keyword bank (the bank's output schema)."""

    keyword: str
    #: The gold product this keyword was derived from, verbatim.
    product: str
    pillar: Literal[
        "brew_fuel",
        "sunrise_move",
        "mindful_awakening",
        "restful_reset",
        "morning_table",
    ]
    #: `exact` — the product's own title, code-derived. `tight_fallback` — one
    #: model-authored variant, used only if the exact title finds nothing.
    match: Literal["exact", "tight_fallback"]
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


def exact_keyword(title: object) -> str:
    """The supplier search string a product's own title yields.

    A deterministic transform, not a model's copy: lowercase, apostrophes
    removed, every other run of non-alphanumerics collapsed to a single space.
    So "Bodum Chambord French Press, 1L — Off White" becomes
    "bodum chambord french press 1l off white".

    Punctuation goes because supplier search boxes index words, and a comma or an
    em dash in a query is noise; the words themselves are the title's own, in its
    own order, and nothing is added, removed or reordered.

    A title stuffed past `MAX_EXACT_WORDS` is truncated to that many words —
    never below the last content word, so the query does not end on "with" or
    "and" — because a truncated title is still a usable search string while no
    keyword at all is a lost product. Truncation keeps the title's own prefix, so    the string remains derived from the product rather than authored about it.
    """
    words = normalized_title_words(title)
    if len(words) <= MAX_EXACT_WORDS:
        return " ".join(words)
    truncated = words[:MAX_EXACT_WORDS]
    while truncated and truncated[-1] in _TRAILING_FUNCTION_WORDS:
        truncated.pop()
    return " ".join(truncated)


def normalized_title_words(title: object) -> List[str]:
    """The title's own words, lowercased and stripped of punctuation."""
    text = str(title or "").lower().replace("’", "").replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().split()


def _content_words(text: object) -> set:
    """The words of `text` that can prove two strings are about the same thing."""
    return set(_STOPWORD_RE.findall(str(text or "").lower()))


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
    resolved = Path(path) if path is not None else Path(
        settings.MORNING_ANTHEM_GOLD_PRODUCTS_PATH
    )
    if not resolved.exists():
        raise KeywordGenerationError(
            f"gold-standard product file not found at {resolved}; run Step 3 "
            "first (scripts/run_gold_standard_research.py)"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise KeywordGenerationError(
            f"gold-standard product file {resolved} is not valid JSON: {exc}"
        ) from exc
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        raise KeywordGenerationError(
            f"gold-standard product file {resolved} carries no 'products' list; "
            "run Step 3 first (scripts/run_gold_standard_research.py)"
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
    """The strongest `limit` products: demand-ranked, title-diverse, pillar-balanced.

    Deterministic. Products whose own NAME carries an AICIS boundary token
    (jade/quartz/salt tools and the like) are not eligible: their exact keyword
    would BE the banned token, so no keyword could both match the product and
    clear the code's fatal boundary rule. They stay in the Step-3 deliverable —
    Step 4 simply cannot name them.

    The rest are ordered by review volume, then rating, then name, and collapsed
    to one row per **normalised exact title** — the very string the exact keyword
    is derived from, so two rows that would emit the identical bank keyword
    (differing only in case or punctuation) cannot both spend a table slot.
    Since the exact title is now the primary keyword, a saturated topic need not
    be bottlenecked to a single product: `MAX_PER_SOURCE_KEYWORD` caps how many
    rows one Step-2 `source_keyword` may contribute, so a category like "no pull
    dog harness" can supply several genuinely distinct products without
    swallowing the whole table. The selection then round-robins across pillars so
    a capped table still covers every pillar.
    """
    ranked: Dict[str, Dict] = {}
    for product in products:
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        if carries_banned_token(name):
            continue
        key = exact_keyword(name) or name.lower()
        if key not in ranked:
            ranked[key] = product
    ordered = sorted(
        ranked.values(),
        key=lambda p: (
            -parse_demand_evidence(p.get("demand_evidence"))[0],
            -parse_demand_evidence(p.get("demand_evidence"))[1],
            str(p.get("name")),
        ),
    )
    per_source: Dict[str, int] = defaultdict(int)
    diverse: List[Dict] = []
    for product in ordered:
        source = str(product.get("source_keyword") or "").strip().lower()
        # A row with no Step-2 keyword is its own bucket (its title is already
        # unique here), so the cap only ever throttles a shared topic.
        bucket = source or str(product.get("name") or "").strip().lower()
        if per_source[bucket] >= MAX_PER_SOURCE_KEYWORD:
            continue
        per_source[bucket] += 1
        diverse.append(product)
    ordered = diverse
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


def build_exact_rows(products: Sequence[Dict]) -> List[Dict]:
    """One `exact` bank row per product: its own title, code-derived.

    The `product` field is the title VERBATIM and `keyword` is the normalised
    search string derived from it, so the two are always in step and nothing
    between them was written by a model.
    """
    rows: List[Dict] = []
    for product in products:
        name = str(product.get("name") or "").strip()
        keyword = exact_keyword(name)
        if not keyword:
            logger.warning("skipping product with no usable title: %r", name)
            continue
        if len(normalized_title_words(name)) > MAX_EXACT_WORDS:
            logger.info(
                "truncated a stuffed title to %d words for %r",
                MAX_EXACT_WORDS, name,
            )
        rows.append(
            {
                "keyword": keyword,
                "product": name,
                "pillar": str(product.get("pillar") or ""),
                "match": "exact",
                "rationale": "the gold product's own title",
            }
        )
    return rows


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


def _row_product(row: dict) -> str:
    return str(row.get("product") or "") if isinstance(row, dict) else ""


def dedupe_keywords(rows: List[dict]) -> Tuple[List[dict], List[str]]:
    """Drop repeated keywords, awarding each repeat to the neediest product.

    Two gold products can normalise to the same search string (near-identical
    titles from different merchants). Step 5 runs each keyword once and the same
    keyword returns the same supplier products, so a repeat is redundant rather
    than useful — it is dropped here, and the duplicate check stays in
    `validate_bank` as the guard for directly-called or hand-merged banks.

    A contested keyword goes to the product with the fewest keywords of its own
    so far (ties to the earliest row), not to whichever batch happened to answer
    first: rows arrive in batch order, so keeping the first occurrence always
    starves the products in the later batches. Returns `(rows, dropped_keywords)`.
    """
    unique: List[Tuple[int, dict]] = []
    contested: Dict[str, List[Tuple[int, dict]]] = {}
    for index, row in enumerate(rows):
        keyword = row.get("keyword") if isinstance(row, dict) else None
        if keyword is None:
            unique.append((index, row))
            continue
        contested.setdefault(str(keyword), []).append((index, row))
    assigned: List[Tuple[int, dict]] = list(unique)
    dropped: List[str] = []
    counts: Dict[str, int] = defaultdict(int)
    for _, row in unique:
        counts[_row_product(row)] += 1
    for keyword, candidates in contested.items():
        if len(candidates) == 1:
            assigned.append(candidates[0])
            continue
        winner = min(
            candidates,
            key=lambda pair: (counts[_row_product(pair[1])], pair[0]),
        )
        assigned.append(winner)
        counts[_row_product(winner[1])] += 1
        dropped.append(keyword)
    assigned.sort(key=lambda pair: pair[0])
    return [row for _, row in assigned], dropped


@dataclass
class BankProblem:
    """One validation finding: which row (if any) and why.

    `row_index` is the position in the row list, or None for a bank-level
    finding. `droppable` marks the findings a *model* can cause — those cost
    their own row and no more, because the product still has its exact keyword.
    Everything else is a code invariant that broke, and fails the run.
    """

    message: str
    row_index: Optional[int] = None
    droppable: bool = False


def validate_bank(rows: Sequence[dict], products: Sequence[Dict]) -> List[BankProblem]:
    """Re-check every rule the prompt states; return the problems found.

    Empty list means valid. The split is deliberate: a problem only the *code*
    can cause (an exact keyword that does not match its product's title, a
    missing exact row, an unknown product, a duplicate that reached this far)
    is fatal, because it means a code invariant broke. A problem the *model* can
    cause (a fallback that is too long, punctuated, banned or loosely coupled)
    is `droppable`: the caller removes that row and re-validates, because one bad
    fallback must not void a whole run.
    """
    problems: List[BankProblem] = []
    by_name = {str(p.get("name") or "").strip(): p for p in products}
    exact_by_product: Dict[str, str] = {}
    seen: Dict[str, int] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(
                BankProblem(f"row {index} is {type(row).__name__}, not an object", index)
            )
            continue
        keyword = str(row.get("keyword") or "").strip()
        product = _row_product(row)
        match = str(row.get("match") or "")
        pillar = str(row.get("pillar") or "")
        if not keyword:
            problems.append(BankProblem(f"row {index} has an empty keyword", index))
            continue
        if match not in MATCHES:
            problems.append(
                BankProblem(f"row {index} ({keyword!r}) has unknown match {match!r}", index)
            )
            continue
        if product not in by_name:
            problems.append(
                BankProblem(
                    f"row {index} ({keyword!r}) names product {product!r}, which "
                    "is not in the table",
                    index,
                )
            )
            continue
        if pillar != str(by_name[product].get("pillar") or ""):
            problems.append(
                BankProblem(
                    f"row {index} ({keyword!r}) has pillar {pillar!r}, which is "
                    f"not its product's {by_name[product].get('pillar')!r}",
                    index,
                )
            )
            continue
        seen[keyword] = seen.get(keyword, 0) + 1

        if match == "exact":
            expected = exact_keyword(product)
            if keyword != expected:
                problems.append(
                    BankProblem(
                        f"row {index} is an exact keyword ({keyword!r}) that does "
                        f"not match its product's own title ({expected!r})",
                        index,
                    )
                )
            banned = carries_banned_token(keyword)
            if banned is not None:
                problems.append(
                    BankProblem(
                        f"row {index} exact keyword {keyword!r} carries the banned "
                        f"token {banned!r} — its product should not have reached "
                        "the table",
                        index,
                    )
                )
            exact_by_product[product] = keyword
            continue

        # --- fallbacks: problems here are the model's, so the caller drops them
        words = keyword.split()
        if not MIN_FALLBACK_WORDS <= len(words) <= MAX_FALLBACK_WORDS:
            problems.append(
                BankProblem(
                    f"row {index} fallback {keyword!r} has {len(words)} word(s), "
                    f"outside {MIN_FALLBACK_WORDS}-{MAX_FALLBACK_WORDS}",
                    index,
                    droppable=True,
                )
            )
        if re.search(r"[^a-z0-9 ]", keyword):
            problems.append(
                BankProblem(
                    f"row {index} fallback {keyword!r} carries punctuation or "
                    "capitals",
                    index,
                    droppable=True,
                )
            )
        banned = carries_banned_token(keyword)
        if banned is not None:
            problems.append(
                BankProblem(
                    f"row {index} fallback {keyword!r} carries the banned token "
                    f"{banned!r}",
                    index,
                    droppable=True,
                )
            )
        if not (_content_words(keyword) & _content_words(product)):
            # The cheap code-side proxy for "specific to THIS product": a
            # fallback sharing no concrete word with its own title is a broader
            # category or a different product, not a variant of this one.
            problems.append(
                BankProblem(
                    f"row {index} fallback {keyword!r} shares no concrete word "
                    f"with its product {product!r} — it is too loosely coupled",
                    index,
                    droppable=True,
                )
            )

    for product in products:
        name = str(product.get("name") or "").strip()
        if name and exact_keyword(name) and name not in exact_by_product:
            problems.append(
                BankProblem(
                    f"product {name!r} has no exact keyword — every product in "
                    "the table must be named by its own title"
                )
            )
    for keyword, count in seen.items():
        if count > 1:
            problems.append(
                BankProblem(
                    f"keyword {keyword!r} appears {count} times; duplicates must "
                    "be collapsed before validation"
                )
            )
    return problems


class KeywordGenerator:
    """Builds the keyword bank: exact titles in code, fallbacks from the LLM."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        gold_products_path: Optional[Path] = None,
        max_products: int = DEFAULT_MAX_PRODUCTS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.batch_size = batch_size
        if self.batch_size < 1:
            raise KeywordGenerationError(
                f"batch_size must be >= 1 (got {self.batch_size})"
            )
        self.prompt = load_prompt_parts()
        products = load_gold_products(gold_products_path)
        self.products = select_gold_products(products, limit=max_products)
        if not self.products:
            raise KeywordGenerationError(
                "no eligible gold products after the boundary-name exclusion "
                "and the per-topic cap; run Step 3 first"
            )

        # Set unconditionally: an injected client skips config *resolution*,
        # not the request itself, so `_call_llm` still needs the key.
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL

        # An injected client (tests, alternative transports) skips the endpoint
        # requirement entirely; only the model name is still required.
        if client is not None:
            self.client = client
            self.base_url = (base_url or "http://llm.invalid").rstrip("/")
            if not self.model:
                raise KeywordGenerationConfigError(
                    "LLM_MODEL is not configured (set LLM_MODEL in .env)"
                )
            return

        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
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

    # --- the LLM call -----------------------------------------------------

    def _call_llm(self, user_message: str) -> Tuple[str, Optional[str]]:
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

    def _assemble_user_message(
        self, chunk: List[str], taken: Sequence[str] = ()
    ) -> str:
        count = len(chunk)
        note = (
            f"\n\n**This call covers only the {count} products in the table "
            "above.** Write **at most one fallback keyword for each**, fewer "
            "where no honest one exists — an empty list is a valid answer. "
            "**Keep the response as short as possible**: `rationale` must be "
            "at most 6 words, and omit any other commentary, so the JSON closes "
            "well within the output limit."
        )
        if taken:
            # Each call is independent, so a later batch cannot know what an
            # earlier one already claimed. Without this list the bank-wide
            # uniqueness rule is unsatisfiable across batches.
            note += (
                f"\n\nEarlier batches already used these {len(taken)} fallback "
                "string(s) — never repeat one of them: "
                + ", ".join(f"`{keyword}`" for keyword in taken)
                + "."
            )
        table = "\n".join([TABLE_HEADER, TABLE_SEPARATOR] + chunk)
        body = self.prompt.user_template.replace("{PRODUCT_TABLE}", table + note)
        return f"{body}\n\n{self.prompt.requirements}"

    # --- the bank ---------------------------------------------------------

    def generate_bank(self) -> List[CandidateKeyword]:
        """Build the whole bank and return it typed, or raise.

        Deterministic where it can be: the exact rows come from
        `build_exact_rows`, so the only model output in the bank is the set of
        fallbacks. A failing batch or an invalid bank writes nothing — the
        runner never sees a partial bank.
        """
        table = build_product_table(self.products)
        rows: List[Dict] = build_exact_rows(self.products)
        taken: List[str] = [str(row["keyword"]) for row in rows]

        for start in range(0, len(table) - 2, self.batch_size):
            # The table's first two lines are header + separator.
            chunk = table[2:][start : start + self.batch_size]
            content, finish_reason = self._call_llm(
                self._assemble_user_message(chunk, taken)
            )
            try:
                batch_rows, salvaged = parse_llm_keywords(content)
            except KeywordGenerationError:
                raise
            if salvaged:
                logger.warning(
                    "batch %d was truncated (finish_reason=%r); salvaged %d "
                    "row(s)",
                    start // self.batch_size + 1, finish_reason, len(batch_rows),
                )
            for row in batch_rows:
                if not isinstance(row, dict):
                    continue
                keyword = str(row.get("keyword") or "").strip()
                product = str(row.get("product") or "").strip()
                if not keyword or not product:
                    continue
                match = str(row.get("match") or "tight_fallback")
                by_name = {str(p.get("name") or "").strip(): p for p in self.products}
                if product not in by_name or match != "tight_fallback":
                    # The model's job is the fallback and nothing else: a row
                    # naming an unknown product, or claiming to be an exact
                    # keyword, is not something a model gets to decide.
                    logger.warning(
                        "ignoring LLM row %r: product %r with match %r is not a "
                        "fallback for a table product",
                        keyword, product, match,
                    )
                    continue
                rows.append(
                    {
                        "keyword": keyword.lower(),
                        "product": product,
                        "pillar": str(by_name[product].get("pillar") or ""),
                        "match": "tight_fallback",
                        "rationale": str(row.get("rationale") or "")[:80],
                    }
                )
                taken.append(keyword.lower())

        rows, dropped = dedupe_keywords(rows)
        if dropped:
            logger.warning(
                "dropped %d duplicate keyword(s) across products: %s",
                len(dropped), ", ".join(sorted(set(dropped))),
            )

        problems = validate_bank(rows, self.products)
        droppable = {
            problem.row_index
            for problem in problems
            if problem.droppable and problem.row_index is not None
        }
        if droppable:
            # A model's fallback may miss; the product still has its exact
            # keyword, so the row goes and the run continues. Re-validating the
            # survivors is cheap insurance that nothing else was riding on it.
            logger.warning(
                "dropping %d fallback row(s) that failed validation: %s",
                len(droppable),
                "; ".join(
                    problem.message
                    for problem in problems
                    if problem.row_index in droppable
                ),
            )
            rows = [row for index, row in enumerate(rows) if index not in droppable]
            problems = validate_bank(rows, self.products)
        if problems:
            raise KeywordGenerationError(
                "keyword bank failed validation:\n- "
                + "\n- ".join(problem.message for problem in problems)
            )
        return [CandidateKeyword(**row) for row in rows]
