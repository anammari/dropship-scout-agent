# Dropship Scout Agent — Implementation Plan (Supplier-First Architecture)

> **Status: aligned with the implemented codebase as of 2026-09-18,
> including the CJdropshipping MCP migration (§4.2, §5, and the CJ rows of
> §10–§12).** The retired REST client/extractor
> (`src/pipeline/cj_client.py`, `src/extractors/cj_api_extractor.py`) and
> their Two-Tier Liveness Gate have been deleted. The suite is 292 tests,
> all passing, zero network. Where the live CJ MCP server contradicted this
> document's assumptions, the implemented behaviour is noted inline as
> **[PINNED FROM LIVE CATALOG]**.
>
> History note: the previous revision of this file accumulated six layered
> remediation plans (PART A–F); that history was pruned on 2026-09-11 and a
> lineage table at the end records which decisions from each remediation
> survive in the current code.

## 1. Mission & Architecture

The agent scouts dropshipping product candidates against strict commercial
and logistical criteria and exports 3–5 validated winning product packages
directly into the Shopify workspace:

```
/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/
```

**Architectural pivot (Supplier-First):** the old Meta Ad → LLM →
supplier-search flow is gone. The pipeline ingests live, verified products
directly from supplier APIs/scrapers, so every candidate starts with a real
supplier URL, a real listed price, and the supplier's own image gallery
*before* the LLM ever sees it.

```
[Stage 1] Supplier extractors (chain, SUPPLIER_PRIORITY_ORDER)
          CJdropshipping MCP  →  AliExpress via Apify  →  Etsy Open API v3
          CJ hits must pass the MCP Payload Liveness Gate (§5)
                         │  List[RawSupplierProduct]  (verified ground truth)
                         ▼
[Stage 2] LLM viability & marketing evaluation (deepseek-v4-flash:cloud)
          authors ONLY: verdict, niche, problem, retail price, ad copy,
          features, shipping notice, tags
          margin math reconciled in code against the REAL listed cost
                         │  (ProductCandidateEvaluation, ACCEPT only)
                         ▼
[Stage 3] Deterministic CDN image download (image_sourcing.py, zero LLM)
          ≥ 3 distinct validated images from RawSupplierProduct.image_urls
                         │
                         ▼
[Stage 4] Workspace export (exporter.py)
          product-NN/{metadata.json, images/}
                         │
                         ▼
[Run summary] funnel counters printed every run
```

**Anti-hallucination contract (hard rules, enforced by Pydantic validators):**

1. The LLM never sources, authors, edits, or invents media URLs — no image
   field exists on any evaluation model. Imagery flows from
   `RawSupplierProduct.image_urls` through `image_sourcing.py` directly.
2. The LLM never authors `supplier_name`, `supplier_retail_url`,
   `estimated_cogs_aud`, or `cogs_estimation_basis` — those are mapped
   programmatically from the verified raw product via
   `ProductCandidateEvaluation.from_raw`.
3. `cogs_estimation_basis` is zero-URL free text: any `http(s)://` substring
   is rejected by a field validator.
4. The LLM's arithmetic is never trusted: margin/markup are recomputed in
   code, and an ACCEPT whose reconciled figures miss the margin floor
   (markup ≥ 3.0 OR margin > AUD 25) is downgraded to REJECT.
5. Every supplier URL must match its supplier's direct-product-page shape —
   search/category/gateway URLs are rejected at the schema level.

## 2. Workspace Layout

```
dropship-scout-agent/
├── CLAUDE.md
├── pyproject.toml
├── .env.example
├── .env                     # credentials (CJ_MCP_TOKEN, APIFY_API_TOKEN, LLM_*, ETSY_API_KEY)
├── src/
│   ├── config.py            # env-driven Settings + load_settings() factory
│   ├── models.py            # RawSupplierProduct, ProvisionalProductEvaluation,
│   │                        #   ProductCandidateEvaluation (+ from_raw)
│   ├── extractors/
│   │   ├── base.py          # BaseSupplierExtractor + shared exceptions
│   │   ├── cj_mcp_extractor.py      # CJ via MCP tools
│   │   ├── aliexpress_apify.py      # Apify-managed AliExpress scraper
│   │   └── etsy_api.py              # Etsy Open API v3
│   ├── evaluators/
│   │   └── llm_filter.py    # instructor + ProvisionalProductEvaluation
│   ├── pipeline/
│   │   ├── cj_mcp_client.py # CJ MCP client + MCP Payload Liveness Gate
│   │   └── image_sourcing.py# deterministic CDN image download/validation
│   ├── exporter.py          # workspace writer (metadata.json + images/)
│   └── main.py              # CLI orchestrator, funnel counters, exit codes
└── tests/                   # hermetic test files (292 tests, zero network)
```

**Deleted by the CJ MCP migration (do not resurrect):**
`src/pipeline/cj_client.py` (REST client + Two-Tier Liveness Gate —
superseded: the CJ API cannot flag stale items and the Turnstile-walled
frontend check is permanently inconclusive) and
`src/extractors/cj_api_extractor.py` (REST extractor wiring the old gate).

**Deleted earlier (do not resurrect):** `src/extractors/playwright_scraper.py`
(Meta Ad Library scraper), `src/extractors/apify_scraper.py` (Meta ad
fallback), `src/pipeline/supplier_sourcing.py` (fuzzy-match sourcing —
obsolete because extractors start from the supplier URL), and the
`ScrapedAdData` / `SupplierSearchSeed` / search-gateway schemas.

## 3. Data Contracts (`src/models.py`)

*Unchanged by the MCP migration — the MCP extractor produces the same
`RawSupplierProduct` the pipeline already consumes.*

### 3.1 `RawSupplierProduct` — the pipeline's unit of truth

One live, active supplier listing, captured upstream of the LLM. Every
instance guarantees a real product URL, a real listed AUD price, and ≥ 3
direct CDN gallery URLs.

| Field | Type / constraint |
|---|---|
| `supplier_name` | Must be one of `AliExpress`, `Etsy`, `CJdropshipping` (`KNOWN_SUPPLIERS`) |
| `supplier_retail_url` | Absolute http(s); must match the supplier's PDP shape (table below); search-gateway shapes rejected |
| `product_title` | Non-empty (exporter additionally hard-drops empty titles) |
| `product_description` | Free text, HTML-stripped by the extractor, ≤ 4000 chars |
| `price_aud` | ≥ 0; supplier listed price × `settings.USD_TO_AUD`, rounded to 2dp |
| `shipping_cost_aud` | Default 0.0 (unquoted shipping; stated in the COGS basis) |
| `image_urls` | ≥ 3 **distinct** absolute http(s) URLs (order-preserving dedupe) |

PDP-shape patterns (enforced on both `RawSupplierProduct` and the final
evaluation — defense in depth):

```python
_SUPPLIER_PDP_PATTERNS = {
    "AliExpress":     re.compile(r"/item/\d+\.html"),
    "Etsy":           re.compile(r"/listing/\d+/"),
    "CJdropshipping": re.compile(r"/product/(?:[\w-]+-p-)?\d+\.html"),
}
_SEARCH_URL_SIGNAL_PATTERN  # SearchText=, /wholesale, /search, /s/,
                            # /catalogsearch, [?&]q=, /category/  -> rejected
```

CJdropshipping accepts both the canonical `/product/{pid}.html` and the
legacy `/{slug}-p-{pid}.html` form, and the `<pid>` itself is either
numeric or UUID-form. The MCP extractor emits CJ's own published
`productUrl` when the payload carries one (the only known-good form for
UUID pids) and the canonical plain-pid URL otherwise.

### 3.2 `ProvisionalProductEvaluation` — LLM-authored fields only

The `instructor` response model. Deliberately carries **no** supplier, COGS,
or image fields.

| Field | Notes |
|---|---|
| `verdict` | `ACCEPT` / `REJECT` |
| `niche_category`, `problem_solved` | ACCEPT requires non-empty `problem_solved` |
| `suggested_retail_aud` | Must clear the margin floor against the REAL cost |
| `marketing_ad_copy` | ACCEPT requires non-empty, grounded in supplier data |
| `key_features` | ACCEPT requires ≥ 3 non-empty bullets (never invented specs) |
| `saturation_risk` | `LOW` / `MEDIUM` / `HIGH` |
| `target_tags` | ACCEPT requires the `dropship` tag (case-insensitive) |
| `shipping_notice_au` | Customer-facing AU shipping line |

### 3.3 `ProductCandidateEvaluation` — final export payload

Built exclusively by `from_raw(provisional, raw)`; never hand-constructed
with LLM-authored supplier data.

- **From the LLM (provisional):** verdict, niche_category, problem_solved,
  suggested_retail_aud, marketing_ad_copy, saturation_risk, target_tags,
  shipping_notice_au, key_features.
- **Mapped from `RawSupplierProduct`:** `supplier_name` and
  `supplier_retail_url` (copied verbatim), `estimated_cogs_aud`
  (= `price_aud + shipping_cost_aud`), `cogs_estimation_basis`
  (programmatic zero-URL rationale quoting the real listed price and
  shipping, naming the supplier).
- **Recomputed in code:** `estimated_margin_aud`
  (= retail − cogs), `markup_multiplier` (= retail / cogs).

Validators: `no_urls_in_cogs_basis` (any `https?://` substring → reject),
`must_be_direct_product_page` (same PDP contract as §3.1),
`enforce_sourcing_completeness` (ACCEPT requires non-empty supplier/cogs/
marketing fields), and the margin floor (markup ≥ 3.0 OR margin > AUD 25.0)
plus the mandatory `dropship` tag.

**Field roles (do not conflate):** `cogs_estimation_basis` is an internal
accounting note (zero URLs, validator-enforced); `supplier_retail_url` is
the sole fulfillment link — a verified, live, exact-match product detail
page; there is **no** `competitor_retail_url` in the Supplier-First export
contract (the competitor concept belonged to the retired ad-scraping
pipeline).

## 4. Supplier Extractors (`src/extractors/`)

### 4.1 Base interface & exception taxonomy (`base.py`)

```python
class BaseSupplierExtractor(abc.ABC):
    engine_name: str    # e.g. "cjdropshipping", "aliexpress_apify", "etsy_api"
    supplier_name: str  # canonical supplier label
    async def fetch_products(self, keywords, country="AU") -> List[RawSupplierProduct]
```

- `ExtractorNotConfiguredError(credential_name)` — credential missing from
  `.env`; the orchestrator moves to the next extractor in the chain.
- `ExtractorBlockedException` — persistent auth failure / connection wall /
  rate-limit block; the chain moves on rather than retrying.
- `ExtractorTimeoutException` — a timeout, distinct from a hard block.

Every extractor skips (never fabricates) hits that cannot yield a fully
formed `RawSupplierProduct` — real PDP URL, usable price, ≥ 3 gallery URLs.

### 4.2 CJdropshipping (`cj_mcp_extractor.py`) — Primary Engine

Zero-bot-surface MCP supplier. Queries the **official CJdropshipping MCP
server** over its remote endpoint — bypassing the Cloudflare Turnstile wall
that blocks every automated frontend read — and evaluates the returned data
stream directly. No HTML is fetched; no bot challenge is answered.

- Connects to `https://developers.cjdropshipping.com/mcp/{CJ_MCP_TOKEN}`
  (MCP token generated on CJ's API Authorization page) using the official
  `mcp` Python SDK over **StreamableHTTP** — the transport CJ documents for
  the remote endpoint. (The SDK's SSE transport is deprecated legacy; do
  not build against it.) The token is never logged: a redacting filter is
  installed on the URL-logging loggers before the first request, because
  the transport logs every request URL at INFO.
- Discovers the tool catalog via `tools/list` on first connection (62 tools
  live) and binds the two the pipeline needs. Exact parameter names are
  mapped from the server's returned tool schemas:
  - product search by target keyword,
  - China-warehouse filtering, **[PINNED FROM LIVE CATALOG]**: the search
    tool's `countryCode` selects the *warehouse* country whose stock backs
    the listing — it is **not** a shipping destination. Passing the AU
    destination returns almost nothing. The China warehouse is
    `isWarehouse=true, countryCode=CN`,
  - an inventory-available filter, **[PINNED FROM LIVE CATALOG]**:
    `startWarehouseInventory=1` ("only products with stock"),
  - result cap from `settings.CJ_MAX_PRODUCTS_PER_KEYWORD` → `pageSize`.
- Executes the product-detail tool for each shortlisted hit to confirm
  pricing/variants and obtain the gallery. **[PINNED FROM LIVE CATALOG]**
  the tool that carries this is **`get_product_detail`**; the catalog's
  `query_sku_details` exists but returns an empty array for catalog product
  ids, so it is only a fallback binding.
- Evaluates each returned payload through the **MCP Payload Liveness Gate**
  (§5.2) *before* constructing a candidate — twice: once cheaply on the
  search hit (so a dead listing never costs a detail round-trip), then on
  the merged search-hit + detail payload.
- Maps verified payloads to `RawSupplierProduct`: PDP URL (CJ's own
  `productUrl` when it publishes one, else canonical
  `https://cjdropshipping.com/product/{pid}.html`), `price_aud` =
  listed USD price × `USD_TO_AUD`, HTML-stripped description, ≥ 3 gallery
  images. Hits without a usable price, gallery, or PDP URL are skipped —
  never fabricated.
- **[PINNED FROM LIVE CATALOG]** CJ pids come in two shapes: numeric
  (`1745360894529376256`) and UUID-style
  (`B03F2DFF-276D-481C-AD18-28DF22E411CC`). The UUID form is a large slice
  of the catalog and is published by CJ under the `{slug}-p-{pid}.html`
  shape, so the PDP-shape validator accepts both pid forms and both URL
  shapes, and the extractor prefers CJ's own published link.
- Chain exceptions: `CjMcpNotConfiguredError` →
  `ExtractorNotConfiguredError("CJ_MCP_TOKEN")`;
  `CjMcpConnectionError` / `CjMcpToolError` → `ExtractorBlockedException`
  (the chain falls through to AliExpress/Etsy).

### 4.3 AliExpress via Apify (`aliexpress_apify.py`)

*Repointed at a pay-per-result actor; the ingestion architecture is
unchanged.* The previous actor (`logical_scrapers/aliexpress-scraper`)
charged a flat monthly rental, which Apify platform credit cannot pay —
the free-tier workflow requires a usage-based actor.

- Runs `cryptosignals/aliexpress-scraper` (pay-per-result, $0.005 per
  product) via `apify-client` 3.x
  (`actor().call(run_input=..., wait_duration=timedelta(...))`; the removed
  `timeout_secs` kwarg must not be used). One **search run per keyword** —
  the actor takes a single `query` string, not a URL list:
  `{"action": "search", "query": <keyword>, "maxItems": N,
  "country": <target country>, "currency": "USD", "sort": "default",
  "proxyConfiguration": {"useApifyProxy": True}}`.
- Maps actor items (`productUrl` matching `/item/\d+\.html`, `title`,
  numeric USD `price`) into candidates. `price` may be a number or a
  `"US $5.32"` string. Items whose `currency` is present and not `USD` are
  **skipped** — the `USD_TO_AUD` conversion below assumes USD.
- **Budget guard:** the actor bills per scraped product, so a run is
  bounded twice — the per-run item cap (`APIFY_MAX_ITEMS_PER_RUN`, default
  100 ≈ $0.50 worst case, further clamped to the actor's own 500 max) and a
  tight `wait_duration` (`APIFY_RUN_TIMEOUT_SECS`, default 60s) so a stuck
  proxy cannot keep billing. The estimated cost is logged at INFO before
  the first call. The Apify account's own monthly spend limit is the
  authoritative ceiling — set it to $5.
- The actor returns only one image per listing, so thin candidates
  (< 3 gallery URLs) are upgraded in place: one headless Chromium session
  (playwright-stealth v2 `Stealth` class — v1's `stealth_async()` free
  function no longer exists) opens each item PDP, harvests the full
  carousel gallery (`div[class*="image-view"]`/`slider` probe, largest
  `srcset` winner), and reads the meta description. A Playwright timeout
  on one PDP skips that candidate only.
- `price_aud = price_usd × USD_TO_AUD`; shipping unquoted (0.0).

### 4.4 Etsy (`etsy_api.py`)

*Unchanged by the MCP migration.*

- Official Open API v3 `GET https://openapi.etsy.com/v3/application/listings/active`
  with `keywords`, `limit`, and `includes=Images` (gallery inline — no
  per-listing follow-up); auth via `x-api-key` (`ETSY_API_KEY`).
- 401/403/429 → `ExtractorBlockedException`; timeout →
  `ExtractorTimeoutException`; other non-200 / non-JSON → keyword skipped.
- Gallery from the listing image resource, best-first:
  `url_fullxfull` → `url_570xN` → `url_570x100` → `url_170x135`; ≥ 3 distinct.
- Currency guard: only USD listings are converted (`currency_code != USD`
  → skipped) so `USD_TO_AUD` math stays like-for-like.
- Listing URL: the API's `url`, falling back to
  `https://www.etsy.com/listing/{id}/`.

## 5. CJ MCP Client & Liveness Protocol (`src/pipeline/cj_mcp_client.py`)

### 5.0 Why the REST client was deleted

The previous design — `cj_client.py` (REST) + `cj_api_extractor.py` + the
Two-Tier Liveness Gate — is being re-architected entirely. Its empirical
track record: the CJ REST API returns identical payloads for dead and live
items (`saleStatus=3`, variants, populated stock — no discriminating field,
including `listedNum`), and CJ fronts every PDP with a Cloudflare Turnstile
wall for all automated clients (httpx, warmed cookies, Playwright,
playwright-stealth — all probed and blocked), so the tier-2 frontend check
was *permanently inconclusive* from this machine. Both exported CJ
candidates passed both gate tiers yet rendered "Product removed" in the
operator's browser. The MCP server replaces the walled frontend as the
absolute source of truth for liveness.

### 5.1 Connection Lifecycle

- **Endpoint:** `https://developers.cjdropshipping.com/mcp/{CJ_MCP_TOKEN}`
  — auth is the MCP token embedded in the URL path (CJ's documented
  remote-connection format). The token is created on CJ's API
  Authorization page and never logged.
- **Transport:** StreamableHTTP via the official `mcp` Python SDK
  (`mcp.client.streamable_http`); the SDK's SSE transport is deprecated
  and must not be used. Base URL (without token) configurable via
  `CJ_MCP_BASE_URL` for testing/proxying.
- **Session:** one client session per pipeline run; connect once, execute
  all keyword/tool calls over it, close on exit. No standard REST rate
  limits or bot challenges are expected on this path — that is the point
  of the pivot; tool-call failures surface as `CjMcpToolError`, never as
  silent retries.
- **Tool discovery:** `tools/list` on first connect pins the actual tool
  names and parameter schemas (62 tools live: `search_products`,
  `get_product_detail`, `get_product_inventory`, plus order/logistics tools
  this pipeline does not use). A tool is resolved by exact name, then
  case-insensitively, then by normalised name/substring — shortest match
  wins so the binding never depends on catalog order. An expected tool that
  cannot be resolved raises `CjMcpToolError` rather than guessing.

### 5.2 MCP Payload Liveness Gate (sole gate)

With the frontend check deleted, the MCP tool response is the **only**
liveness arbiter. `cj_mcp_client.is_mcp_payload_live` validates every
product payload before it becomes a `RawSupplierProduct`:

- **Required signals:** the payload must explicitly confirm (a) the listing
  is active (not removed/delisted) and (b) stock availability — via
  whatever availability/status/inventory fields the payload exposes.
  **[PINNED FROM LIVE CATALOG]** the search hit carries `saleStatus: "3"`
  and `warehouseInventoryNum`; the detail record carries `status: "3"`,
  `productImageSet`, and *null* inventory.
- **Inverted tolerance:** unlike the old REST gate (where *unpopulated*
  stock data was treated as unverifiable-not-dead), a missing or ambiguous
  liveness signal is now a **DROP**. An unrecognised status value is DEAD.
  The MCP stream is the source of truth; "we couldn't tell" is not
  acceptable from it. A detail call that fails or returns an empty payload
  drops the candidate immediately.
- **Merging:** `merge_product_payloads` lets the detail record win where
  both carry a field, and gives it the availability verdict outright when
  it reports stock of its own — so a staler search figure can never rescue
  a listing the detail says is empty. The search hit's
  `warehouseInventoryNum` is carried over only when the detail has no
  populated stock (the normal case, since CJ's detail tool returns null
  inventory).
- **Out-of-stock / delisted items returned by the MCP server are dropped
  strictly** — logged at INFO with pid + reason, never emitted.
- **Residual-risk note (operator-facing):** the REST API's stock fields
  looked identical for dead and live items, so the implementation must
  verify the MCP payloads expose *genuinely distinct* availability signals
  (fresher inventory, explicit removed/delisted flags). Until the stream
  has demonstrated stale-free exports across several runs, keep the
  operator's manual browser spot-check of exported CJ
  `supplier_retail_url`s as the cheap final safeguard. The cross-repo QA
  audit (`my-store-build/scripts/audit_dropship_candidates.py`) continues
  to hard-fail any exported URL on the geo-block signals (`qksource.com`
  host / EU-block body) even on HTTP 200.

### 5.3 Error taxonomy

- `CjMcpNotConfiguredError` — `CJ_MCP_TOKEN` missing from `.env`.
- `CjMcpConnectionError` — connection/session failure against the remote
  endpoint (auth rejected, unreachable, protocol error).
- `CjMcpToolError` — tool execution failed: tool not found in the
  discovered catalog, invalid parameters, or a tool-level error result.
- All three map onto the base extractor chain taxonomy (§4.1); none are
  swallowed silently.

### 5.4 Legacy Two-Tier Liveness Gate — retired

`is_detail_payload_live` (API payload classifier) and
`verify_product_page_liveness` (Turnstile-degraded frontend body reader)
were deleted with `cj_client.py`. Their historical rationale and empirical
limits are preserved in §13 and the lineage table (§14).

## 6. LLM Evaluation (`src/evaluators/llm_filter.py`)

*Unchanged by the MCP migration.*

- **Client:** `instructor.from_openai(AsyncOpenAI(base_url, api_key))`
  against an OpenAI-compatible endpoint (default Ollama Cloud,
  `https://ollama.com/v1`, model `deepseek-v4-flash:cloud` via
  `settings.LLM_MODEL`). Do **not** pass `max_retries` to the constructor —
  with instructor 1.16 + openai 2.x it collides with the patched create_fn
  ("got multiple values for keyword argument"); retries are per-call.
- **Response model:** `ProvisionalProductEvaluation` only. The prompt
  receives title, description (≤ 4000 chars), supplier name, the REAL
  listed price, shipping, and the target country — **never any image URL**,
  and the hard rules forbid the model emitting any URL/domain/item ID
  anywhere.
- **Viability gates** (system prompt, adapted to real supplier data): the
  5-point gate — problem solver/enthusiast niche; margin floor vs the real
  listed cost; AU logistical feasibility (< 1.2 kg, durable, air-freight
  compliant, inferred from title/description only); saturation resistance
  (Kmart/Target/Bunnings/Big W/Woolworths test); 3-second demonstration
  appeal. Failing 2+ points → REJECT.
- **Reconciliation (`_reconcile`):** the verdict is trusted, the arithmetic
  is not — landed COGS is the actual listed price + shipping, and an ACCEPT
  whose retail suggestion misses the floor (markup ≥ 3.0 OR margin >
  AUD 25) is downgraded to REJECT before the final model is constructed.
- **Retry/drop policy:** instructor's bounded per-call retries
  (`max_retries=2`) on schema failure; exhaustion propagates and the
  orchestrator counts it as `dropped_llm_validation_failed` — never
  exported. A final-construction `ValidationError` after reconciliation is
  also downgraded to REJECT defensively.

## 7. Image Sourcing (`src/pipeline/image_sourcing.py`)

*Unchanged by the MCP migration.*

Single, exclusive source: `RawSupplierProduct.image_urls` — the
extractor-captured CDN gallery of the live listing. Zero LLM, zero browser.

- **Size-suffix upgrade:** alicdn URLs are size-suffixed thumbnail
  variants; `strip_size_suffix()` upgrades to the original asset before
  download. Handles the legacy trailing form (`photo.jpg_640x640.jpg`),
  Shopify end-stems (`photo_800x800.jpg`), and alicdn's **mid-filename**
  marker (`S...jpg_960x960q75.jpg_.avif` — the marker must be preceded by
  an image extension). The upgraded original is tried first, the as-served
  URL kept as fallback ordering.
- **Validation gates** (deterministic, never trusting HTML attributes):
  HTTP 200; content-type in `image/jpeg|jpg|png|webp` (AVIF — the format
  alicdn thumbnails serve — rejected); byte-size band 15 KB–15 MB;
  PIL-decoded shortest side ≥ 800 px; SHA-256 byte-hash dedupe so one
  image served twice counts once.
- **Hard 3-image minimum:** fewer than 3 distinct validated images →
  `source_product_images` returns `None` and the exporter drops the
  candidate entirely. No fallback tier to another site exists. Cap: 8
  images per product, 64 candidate URLs considered.

## 8. Exporter (`src/exporter.py`)

*Unchanged by the MCP migration.*

`CandidateExporter.export_candidates(pairs)` takes
`(ProductCandidateEvaluation, RawSupplierProduct)` ACCEPT pairs and, per
candidate:

0. **Duplicate guard:** skip the candidate when its `supplier_retail_url`
   already appears in any existing `product-*/metadata.json` (or in a
   package this call already wrote). Re-running a keyword re-offers the
   same supplier products, and writing them into fresh directories would
   duplicate the catalog. The check runs before any download, so a
   duplicate costs nothing and creates no directory; it is counted as
   `skipped_duplicate` and surfaced in the run summary. A package that
   cannot be read is ignored with a warning rather than aborting the run.
1. `source_product_images(...)` → ≥ 3 validated blobs. Fewer → the
   candidate is dropped (`dropped_no_valid_images` counter incremented,
   logged) and **no directory is created**.
2. Only on success: allocate the next free `product-NN` directory
   (numbering continues past existing ones so re-runs never overwrite),
   write `images/image-N.<ext>` (extension from the blob's decoded
   format), and write `metadata.json`.
3. The 3-image minimum is re-asserted immediately before writing
   (defensive; a violation is a hard internal error).

`metadata.json` contract:

```json
{
  "product_title": "...",
  "category": "...",
  "suggested_price_aud": 49.99,
  "estimated_cogs_aud": 18.60,
  "cogs_estimation_basis": "Supplier listed price AUD $18.60 taken directly from the live CJdropshipping listing; ...",
  "projected_margin_aud": 31.39,
  "marketing_ad_copy": "...",
  "features": ["...", "...", "..."],
  "target_tags": ["dropship", "..."],
  "shipping_notice_au": "Standard tracked international shipping: 7-12 business days",
  "supplier_name": "CJdropshipping",
  "supplier_retail_url": "https://cjdropshipping.com/product/<pid>.html",
  "image_source": "supplier_gallery"
}
```

`image_source` is always `"supplier_gallery"` (retained for provenance
auditing). Non-ACCEPT entries are skipped defensively before any download
or directory creation; one broken candidate never aborts the batch.

## 9. Orchestration (`src/main.py`)

```
python -m src.main --keyword "kitchen gadgets" --target-count 3 \
                   [--country AU] [--extractor {auto,cjdropshipping,aliexpress,etsy}]
```

- **Extractor chain:** built from `settings.SUPPLIER_PRIORITY_ORDER`
  (registry: `cjdropshipping` → CjMcpExtractor, `aliexpress` →
  AliExpressApifyExtractor, `etsy` → EtsyApiExtractor; unknown keys skipped
  with a warning).
  `--extractor <key>` forces one engine. A chain engine that is
  NotConfigured/Blocked/Timeout falls through to the next; if every engine
  fails, `PipelineExtractionFailedError` surfaces the intervention block.
  The LLM filter is constructed **before** extraction so a misconfigured
  `.env` fails fast without burning supplier quota.
- **Evaluation loop:** products are evaluated in scrape order until
  `target_count` ACCEPTs are *exported* (early stop). An ACCEPT only counts
  as in hand once its gallery clears the 3-image gate: when the image stage
  drops candidates the loop keeps pulling from the remaining scrape order
  rather than ending the run with viable products never evaluated. LLM
  call/schema failure → `dropped_llm_validation_failed` counter, continue.
- **Export stage:** exporter's `dropped_no_valid_images` and
  `skipped_duplicate` counters are folded into the summary; exports
  accumulate in `PipelineSummary.exports`. The counters accumulate across
  the run's export rounds — resetting per call would silently discard
  earlier rounds' drops.
- **Funnel counters** (printed every run so regressions are visible
  immediately): `candidates_scraped`, `evaluated`, `accepted`, `rejected`,
  `dropped_llm_validation_failed`, `dropped_no_valid_images`,
  `skipped_duplicate`, `candidates_exported`.
- **Exit codes:** `0` complete (a PARTIAL with fewer exports than the
  target still exits 0, with a `[PARTIAL]` warning); `1` human
  intervention required (all extractors unavailable/blocked, LLM config
  missing, or the funnel exhausted with zero exports); `2` unexpected
  error.
- **Human-intervention protocol:** failures render the
  `[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]` block — `Supplier
  Extraction Stage`, `LLM Evaluation Filter Configuration`, or `Pipeline
  Funnel Exhausted`, each with concrete remediation instructions and the
  exact re-run command. The MCP migration adds no new intervention class:
  MCP auth/connection failures surface through the existing Supplier
  Extraction Stage block.

## 10. Configuration Reference (`.env` / `src/config.py`)

| Key | Default | Used by |
|---|---|---|
| `CJ_MCP_TOKEN` | — | cj_mcp_client — CJ MCP auth (token in URL path) |
| `CJ_MCP_BASE_URL` | `https://developers.cjdropshipping.com/mcp` | cj_mcp_client (token appended) |
| ~~`CJ_API_KEY`~~ | — | *Removed from `Settings`* with the REST client; inert if left in `.env` |
| `APIFY_API_TOKEN` | — | aliexpress_apify |
| `APIFY_ACTOR_ID` | `cryptosignals/aliexpress-scraper` | aliexpress_apify — must be a pay-per-result actor (credit cannot pay a rental) |
| `APIFY_MAX_ITEMS` | `20` | listings per keyword run (AliExpress) |
| `APIFY_MAX_ITEMS_PER_RUN` | `100` | hard item cap per `fetch_products` call (budget guard) |
| `APIFY_PRICE_PER_RESULT_USD` | `0.005` | run cost estimate (the actor's per-result price) |
| `APIFY_RUN_TIMEOUT_SECS` | `60` | Apify `wait_duration` bound |
| `ETSY_API_KEY` | — | etsy_api |
| `LLM_BASE_URL` | — (e.g. `https://ollama.com/v1`) | llm_filter |
| `LLM_API_KEY` | — | llm_filter |
| `LLM_MODEL` | `deepseek-v4-flash:cloud` | llm_filter |
| `SUPPLIER_PRIORITY_ORDER` | `cjdropshipping,aliexpress,etsy` | extractor chain order |
| `USD_TO_AUD` | `1.55` | USD → AUD conversion (all price math) |
| `CJ_MAX_PRODUCTS` | `10` | products expanded per CJ keyword (cap on MCP search results → `pageSize`) |
| `TARGET_COUNTRY` | `AU` | extraction/evaluation target (AU shippability filters) |
| `EXPORT_DIR` | `/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates` | exporter destination |
| `USER_AGENT` | desktop Chrome UA | CDN downloads, Apify harvest |

`Settings` never logs or prints its values (plain class, default repr leaks
nothing); `load_settings()` re-reads the environment so tests can
re-snapshot after `monkeypatch.setenv`.

## 11. Test Suite (`tests/`)

Hermetic by construction — every HTTP/MCP interaction is mocked (fakes,
mocked transports, or a scripted MCP session); extractors, the LLM filter,
and the exporter are scriptable fakes. Currently **292 tests, all
passing**.

| File | Covers |
|---|---|
| `test_models.py` | RawSupplierProduct / provisional / final validators, `from_raw` math, zero-URL rule, PDP-shape rejection (incl. CJ numeric + UUID pids) |
| `test_config.py` | defaults, env overrides, blank-key fallback (a key present but empty resolves to its default), `load_settings()` isolation, CJ MCP token/base-URL resolution, the Apify actor/pricing/per-run-cap/timeout settings, and `.env`↔`.env.example` key-order alignment |
| `test_aliexpress_apify.py` | the actor payload shape and `wait_duration` bound, the `asyncio.to_thread` offload, dataset→`RawSupplierProduct` mapping (incl. the USD currency guard and `productUrl` fallback), price coercion, the hard 3-image rule (harvest upgrade, drop, crash isolation), the per-run budget guard, and the failure taxonomy |
| `test_cj_mcp_client.py` | session/token handling against a scripted MCP session, endpoint/redaction behaviour, `tools/list` discovery and alias binding, schema-driven tool-call serialization, loose JSON decoding (CJ prefixes bodies with prose), result parsing, error taxonomy mapping (incl. the bounded rate-limit retry), the search-hit/detail merge rule, and the MCP Payload Liveness Gate (out-of-stock / delisted / missing-availability / ambiguous payloads strictly dropped) |
| `test_cj_mcp_extractor.py` | `RawSupplierProduct` construction from mock MCP payloads (PDP URL incl. UUID pids, USD→AUD price, gallery ≥ 3), search parameters actually sent, liveness-gate drops wired through the extractor, no-price/no-gallery/no-pid skips, chain exception mapping (NotConfigured / Blocked), and one end-to-end pass through the real client |
| `test_llm_filter.py` | message construction, reconcile downgrade, retry/drop policy |
| `test_image_sourcing.py` | validation gates, dedupe, `strip_size_suffix` (incl. alicdn mid-filename form), 3-image minimum |
| `test_exporter.py` | export contract, 3-image gate, metadata keys, no-partial-directory rule, cross-run duplicate guard (skip without a directory, fresh URLs still export, counter accumulation) |
| `test_main.py` | extractor chain, funnel counting, early stop, intervention blocks, CLI exit codes |

```bash
source .venv/bin/activate
pytest tests/ -v        # expect: all pass, zero network
```

## 12. Environment Setup

- Python 3.13 venv at `.venv/` (Homebrew `python3.13`; system python is too
  old for current pydantic/instructor). Install with
  `pip install -e ".[dev]"`; pinned versions live in `pyproject.toml`.
- The official `mcp` Python SDK (`mcp==2.2.0`, StreamableHTTP client) is a
  declared dependency; `apify-client`, `instructor`, `httpx`, and
  `playwright` (AliExpress harvest only) stay as pinned.
- playwright-stealth **must stay ≥ 2.0** (the `Stealth` class API; v1's
  `stealth_async()` no longer exists). Correct pattern:
  ```python
  from playwright_stealth import Stealth
  await Stealth(navigator_user_agent_override=settings.USER_AGENT) \
      .apply_stealth_async(context)
  ```
- Playwright Chromium binary: `python -m playwright install chromium`.

## 13. Operational Notes & Known Limits

- **CJ liveness story:** the retired Two-Tier Gate is superseded by the MCP
  pivot (§5.0). Empirical record motivating it: both exported REST-era CJ
  candidates (pids `2097572473520115714` "Desktop Air Cooler" and
  `2097985041113341954` "Knife Sharpener Pro") passed tier-1 and were
  tier-2-inconclusive, yet rendered "Product removed" in the operator's
  browser. The MCP stream is now the sole liveness arbiter; verify its
  availability signals genuinely differ from the REST API's over the first
  few runs, and keep the manual browser spot-check as the cheap final
  safeguard meanwhile. The Turnstile wall remains irrelevant to ingestion
  (no frontend reads) — a real human browser session passes it; automated
  clients do not, and no automated client ever needs to.
- **CJ MCP specifics (pinned from the live catalog):** the warehouse filter
  is `isWarehouse=true` + `countryCode=CN` (`countryCode` is a *warehouse*
  country, not a destination); the inventory filter is
  `startWarehouseInventory=1`; the detail tool is `get_product_detail`
  (`query_sku_details` returns `[]` for catalog pids). CJ prefixes every
  tool body with prose ("📋 Found N products total.") before the JSON, so
  payloads are decoded loosely from the first brace. Rate limits are not
  publicly documented and are real in practice — a single 429-style tool
  error is retried once after a 2s backoff, then surfaces as
  `CjMcpToolError`.
- **CJ MCP token hygiene:** the token travels in the endpoint URL path and
  the HTTP transport logs request URLs at INFO, so `cj_mcp_client` installs
  a redacting filter on the URL-logging loggers before connecting. Without
  it, a plain `logging.INFO` run writes the credential to stdout.
- **CJ gallery quality:** older CJ listings routinely serve sub-800px
  galleries, so the hard 3-image gate legitimately drops them. With
  `CJ_MAX_PRODUCTS=10` the pipeline can therefore need several candidates
  before one exports — that is the gate working, not a failure.
- **Apify budget model:** the AliExpress actor is **pay-per-result**
  ($0.005/product), so free-tier platform credit covers it — a rental actor
  ($20–25/month) does **not**, since credit cannot pay a rental fee. Set
  the Apify Console monthly spend limit to $5 as the authoritative ceiling;
  the in-code per-run item cap plus the logged cost estimate are the early
  warning. The actor's own docs say residential proxies give consistent
  results, which free-tier API runs cannot use, so a live run may be
  blocked — that surfaces as `ExtractorBlockedException` and degrades
  gracefully to the next extractor in the chain.
- **Cross-repo QA:** the Shopify workspace audit script re-verifies every
  exported `supplier_retail_url` against the geo-block signals and
  hard-fails even on HTTP 200.

## 14. Design Lineage (traceability)

Prior remediation plans accumulated in this file (v1–v6) were pruned on
2026-09-11. What each contributed that survives:

| Plan | Contribution still in the code |
|---|---|
| v2 (PART B) | The anti-hallucination contract itself: no image-URL fields on evaluation models; hard drop-rules instead of placeholder data; funnel counters in the run summary |
| v3 (PART C) | Zero-URL `cogs_estimation_basis` validator (any `https?://` substring rejected, field-scoped for instructor retries); `deepseek-v4-flash:cloud` as the evaluation model |
| v4 (PART D) | Direct-product-page-only sourcing; PDP-shape validation per supplier; single-source image rule with the hard 3-image minimum; exporter ordering (images verified before any directory is created). Its `supplier_sourcing.py` module was later deleted by the Supplier-First pivot (v6) |
| v4/v5 (PART D/E) | Zero-URL + search-gateway URL regexes; `image_source: "supplier_gallery"` provenance field |
| v5 (PART E) | CJ `countryCode` regional filter; qksource.com / EU-block geo-block signals; alicdn mid-filename size-suffix fix in `strip_size_suffix()` |
| v6 (PART F) | The Supplier-First architecture itself: extractors → LLM → deterministic images → export; `RawSupplierProduct`; programmatic COGS/supplier mapping |
| Post-v6 | **Two-Tier Liveness Gate** — tier-1 API payload gate + tier-2 frontend body gate; response to stale CJ items exporting as winners. **Retired by the MCP migration** after both exported CJ candidates proved stale despite passing it |
| **MCP migration (2026-09-11)** | CJ ingestion re-architected onto the official CJdropshipping MCP server: `cj_mcp_client.py` (StreamableHTTP, token-in-URL auth + log redaction, schema-driven tool binding, MCP Payload Liveness Gate, `CjMcp*` error taxonomy) + `cj_mcp_extractor.py`; deletes `cj_client.py` / `cj_api_extractor.py` and the Two-Tier Gate; AliExpress and Etsy untouched. Also fixed the evaluation loop so an ACCEPT that dies at the image gate does not end the run with products still unevaluated, and added the exporter's cross-run duplicate guard (`skipped_duplicate`) so re-running a keyword cannot duplicate the catalog |
| **Apify actor repoint (2026-09-17)** | AliExpress ingestion moved off the rental actor `logical_scrapers/aliexpress-scraper` ($20/month, unpayable from platform credit) onto the pay-per-result `cryptosignals/aliexpress-scraper` ($0.005/product): one `action: "search"` run per keyword, USD-only currency guard, a per-run item cap + logged cost estimate as the budget guard, and a 60s `wait_duration` default. First test coverage for the extractor (`test_aliexpress_apify.py`, a scripted `apify_client` fake) |
| **Live-run hardening (2026-09-18)** | Two defects the hermetic suite could not catch, found by the first real ingestion run: (1) `apify-client` 3.x returns a pydantic `Run` model, not a dict, so `(run or {}).get("defaultDatasetId")` raised `AttributeError` — now `_run_dataset_id()` reads both shapes, and the test fake can return the real model shape; (2) the actor's `productUrl` carries a search-tracking query string (`algo_pvid`, `pdp_npi`, `search_p4p_id`, …) that was being exported verbatim as the fulfilment link — now `_canonical_product_url()` reduces it to `/item/<id>.html` |

Removed as no longer applicable: the Meta Ad Library scraper and its
landing-page resolution protocol, `ScrapedAdData`,
`competitor_store_url`/`competitor_retail_url`,
`supplier_search_queries`/`aliexpress_search_url`, `SupplierSearchSeed`,
the 3-tier image pipeline (ad-creative / competitor-gallery /
supplier-search), similarity-scored supplier matching, and the
`SUPPLIER_MATCH_SIMILARITY_THRESHOLD` setting — plus, removed by the MCP
migration, `cj_client.py`, `cj_api_extractor.py`, the Two-Tier Liveness
Gate, and the `CJ_API_KEY`-only auth path.