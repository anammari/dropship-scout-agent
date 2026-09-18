"""Pydantic schemas for the Supplier-First ingestion pipeline (plan Part F).

Architectural pivot (Part F.0): the old Meta Ad -> LLM -> supplier-search
flow is removed. Extractors fetch live, active supplier products directly
and wrap them in `RawSupplierProduct` — every candidate therefore starts
with a real supplier URL, a real listed price, and the supplier's own
image gallery. The LLM's job shrinks to AU-market viability and marketing
copy (F.4); all supplier/cost fields on `ProductCandidateEvaluation` are
mapped programmatically from the raw product via `from_raw` (F.2) — the
LLM never authors `supplier_name`, `supplier_retail_url`,
`estimated_cogs_aud`, or `cogs_estimation_basis`.

Data-integrity rules carried over from earlier remediations:
- No evaluation model contains an image-url field: the LLM must never
  author, edit, or guess media URLs. Imagery flows from the verified
  `RawSupplierProduct.image_urls` CDN array to
  `src/pipeline/image_sourcing.py` directly.
- `cogs_estimation_basis` is zero-URL free text: any `http(s)://`
  substring is rejected by a field validator. The programmatic basis
  built in `from_raw` satisfies this by construction.
- `supplier_retail_url` is validated against the supplier's known
  direct-product-page shape — search/category/gateway URLs are rejected
  at the schema level on both `RawSupplierProduct` and the final
  evaluation.
"""

import re
from typing import List, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Intentionally broad (plan Part C): any http(s):// substring, any domain.
# LLMs cannot know ephemeral marketplace item IDs, so any URL the model
# emits into a rationale field is by construction a hallucination.
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Search/category/gateway URL shapes a supplier_retail_url must NEVER have:
# a candidate whose "supplier" link is really a keyword-search results page
# is an operational dead end at fulfillment time.
_SEARCH_URL_SIGNAL_PATTERN = re.compile(
    r"(SearchText=|/wholesale|/search|/s/|/catalogsearch|[?&]q=|/category/)",
    re.IGNORECASE,
)

# CJdropshipping product ids come in two shapes: numeric (newer listings,
# e.g. 1745360894529376256) and UUID-style (older listings, e.g.
# B03F2DFF-276D-481C-AD18-28DF22E411CC). Both are live PDP ids — the MCP
# search returns the UUID form for a large slice of the catalog, so a
# digits-only pattern would silently reject them.
_CJ_PID_PATTERN = (
    r"(?:\d+|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})"
)

# Known direct-product-page URL shapes per supplier (F.3 extractors emit
# exactly these shapes; validators enforce them end-to-end). CJdropshipping
# accepts both the legacy slug-pid shape and the canonical
# /product/{pid}.html detail URL.
_SUPPLIER_PDP_PATTERNS = {
    "AliExpress": re.compile(r"/item/\d+\.html"),
    "Etsy": re.compile(r"/listing/\d+/"),
    "CJdropshipping": re.compile(
        rf"/product/(?:[\w-]+-p-)?{_CJ_PID_PATTERN}\.html"
    ),
}

KNOWN_SUPPLIERS = frozenset(_SUPPLIER_PDP_PATTERNS)


def _reject_urls_in_cogs_basis(v: str) -> str:
    """Zero-URL hard rule (plan Part C / CLAUDE.md §3.5 rule 2).

    Field-scoped so instructor's retry loop gets a precise field location
    to re-prompt against. Runs on REJECT verdicts too — a fabricated URL
    in the rationale is a hallucination bug regardless of the verdict.
    """
    if _URL_PATTERN.search(v or ""):
        raise ValueError(
            "cogs_estimation_basis must not contain URLs or item IDs; "
            "provide only numerical cost breakdowns, material weights, "
            "and freight assumptions"
        )
    return v


def _enforce_accept_gates(obj) -> None:
    """ACCEPT-only margin-math and tagging gates (CLAUDE.md §3 gate 2).

    A final ACCEPT must clear the margin floor — markup_multiplier >= 3.0
    OR estimated_margin_aud strictly greater than AUD 25.0 — and must
    carry the mandatory `dropship` target tag (case-insensitive,
    whitespace-tolerant).
    """
    if obj.verdict != "ACCEPT":
        return
    if not (obj.markup_multiplier >= 3.0 or obj.estimated_margin_aud > 25.0):
        raise ValueError(
            "ACCEPT verdict requires markup_multiplier >= 3.0 or "
            "estimated_margin_aud > 25.0 AUD gross profit per unit"
        )
    tags = {tag.strip().lower() for tag in obj.target_tags if tag and tag.strip()}
    if "dropship" not in tags:
        raise ValueError(
            'ACCEPT verdict requires the mandatory "dropship" tag in target_tags'
        )


class RawSupplierProduct(BaseModel):
    """One live, active supplier listing, captured upstream of the LLM.

    The unit of the Supplier-First pipeline (plan F.2): every instance
    guarantees a real, currently-listed product URL, the supplier's own
    listed price (AUD-converted), and >= 3 direct CDN gallery URLs. The
    LLM evaluates THIS — no ad copy, no invented COGS, no guessed links.
    """

    supplier_name: str = Field(
        description="'AliExpress', 'Etsy', or 'CJdropshipping' (canonical name)"
    )
    supplier_retail_url: str = Field(
        description="100% live, direct product-detail page URL from the extractor."
    )
    product_title: str
    product_description: str
    price_aud: float = Field(
        ge=0,
        description="Supplier listed unit price converted to AUD by the extractor.",
    )
    shipping_cost_aud: float = Field(
        default=0.0,
        ge=0,
        description="Supplier-quoted tracked shipping to AU in AUD (0 when unquoted).",
    )
    image_urls: List[str] = Field(
        min_length=3,
        description="Direct CDN links from the supplier's own product gallery.",
    )

    @field_validator("supplier_name")
    @classmethod
    def must_be_known_supplier(cls, v: str) -> str:
        if v not in KNOWN_SUPPLIERS:
            raise ValueError(
                f"supplier_name must be one of {sorted(KNOWN_SUPPLIERS)}, got {v!r}"
            )
        return v

    @field_validator("supplier_retail_url")
    @classmethod
    def must_be_direct_product_page(cls, v: str, info) -> str:
        if not v or not v.startswith(("http://", "https://")):
            raise ValueError("supplier_retail_url must be a non-empty absolute URL")
        if _SEARCH_URL_SIGNAL_PATTERN.search(v):
            raise ValueError(
                "supplier_retail_url must be a direct, exact-match product "
                "page — search/category/gateway URLs are not permitted"
            )
        supplier_name = (info.data or {}).get("supplier_name")
        pattern = _SUPPLIER_PDP_PATTERNS.get(supplier_name)
        if pattern and not pattern.search(v):
            raise ValueError(
                f"supplier_retail_url does not match the expected direct "
                f"product-page shape for supplier_name={supplier_name!r}"
            )
        return v

    @field_validator("image_urls")
    @classmethod
    def at_least_three_distinct_absolute_urls(cls, v: List[str]) -> List[str]:
        for url in v:
            if not url or not url.startswith(("http://", "https://")):
                raise ValueError(
                    "image_urls entries must be absolute http(s) URLs"
                )
        seen = set()
        ordered = [u for u in v if not (u in seen or seen.add(u))]
        if len(ordered) < 3:
            raise ValueError(
                "image_urls must carry at least 3 DISTINCT gallery URLs "
                f"(got {len(ordered)} unique of {len(v)} provided)"
            )
        return ordered


class ProvisionalProductEvaluation(BaseModel):
    """LLM-authored marketing/viability fields only (plan F.4).

    Deliberately carries NO supplier, COGS, or image fields: the LLM's
    entire job is the ACCEPT/REJECT verdict and the marketing payload.
    `ProductCandidateEvaluation.from_raw` merges this with a
    `RawSupplierProduct` in code.
    """

    verdict: Literal["ACCEPT", "REJECT"]
    niche_category: str
    problem_solved: str
    suggested_retail_aud: float = Field(ge=0)
    marketing_ad_copy: str = Field(
        description=(
            "Compelling shopper-facing ad copy for the Australian market. "
            "Grounded ONLY in the supplier product data — never invented "
            "specifications."
        )
    )
    saturation_risk: Literal["LOW", "MEDIUM", "HIGH"]
    target_tags: List[str] = Field(default_factory=lambda: ["dropship"])
    shipping_notice_au: str
    key_features: List[str] = Field(
        default_factory=list,
        description=(
            "Concrete marketing bullets derived ONLY from the supplier "
            "product title/description — never invented specifications."
        ),
    )

    @model_validator(mode="after")
    def enforce_provisional_completeness(self):
        if self.verdict == "ACCEPT":
            if not self.marketing_ad_copy or not self.marketing_ad_copy.strip():
                raise ValueError(
                    "ACCEPT verdict requires non-empty marketing_ad_copy"
                )
            if not self.problem_solved or not self.problem_solved.strip():
                raise ValueError(
                    "ACCEPT verdict requires a non-empty problem_solved"
                )
            features = [f for f in self.key_features if f and f.strip()]
            if len(features) < 3:
                raise ValueError(
                    "ACCEPT verdict requires at least 3 non-empty key_features"
                )
        tags = {t.strip().lower() for t in self.target_tags if t and t.strip()}
        if self.verdict == "ACCEPT" and "dropship" not in tags:
            raise ValueError(
                'ACCEPT verdict requires the mandatory "dropship" tag in target_tags'
            )
        return self


class ProductCandidateEvaluation(BaseModel):
    """Final evaluation: LLM marketing fields + programmatic supplier/cost data.

    `supplier_name`, `supplier_retail_url`, `estimated_cogs_aud`, and
    `cogs_estimation_basis` are NEVER authored by the LLM — they are mapped
    from the verified `RawSupplierProduct` by `from_raw` (plan F.2).
    `estimated_margin_aud`/`markup_multiplier` are recomputed in code; the
    LLM's arithmetic is never trusted. There is intentionally NO image-url
    field: imagery flows from `RawSupplierProduct.image_urls` through
    `src/pipeline/image_sourcing.py` straight to the exporter.
    """

    # --- LLM-authored (via ProvisionalProductEvaluation) ---
    verdict: Literal["ACCEPT", "REJECT"]
    niche_category: str
    problem_solved: str
    suggested_retail_aud: float
    marketing_ad_copy: str
    saturation_risk: Literal["LOW", "MEDIUM", "HIGH"]
    target_tags: List[str] = Field(default_factory=lambda: ["dropship"])
    shipping_notice_au: str
    key_features: List[str] = Field(default_factory=list)

    # --- Mapped programmatically from RawSupplierProduct (never the LLM) ---
    supplier_name: str = Field(
        description=(
            "The marketplace this live product came from, copied verbatim "
            "from RawSupplierProduct.supplier_name."
        )
    )
    supplier_retail_url: str = Field(
        description=(
            "The verified, live product-detail page, copied verbatim from "
            "RawSupplierProduct.supplier_retail_url. NEVER authored by the LLM."
        )
    )
    estimated_cogs_aud: float = Field(
        description=(
            "RawSupplierProduct.price_aud + shipping_cost_aud — real supplier "
            "data, not an LLM estimate."
        )
    )
    cogs_estimation_basis: str = Field(
        description=(
            "Zero-URL accounting rationale constructed programmatically from "
            "the supplier's own listed price and shipping figures."
        )
    )
    # --- Derived deterministically in from_raw ---
    estimated_margin_aud: float
    markup_multiplier: float

    @field_validator("cogs_estimation_basis")
    @classmethod
    def no_urls_in_cogs_basis(cls, v: str) -> str:
        return _reject_urls_in_cogs_basis(v)

    @field_validator("supplier_retail_url")
    @classmethod
    def must_be_direct_product_page(cls, v: str, info) -> str:
        """Defense in depth — mirrors RawSupplierProduct's URL contract."""
        if not v or not v.startswith(("http://", "https://")):
            raise ValueError("supplier_retail_url must be a non-empty absolute URL")
        if _SEARCH_URL_SIGNAL_PATTERN.search(v):
            raise ValueError(
                "supplier_retail_url must be a direct, exact-match product "
                "page — search/category/gateway URLs are not permitted"
            )
        supplier_name = (info.data or {}).get("supplier_name")
        pattern = _SUPPLIER_PDP_PATTERNS.get(supplier_name)
        if pattern and not pattern.search(v):
            raise ValueError(
                f"supplier_retail_url does not match the expected direct "
                f"product-page shape for supplier_name={supplier_name!r}"
            )
        return v

    @model_validator(mode="after")
    def enforce_sourcing_completeness(self):
        if self.verdict == "ACCEPT":
            if not self.supplier_name or not self.supplier_name.strip():
                raise ValueError(
                    "ACCEPT verdict requires a non-empty supplier_name"
                )
            if not self.supplier_retail_url:
                raise ValueError(
                    "ACCEPT verdict requires a non-empty supplier_retail_url"
                )
            if not self.cogs_estimation_basis or not self.cogs_estimation_basis.strip():
                raise ValueError(
                    "ACCEPT verdict requires a non-empty cogs_estimation_basis"
                )
            if not self.marketing_ad_copy or not self.marketing_ad_copy.strip():
                raise ValueError(
                    "ACCEPT verdict requires a non-empty marketing_ad_copy"
                )
        _enforce_accept_gates(self)
        return self

    @classmethod
    def from_raw(
        cls, provisional: ProvisionalProductEvaluation, raw: RawSupplierProduct
    ) -> "ProductCandidateEvaluation":
        """Map a provisional LLM verdict onto verified supplier data (F.2/F.5).

        COGS is the supplier's real listed price + shipping; margin math is
        recomputed in code. Raises ValidationError through the model's
        validators when the ACCEPT's retail price fails the margin floor —
        callers (the evaluator) treat that as a downgrade-to-REJECT signal,
        not an export.
        """
        cogs = round(raw.price_aud + raw.shipping_cost_aud, 2)
        if raw.shipping_cost_aud > 0:
            basis = (
                f"Supplier listed price AUD ${raw.price_aud:.2f} plus AUD "
                f"${raw.shipping_cost_aud:.2f} tracked shipping to AU, taken "
                f"directly from the live {raw.supplier_name} listing."
            )
        else:
            basis = (
                f"Supplier listed price AUD ${raw.price_aud:.2f} taken "
                f"directly from the live {raw.supplier_name} listing; the "
                f"listing API does not quote shipping to AU, so shipping is "
                f"treated as AUD $0.00 pending fulfillment quotes."
            )
        margin = round(provisional.suggested_retail_aud - cogs, 2)
        markup = round(provisional.suggested_retail_aud / cogs, 2) if cogs > 0 else 0.0
        return cls(
            verdict=provisional.verdict,
            niche_category=provisional.niche_category,
            problem_solved=provisional.problem_solved,
            suggested_retail_aud=provisional.suggested_retail_aud,
            marketing_ad_copy=provisional.marketing_ad_copy,
            saturation_risk=provisional.saturation_risk,
            target_tags=provisional.target_tags,
            shipping_notice_au=provisional.shipping_notice_au,
            key_features=list(provisional.key_features),
            supplier_name=raw.supplier_name,
            supplier_retail_url=raw.supplier_retail_url,
            estimated_cogs_aud=cogs,
            cogs_estimation_basis=basis,
            estimated_margin_aud=margin,
            markup_multiplier=markup,
        )