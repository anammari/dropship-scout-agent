"""Step 4 gold-standard keyword generation: the supplier search keywords Step 5
feeds to CJdropshipping, built **exact-title first** from the Step-3 gold list.

Each product's primary keyword is its own title, derived in code; the configured
reasoning LLM contributes only the optional one-per-product fallback. The prompt
ships as the package resource `gold_keyword_prompt.md`, whose product table is a
template slot rendered from the live Step-3 deliverable. The keyword deliverable
itself stays untracked (`outputs/` — production deliverables of the Morning
Anthem pipeline, never repo artefacts).
"""

from src.keywords.generator import (
    BANNED_TOKENS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_PRODUCTS,
    FIELDS,
    MATCHES,
    MAX_FALLBACK_WORDS,
    MIN_FALLBACK_WORDS,
    PILLARS,
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
    PromptParts,
    build_exact_rows,
    build_product_table,
    carries_banned_token,
    dedupe_keywords,
    exact_keyword,
    load_gold_products,
    load_prompt_parts,
    parse_demand_evidence,
    parse_llm_keywords,
    select_gold_products,
    validate_bank,
)

__all__ = [
    "BANNED_TOKENS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_PRODUCTS",
    "FIELDS",
    "MATCHES",
    "MAX_FALLBACK_WORDS",
    "MIN_FALLBACK_WORDS",
    "PILLARS",
    "CandidateKeyword",
    "KeywordGenerationConfigError",
    "KeywordGenerationError",
    "KeywordGenerator",
    "PromptParts",
    "build_exact_rows",
    "build_product_table",
    "carries_banned_token",
    "dedupe_keywords",
    "exact_keyword",
    "load_gold_products",
    "load_prompt_parts",
    "parse_demand_evidence",
    "parse_llm_keywords",
    "select_gold_products",
    "validate_bank",
]
