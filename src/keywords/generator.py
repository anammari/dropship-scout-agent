"""Step 3 keyword generation (plan §4): the reasoning LLM turns the 8
prioritized products of the Step 2 research into the 50–70 candidate supplier
search keywords that Step 4's two-tier Jev gate filters.

The prompt is a tracked package resource (`step3_prompt.md`): the generator
splits it into system message, product table and requirements, then runs it
in batches (default 4 products per call). Batching exists because the
endpoint truncates long responses — the reasoning model's hidden thinking
consumes a variable share of the output budget, so a 70-keyword single
response came back cut mid-JSON (`finish_reason=length`, observed at
~6k–10k chars on `deepseek-v4.1-flash:cloud`). A response that is still
truncated is salvaged by trimming the JSON tail back to the last complete
row rather than losing the batch or spending another call.

Anti-hallucination posture: the LLM only authors keyword rows against the
prompt's own product table, and every structural rule — broad/modifier
pairing, pillar membership, the AICIS banned-token boundary, the 50–70
band, per-product coverage — is re-validated in code after the call. A pool
that fails any rule raises `KeywordGenerationError` instead of flowing on
to Step 4.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Tuple

import httpx
from pydantic import BaseModel, ValidationError

from src.config import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "step3_prompt.md"

MIN_KEYWORDS = 50
MAX_KEYWORDS = 70
MIN_PER_PRODUCT = 5
DEFAULT_BATCH_SIZE = 4
TEMPERATURE = 0.6
MAX_TOKENS = 8000
LLM_TIMEOUT_SECONDS = 900.0

PILLARS = ("curated_home", "self_care_rituals")
ROLES = ("broad", "modifier")
FIELDS = ("keyword", "product", "pillar", "role", "tightens", "rationale")

# The prompt's hard AICIS boundary (requirement 5) and commodity-replacement
# exclusion (requirement 6), enforced in code: any keyword carrying one of
# these substrings fails pool validation, so a violating keyword can never
# reach the Jev gate or a supplier search.
BANNED_TOKENS = (
    "dead sea", "diatomaceous", "jade", "quartz", "crystal", "mud", "salt",
    "soap", "cream", "lotion", "serum", "gel", "shampoo bar", "supplement",
    "vitamin", "oil", "kmart", "bunnings", "coles", "woolworths", "big w",
    "toilet",
)


class KeywordGenerationError(Exception):
    """A batch failed, or the merged pool broke a structural rule."""


class KeywordGenerationConfigError(KeywordGenerationError):
    """LLM endpoint configuration is missing or incomplete (`.env` keys)."""


class CandidateKeyword(BaseModel):
    """One validated row of the candidate pool (the prompt's output schema)."""

    keyword: str
    product: str
    pillar: Literal["curated_home", "self_care_rituals"]
    role: Literal["broad", "modifier"]
    tightens: Optional[str] = None
    rationale: str


@dataclass
class PromptParts:
    """The prompt markdown split into its message parts."""

    system: str
    intro: str
    table_header: str
    product_rows: List[str]
    products: List[str]
    requirements: str


def load_prompt_parts(path: Path = PROMPT_PATH) -> PromptParts:
    """Split the prompt markdown into message parts and the product table."""
    text = path.read_text(encoding="utf-8")
    try:
        system = text.split("## System message", 1)[1].split("## User message", 1)[0].strip()
        user_body = text.split("## User message", 1)[1].split("### Requirements", 1)[0]
        requirements = "### Requirements" + text.split("### Requirements", 1)[1]
    except IndexError as exc:
        raise KeywordGenerationError(
            f"prompt file {path.name} is missing its message markers: {exc}"
        ) from exc

    lines = user_body.splitlines()
    try:
        first_row = next(
            i for i, line in enumerate(lines) if line.startswith("| 1 |")
        )
    except StopIteration as exc:
        raise KeywordGenerationError(
            f"prompt file {path.name} has no numbered product table rows"
        ) from exc
    intro = "\n".join(lines[: first_row - 2]).strip()
    table_header = "\n".join(lines[first_row - 2 : first_row])
    product_rows = [
        line for line in lines[first_row:] if re.match(r"\|\s*\d+\s*\|", line)
    ]
    products: List[str] = []
    for row in product_rows:
        cells = row.split("|")
        if len(cells) < 3:
            raise KeywordGenerationError(
                f"prompt file {path.name} has a malformed product table row: "
                f"{row[:60]!r}"
            )
        products.append(cells[2].strip())
    return PromptParts(
        system=system,
        intro=intro,
        table_header=table_header,
        product_rows=product_rows,
        products=products,
        requirements=requirements,
    )


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


def validate_pool(
    rows: List[dict],
    products: List[str],
    min_keywords: int = MIN_KEYWORDS,
    max_keywords: int = MAX_KEYWORDS,
) -> List[str]:
    """Structural validation of the merged pool.

    Returns human-readable problems; an empty list means the pool may flow
    to Step 4. Every rule mirrors a prompt requirement, so a pool the LLM
    was told to produce is also the pool the code accepts.
    """
    problems: List[str] = []
    broad_terms = {r["keyword"] for r in rows if r.get("role") == "broad"}

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
        lowered = str(row["keyword"]).lower()
        banned = next((t for t in BANNED_TOKENS if t in lowered), None)
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
            f"rows name products outside the prompt table: {sorted(unknown)}"
        )
    if unmentioned:
        problems.append(f"prompt products with no keywords: {sorted(unmentioned)}")

    per_product = Counter(str(r.get("product")) for r in rows)
    thin = [(p, n) for p, n in sorted(per_product.items()) if n < MIN_PER_PRODUCT]
    if thin:
        problems.append(
            f"products below the {MIN_PER_PRODUCT}-keyword minimum: {thin}"
        )
    return problems


class KeywordGenerator:
    """Runs the Step 3 prompt through the reasoning LLM in batches."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.batch_size = batch_size
        self.prompt = load_prompt_parts()
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
            raise KeywordGenerationConfigError(
                f"Missing required LLM configuration in .env: {', '.join(missing)}"
            )
        self.client = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)

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
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise KeywordGenerationError(
                f"LLM response carries no completion content: {str(data)[:400]}"
            ) from exc
        return content, choice.get("finish_reason")

    def _assemble_user_message(self, chunk: List[str]) -> str:
        count = len(chunk)
        total = len(self.prompt.product_rows)
        note = (
            f"\n\n**This call covers only the {count} products in the table "
            f"above**: produce **8-9 keywords per product** "
            f"({8 * count}-{9 * count} in total). The full 50-70 keyword pool "
            f"across all {total} products is generated in batches, so do not "
            "attempt the others. **Keep the response as short as possible**: "
            "`rationale` must be at most 6 words, and omit any other "
            "commentary, so the JSON closes well within the output limit."
        )
        return (
            f"{self.prompt.intro}\n\n{self.prompt.table_header}\n"
            + "\n".join(chunk)
            + note
            + "\n\n"
            + self.prompt.requirements
        )

    def generate(self) -> List[CandidateKeyword]:
        """Generate and validate the full keyword pool (plan Step 3).

        Batches the prompt's product table through the reasoning LLM, merges
        the batch responses (salvaging any truncated one), validates the
        merged pool structurally, and returns the typed candidates. Raises
        `KeywordGenerationError` when a batch fails or the pool breaks any
        structural rule.
        """
        rows: List[dict] = []
        product_rows = self.prompt.product_rows
        for start in range(0, len(product_rows), self.batch_size):
            chunk = product_rows[start : start + self.batch_size]
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

        problems = validate_pool(rows, self.prompt.products)
        if problems:
            for problem in problems:
                logger.error("keyword pool rejected: %s", problem)
            raise KeywordGenerationError(
                "keyword pool failed validation:\n- " + "\n- ".join(problems)
            )
        logger.info(
            "keyword pool accepted: %d keywords across %d products",
            len(rows),
            len(self.prompt.products),
        )
        try:
            return [CandidateKeyword(**row) for row in rows]
        except ValidationError as exc:
            raise KeywordGenerationError(
                f"keyword rows failed schema coercion: {exc}"
            ) from exc