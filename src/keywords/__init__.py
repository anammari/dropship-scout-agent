"""Step 4 gold-standard keyword generation: the reasoning LLM's 50–70 supplier
search keywords, derived from the Step-3 GOLD-STANDARD product list, validated
and typed for Step 5's dual-supplier ingestion (plan §6). The prompt ships as
the package resource `gold_keyword_prompt.md`; the product table it is fed is a
template slot rendered from the live Step-3 deliverable, and the keyword
deliverable itself stays untracked (outputs/ — production deliverables of the
updated pipeline, never repo artefacts).
"""

from src.keywords.generator import (
    DEFAULT_MAX_PRODUCTS,
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
    PromptParts,
    build_product_table,
    carries_banned_token,
    dedupe_keywords,
    default_per_product_min,
    load_gold_products,
    load_prompt_parts,
    parse_llm_keywords,
    per_product_target,
    select_gold_products,
    validate_pool,
)

__all__ = [
    "DEFAULT_MAX_PRODUCTS",
    "CandidateKeyword",
    "KeywordGenerationConfigError",
    "KeywordGenerationError",
    "KeywordGenerator",
    "PromptParts",
    "build_product_table",
    "carries_banned_token",
    "dedupe_keywords",
    "default_per_product_min",
    "load_gold_products",
    "load_prompt_parts",
    "parse_llm_keywords",
    "per_product_target",
    "select_gold_products",
    "validate_pool",
]
