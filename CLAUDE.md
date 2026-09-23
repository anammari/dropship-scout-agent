# CLAUDE.md - Dropship Scout Agent (Supplier-First Architecture)

> Aligned with the implemented codebase as of 2026-09-23. The full
> engineering spec — schemas, validator code, liveness-gate rules, config
> reference — lives in `_docs/plan.md`. This file is the operating mandate
> and the single source of operating context; there is no companion
> session file.

## 1. MISSION & HIGH-LEVEL OBJECTIVE

The **Dropship Scout Agent** scouts dropshipping product candidates against
strict commercial and logistical criteria and exports 3–5 validated,
high-margin winning product packages directly into the Shopify workspace:

```
/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/
```

The pipeline ingests **live, verified products directly from supplier
APIs/scrapers** (Supplier-First architecture): every candidate starts with a
real supplier product URL, a real listed price, and the supplier's own image
gallery *before* the LLM ever sees it. CJdropshipping is read through its
official **MCP server** (StreamableHTTP) — see §5.

```
[Stage 1] Supplier extractors (chain, SUPPLIER_PRIORITY_ORDER)
          CJdropshipping MCP → AliExpress Dropshipping Center → Etsy Open API v3
          CJ hits must pass the MCP Payload Liveness Gate (§5)
          AliExpress hits must pass the Winning-Product Gate (§6)
                         │  List[RawSupplierProduct]  (verified ground truth)
                         ▼
[Stage 2] LLM viability & marketing evaluation (deepseek-v4-flash:cloud)
                         ▼
[Stage 3] Deterministic CDN image download (image_sourcing.py, zero LLM)
                         ▼
[Stage 4] Workspace export (exporter.py): product-NN/{metadata.json, images/}
```

## 2. ANTI-HALLUCINATION CONTRACT (MANDATORY, validator-enforced)

1. **The LLM never sources, authors, edits, or invents media URLs — ever.**
   No image-URL field exists on any evaluation model. Imagery flows from
   `RawSupplierProduct.image_urls` through `image_sourcing.py` directly.
2. **The LLM never authors** `supplier_name`, `supplier_retail_url`,
   `estimated_cogs_aud`, or `cogs_estimation_basis` — those are mapped
   programmatically from the verified raw product via
   `ProductCandidateEvaluation.from_raw`.
3. **`cogs_estimation_basis` is zero-URL** — any `http(s)://` substring is
   rejected by a Pydantic field validator; the field carries only numerical
   cost/materials/freight reasoning.
4. **LLM arithmetic is never trusted:** margin/markup are recomputed in code,
   and an ACCEPT whose reconciled figures miss the margin floor
   (`MIN_MARKUP_MULTIPLIER` ≥ 2.5 OR margin > `MIN_MARGIN_AUD` 20) is
   downgraded to REJECT.
5. **Every supplier URL must match its supplier's direct-product-page
   shape** — search/category/gateway URLs are rejected at the schema level:

   | Supplier | Required URL shape |
   |---|---|
   | AliExpress | `/item/<id>.html` |
   | Etsy | `/listing/<id>/` |
   | CJdropshipping | `/product/<pid>.html` or `/product/<slug>-p-<pid>.html`; `<pid>` is numeric **or** UUID-form (`B03F2DFF-276D-481C-AD18-28DF22E411CC`) |

6. **Strict schema validation over prompt trust:** instructor/Pydantic
   validators reject and force a retry (or downgrade to REJECT) on any
   violating LLM output.

## 3. THE 5-POINT EVALUATION GATE (LLM system prompt)

Any candidate failing two or more points is marked `REJECT`:

1. **Problem Solver or Emotional Trigger** — solves an active discomfort or
   serves a high-passion enthusiast niche.
2. **Margin Viability (AUD)** — priced for **realistic Australian retail** in
   the store's "Modern Arab-Aussie Lifestyle & Cultural Nostalgia" niche,
   against the **strict landed cost** read from the supplier record: never a
   cheaper invented basis, and never a mechanical 3x–4x multiplier on cost.
   The honest price must clear `MIN_MARKUP_MULTIPLIER` (2.5x) **or** leave
   `MIN_MARGIN_AUD` (AUD 20) gross profit per unit; a product whose realistic
   AU price cannot clear that floor is REJECTed on this gate rather than
   priced up to fit (enforced twice: `_reconcile` downgrade in
   `llm_filter.py` and `_enforce_accept_gates` in `models.py`, both reading
   the same config keys).
3. **Australian Logistical Feasibility** — < 1.2 kg, durable, non-perishable,
   air-freight compliant (inferred from title/description only).
4. **Local Saturation Resistance** — not an everyday Kmart/Target/Bunnings/
   Big W/Woolworths commodity.
5. **Demonstration Appeal** — a 3-second visual problem→solution dynamic.

## 4. WORKSPACE STRUCTURE

```
dropship-scout-agent/
├── CLAUDE.md
├── pyproject.toml
├── .env.example / .env      # credentials via python-dotenv; real env wins
├── src/
│   ├── config.py            # env-driven Settings + load_settings() factory
│   ├── models.py            # RawSupplierProduct, ProvisionalProductEvaluation,
│   │                        #   ProductCandidateEvaluation (+ from_raw)
│   ├── extractors/
│   │   ├── base.py          # BaseSupplierExtractor + shared exceptions
│   │   ├── cj_mcp_extractor.py      # CJdropshipping MCP + liveness gate
│   │   ├── aliexpress_ds.py         # native DS Center ingestion + winner gate
│   │   └── etsy_api.py              # Etsy Open API v3
│   ├── evaluators/llm_filter.py     # instructor + ProvisionalProductEvaluation
│   ├── pipeline/
│   │   ├── cj_mcp_client.py # CJ MCP client + MCP Payload Liveness Gate
│   │   └── image_sourcing.py# deterministic CDN image download/validation
│   ├── exporter.py          # workspace writer (metadata.json + images/)
│   └── main.py              # CLI orchestrator, funnel counters, exit codes
├── scripts/
│   └── generate_ali_session.py  # optional saved DS Center login (§6)
└── tests/                   # 318 hermetic tests, zero network (9 modules + conftest)
```

**Retired pipelines — do not rebuild.** The Meta Ad Library scraper
(`playwright_scraper.py`, `apify_scraper.py`), fuzzy-match sourcing
(`pipeline/supplier_sourcing.py`), the CJ REST client and extractor
(`pipeline/cj_client.py`, `extractors/cj_api_extractor.py`), the
Apify-hosted AliExpress scraper (`extractors/aliexpress_apify.py`, with its
pay-per-result actor, its US-market unauthenticated welcome-deal pricing and
every `APIFY_*` key) and the per-candidate DS Center gate it carried
(`ENABLE_DS_CENTER_GATE`) are deleted. AliExpress is
read natively from the Dropshipping Center (§6), CJ is MCP-only (§5), and
`supplier_retail_url` is the sole retail link in the export contract (§8).
Costing rationale for the replacement: §12. Deletion history:
`_docs/plan.md` §14.

## 5. CJ MCP PAYLOAD LIVENESS GATE (MANDATORY)

> **Why this gate is strict:** a delisted CJ item's payload can still look
> fully alive (active status, variants, non-zero stock) while its PDP renders
> "Product removed", and CJ fronts every PDP with Cloudflare Turnstile for
> automated clients — so no frontend read can settle liveness. The MCP
> stream is therefore the sole arbiter: no HTML is fetched and no bot
> challenge is answered on this path.

Every CJ hit runs the gate in `cj_mcp_extractor._build_product` before a
candidate is emitted — twice: once cheaply on the search hit (so a dead
listing never costs a detail round-trip), then again on the merged
search-hit + product-detail payload.

`cj_mcp_client.is_mcp_payload_live(payload, target_country)` is **LIVE only
when the payload explicitly confirms both**:

1. **The listing is active** — a recognised active status value
   (`saleStatus`/`entryStatus`/`status`) or a truthy listing flag, with no
   explicit delist flag set. An *unrecognised* status value is DEAD.
2. **Stock availability** — at least one recognised inventory field with a
   positive quantity, target country preferred. A payload with no
   recognised inventory field at all is DEAD.

**Inverted tolerance:** unpopulated stock is not "unverifiable" — "we
couldn't tell" is a DROP. A failed or empty detail call drops the candidate
on the spot.

**Merging rule** (`merge_product_payloads`): the detail record wins where
both carry a field, and it owns the availability verdict outright when it
reports stock of its own (so a staler search figure can never rescue a
listing the detail says is empty). Because CJ's detail tool returns *null*
inventory for catalog products, the search hit's `warehouseInventoryNum` is
carried over only when the detail has no populated stock.

**Residual risk (operator-facing):** the MCP stream has not yet
demonstrated stale-free exports across several runs. Keep the operator's
manual browser spot-check of exported CJ `supplier_retail_url`s as the
cheap final safeguard. The cross-repo QA audit
(`my-store-build/scripts/audit_dropship_candidates.py`) continues to
hard-fail any exported URL on the geo-block signals (`qksource.com` host /
EU-block body) even on HTTP 200.

## 6. SUPPLIER EXTRACTORS

All extractors subclass `BaseSupplierExtractor.fetch_products(keywords,
country="AU") -> List[RawSupplierProduct]` and share one exception taxonomy:
`ExtractorNotConfiguredError` (missing credential — chain moves on),
`ExtractorBlockedException` (auth/rate-limit/anti-bot wall — chain moves
on), `ExtractorTimeoutException`. Every extractor **skips, never
fabricates**, hits that cannot yield a fully-formed product (real PDP URL,
usable price, ≥ 3 gallery URLs).

- **`cj_mcp_extractor.py` (primary, `engine_name="cjdropshipping"`):**
  connects to the official CJ MCP server over StreamableHTTP
  (`{CJ_MCP_BASE_URL}/{CJ_MCP_TOKEN}`, token never logged), discovers the
  tool catalog via `tools/list`, then per keyword runs the product-search
  tool with the China-warehouse mapping (`isWarehouse=true`,
  `countryCode=CN`) plus the inventory filter
  (`startWarehouseInventory=1`). The returned hits then pass the **CJ
  commercial gate** (§6.2) before anything else, so an unproven seller never
  costs a detail round-trip. Each surviving hit is expanded through the
  **product-detail** tool (`get_product_detail`), the liveness
  gate runs, the gallery comes from `productImageSet`, the description is
  HTML-stripped, and `price_aud = listed USD × USD_TO_AUD`. The PDP link
  prefers CJ's own `productUrl` (the only known-good form for UUID pids),
  falling back to the canonical `/product/{pid}.html`.
- **`aliexpress_ds.py` (native, `engine_name="aliexpress_ds_center"`):**
  reads the AliExpress **Dropshipping Center's** own MTOP H5 APIs through a
  stealth Playwright context's request jar — the same internal calls its
  React UI makes, so no HTML scraping, no SPA driving, no per-result
  billing. Per keyword it calls
  `mtop.aidc.ds.center.selection.search` (`sort=ORDERS_DESC`, page size
  `ALI_DS_MAX_PRODUCTS`) and expands every hit through
  `mtop.aidc.ds.center.selection.queryByItemUrl` for the item's
  real AU-market record. Both ride MTOP's token-then-sign handshake
  (appKey `12574478`; the first call primes the `_m_h5_tk` cookie unsigned,
  the second signs `md5(token&t&appKey&data)`), responses are
  JSONP-tolerant, and the context carries AliExpress's ship-to cookie
  (`aep_usuc_f`, `site=glo&region=<run country>&b_locale=en_US`) because the
  DS Center's catalogue and prices are market-specific — the same item is
  quoted US $5.23 in the default market and US $7.22 for AU, so the pin is
  load-bearing. The record's minor-unit price plus its quoted currency drive
  `price_aud` (USD → `USD_TO_AUD`; AUD as-is; any other currency skipped
  rather than mispriced). Every hit must then clear the **Winning-Product
  Gate** (§6.1), survivors are sorted by order volume descending, and each
  survivor's PDP is harvested once for its gallery + description: the record
  itself carries only a single `itemMainPic`, so the full carousel comes from
  that one page load (`_GALLERY_JS` / `_META_DESCRIPTION_JS` probes;
  `Stealth` class API — playwright-stealth ≥ 2.0, v1's `stealth_async()` no
  longer exists). The saved session (`ALI_DS_STATE_PATH`) is optional —
  anonymous AU access returns byte-identical data — so it is injected only
  when the file exists and its absence is never an error; a session/auth
  refusal (`FAIL_SYS_SESSION_EXPIRED`, `FAIL_SYS_USER_VALIDATE`,
  `FAIL_SYS_ILLEGAL_ACCESS`) or a login-page redirect raises
  `DsCenterSessionExpiredError`, which the CLI renders as an intervention
  block with the recovery steps.

- **`etsy_api.py`:** Open API v3 `listings/active` with `includes=Images`
  (`x-api-key`); best-first gallery keys (`url_fullxfull` → …); 401/403/429
  → Blocked; **USD-only listings** are converted (currency guard), others
  skipped.

### 6.1 WINNING-PRODUCT GATE (AliExpress, MANDATORY)

Enforced in the extractor on every search hit's item record — before the LLM
and before any image work — so no weak seller ever costs a downstream call:

| Config | Default | Drop behaviour |
|---|---|---|
| `MIN_DS_ORDER_COUNT` | `500` | logs `Skipping <ID>: Insufficient order volume (<count>)` |
| `MIN_DS_RATING` | `4.5` | logs `Skipping <ID>: Rating too low (<rating>)` |

Orders arrive as display text (`"148 sold"`, `"10000+ sold"`) and are read at
their floor; rating is the record's `score`. **A metric the DS Center does
not report is treated as unproven and the item is dropped** (logged
`…(unavailable)` / `…Rating unavailable`) — the same inverted tolerance §5
applies to unverifiable CJ stock. Survivors are sorted by order volume
descending, so the target count fills with the strongest sellers first.

### 6.2 CJ COMMERCIAL GATE (CJdropshipping, MANDATORY)

The CJ mirror of §6.1 — same shape (a quantitative pre-LLM gate with
fail-closed semantics and a descending sort), but **single-metric by
necessity.** CJ's MCP surface publishes no historical-sales figure at all:
a live spike of `search_products` and `get_product_detail` found no
`sellNum` / `sales` / `soldNum` / `orderNum` under any name, and none of the
62 advertised tools carries one (the order tools report the *operator's own*
orders, not market demand). `variantVolume` is a decoy — it is the variant's
volumetric freight dimension in mm³, never a sales figure.

The dropshipper listing count is therefore the only demand proof available:

| Config | Default | Drop behaviour |
|---|---|---|
| `MIN_CJ_LISTED_COUNT` | `150` | logs `Skipping <ID>: Insufficient CJ list count (<count>)` |

`listedNum` arrives as a plain integer on the search hit, so the gate runs on
the raw hits **before** the `get_product_detail` expansion. A hit that does
not report a count is unproven and dropped (logged `…(unavailable)`), per the
same inverted tolerance §5 applies to stock. Survivors are sorted by listing
count descending, so the detail expansion and the LLM meet the most widely
listed products first — the run logs them once at INFO
(`CJ commercial gate passed <kept>/<total> hit(s) … (listed counts, strongest
first: […])`), which is the direct evidence the ranking ran. The pool is the
search page CJ already returned (capped at `CJ_MAX_PRODUCTS`), so this
re-ranks that page, not CJ's catalogue.

**This is a commercial verdict, not a liveness one.** `listedNum` cannot
settle whether a listing is still live — §5.0 records exactly that: dead and
live REST payloads were indistinguishable on this and every other field. A
hit that clears this gate must still pass the §5 liveness gate, which remains
the sole arbiter of availability.

## 7. IMAGE SOURCING (`src/pipeline/image_sourcing.py`)

Single, exclusive source: `RawSupplierProduct.image_urls` (the
extractor-captured CDN gallery). Zero LLM, zero browser.

- `strip_size_suffix()` upgrades alicdn thumbnail URLs to the original
  asset before download (legacy trailing form, Shopify end-stems, and the
  alicdn **mid-filename** marker `S...jpg_960x960q75.jpg_.avif`).
- Validation gates: HTTP 200; content-type `image/jpeg|png|webp` (AVIF
  rejected); 15 KB–15 MB; PIL-decoded shortest side ≥ 800 px; SHA-256
  byte-hash dedupe.
- **Hard 3-image minimum:** fewer than 3 distinct validated images → the
  candidate is dropped, no fallback tier, no partial export. Cap 8 images,
  64 candidate URLs.

## 8. EXPORT CONTRACT (`src/exporter.py`)

Per ACCEPT pair `(ProductCandidateEvaluation, RawSupplierProduct)`:
the candidate is first checked against every existing `product-*/metadata.json`
— a `supplier_retail_url` already present in an exported package is skipped
(`skipped_duplicate`, no directory, no download), because re-running the same
keyword re-offers the same supplier products. Then images
verified first, then the next free `product-NN` directory is allocated
(numbering continues past existing dirs — re-runs never overwrite) and
`metadata.json` + `images/image-N.<ext>` are written. No images → no
directory, candidate counted as `dropped_no_valid_images`.

`metadata.json` (exact exporter keys):

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

`supplier_retail_url` is the **sole fulfillment link** (verified, live,
exact-match PDP — never a search gateway). `cogs_estimation_basis` is an
internal accounting note (zero URLs). `image_source` is always
`"supplier_gallery"`.

## 9. ORCHESTRATION (`src/main.py`)

```bash
python -m src.main --keyword "kitchen gadgets" --target-count 3 \
                   [--country AU] [--extractor {auto,cjdropshipping,aliexpress,etsy}]
```

- The LLM filter is constructed **before** extraction (fail-fast on a bad
  `.env`). The extractor chain follows `SUPPLIER_PRIORITY_ORDER`; a chain
  engine that is NotConfigured/Blocked/Timeout falls through to the next;
  `--extractor <key>` forces one engine.
- Products are evaluated in scrape order until `target_count` ACCEPTs are
  *exported* (early stop). An ACCEPT only counts once its gallery clears the
  3-image gate: when the image stage drops candidates, the loop keeps
  pulling from the remaining scrape order instead of ending the run with
  viable products never evaluated. LLM schema failure →
  `dropped_llm_validation_failed`, continue.
- **Funnel counters printed every run:** `candidates_scraped`, `evaluated`,
  `accepted`, `rejected`, `dropped_llm_validation_failed`,
  `dropped_no_valid_images`, `skipped_duplicate`, `candidates_exported`.
  The exporter's counters accumulate across the run's export rounds (the
  orchestrator may export once per evaluation round), so no round's drops
  are discarded.
- **Exit codes:** `0` complete (a `[PARTIAL]` with fewer exports than the
  target still exits 0), `1` human intervention required, `2` unexpected
  error.

### Human intervention protocol

On an unrecoverable boundary, render:

```
[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]
------------------------------------------------------------
Step: <step name>
Reason: <concrete cause>

Instructions for You:
1. <exact action>
2. <command/config to run>
3. <confirmation to paste back>
------------------------------------------------------------
```

Current triggers: **Supplier Extraction Stage** (all extractors
unavailable/blocked), **LLM Evaluation Filter Configuration** (missing LLM
env config), **AliExpress Dropshipping Center Session** (the DS Center
refused the client — re-run `scripts/generate_ali_session.py`, or delete
`ALI_DS_STATE_PATH` and run anonymously), **Pipeline Funnel Exhausted** (zero
exports after all gates). Drops at individual gates are surfaced in the run
summary, not as interventions.

### Optional DS Center session script

```bash
source .venv/bin/activate && python scripts/generate_ali_session.py
```

Opens a non-headless stealth Chromium (Playwright's bundled build — the
operator's own Chrome profile and tabs are untouched), lets the operator log
in and open the Dropshipping Center by hand, then writes the context's
`storage_state` to `ALI_DS_STATE_PATH`. Needed only if AliExpress starts
requiring a login for the MTOP calls in §6.

## 10. CONFIGURATION (`.env`, read by `src/config.py`)

| Key | Default | Used by |
|---|---|---|
| `CJ_MCP_TOKEN` | — | cj_mcp_client / cj_mcp_extractor (primary; token in URL path) |
| `CJ_MCP_BASE_URL` | `https://developers.cjdropshipping.com/mcp` | cj_mcp_client (token appended) |
| `ALI_DS_STATE_PATH` | `ali_ds_state.json` | aliexpress_ds (optional saved session; injected only if present) |
| `ALI_DS_MAX_PRODUCTS` | `20` | DS Center search page size / per-keyword expansion cap |
| `MIN_DS_ORDER_COUNT` | `500` | aliExpress winning-product gate (historical orders) |
| `MIN_DS_RATING` | `4.5` | winning-product gate (rating out of 5) |
| `ETSY_API_KEY` | — | etsy_api |
| `LLM_BASE_URL` / `LLM_API_KEY` | — | llm_filter |
| `LLM_MODEL` | `deepseek-v4-flash:cloud` | llm_filter |
| `SUPPLIER_PRIORITY_ORDER` | `cjdropshipping,aliexpress,etsy` | extractor chain |
| `USD_TO_AUD` | `1.55` | all USD→AUD price math |
| `MIN_CJ_LISTED_COUNT` | `150` | CJ commercial gate (minimum `listedNum`; CJ reports no sales figure) |
| `CJ_MAX_PRODUCTS` | `10` | products expanded per CJ keyword (MCP search cap) |
| `MIN_MARKUP_MULTIPLIER` | `2.5` | margin floor (markup leg), llm_filter + models |
| `MIN_MARGIN_AUD` | `20.0` | margin floor (gross-profit leg), llm_filter + models |
| `TARGET_COUNTRY` | `AU` | extraction/evaluation target; also the AliExpress ship-to market |
| `EXPORT_DIR` | `…/my-store-build/inspiration/dropship-candidates` | exporter |
| `USER_AGENT` | desktop Chrome UA | CDN downloads, Playwright PDP harvest |

Every key in `.env.example` is present in `.env` in the same order with the
same description; a key that is commented out **or present but blank**
resolves to its default in `src/config.py` (blank never means `0`/`False`).

`Settings` never logs or prints its values (no-leak repr); credentials live
only in `.env` / the real environment.

## 11. TESTS & ENVIRONMENT

- Hermetic suite: `source .venv/bin/activate && pytest tests/ -v` — 318
  tests, zero network (httpx.MockTransport + fake MCP sessions + scripted
  Playwright/MTOP fakes).
- `tests/test_aliexpress_ds.py` covers the payload decoding (plain and
  JSONP), order/rating parsing, the currency guard and AUD conversion, the
  §6.1 gate and each documented drop log, the MTOP priming-then-signed
  handshake, ordering by order volume, dedupe across keywords, the
  session-expiry and blocked-exchange paths, the optional state file, and the
  PDP gallery/description harvest. `tests/test_config.py` covers every key in
  §10.
- **Live evidence (2026-09-21, read-only):** `garlic grater` → 20 search hits
  → 18 gated out → 2 kept, both harvested with 13 gallery URLs; a full
  pipeline run exported one package at a real DS cost of **AUD 3.86** and the
  other candidate was REJECTed by the LLM at 2.01x markup — the realistic-AU
  pricing rule of §3 doing its job.
- **CJ payload spike (2026-09-23, read-only):** `garlic grater` → 10 search
  hits and `kitchen gadgets` → 10 hits, the first expanded once through
  `get_product_detail`, confirm `listedNum` is present and numeric on 10/10
  hits on both records (observed 15–1071 and 440–4175 respectively), while
  every candidate sales key (`sellNum`, `sellCount`, `sales`, `soldNum`,
  `soldCount`, `orderNum`, `importNum`) is absent from both. At
  `MIN_CJ_LISTED_COUNT=20` every hit on both keywords passed — the gate
  filtered nothing, which is why the default is 150 (§6.2); at 150 the
  weakest `garlic grater` hit (15) drops and the remaining nine survive.
- Python 3.13 venv at `.venv/`; install with `pip install -e ".[dev]"`;
  Playwright Chromium: `python -m playwright install chromium` (used by the
  AliExpress DS Center calls and the PDP gallery harvest).
- The `mcp` SDK is a declared dependency (StreamableHTTP client only).

## 12. OPERATIONAL NOTES

- **CJ liveness:** the MCP stream is the sole arbiter (no frontend reads, no
  Turnstile). Manual browser spot-checks of exported CJ
  `supplier_retail_url`s remain the cheap final safeguard until the stream
  has demonstrated stale-free exports across several runs.
- **CJ MCP parameters are pinned from the live catalog** — `countryCode` on
  `search_products` is a WAREHOUSE-country filter, not a shipping
  destination (passing `AU` returns almost nothing); the China warehouse is
  `isWarehouse=true, countryCode=CN`; the inventory filter is
  `startWarehouseInventory=1`. The detail tool is `get_product_detail` (the
  plan's `query_sku_details` returns `[]` for catalog pids).
- **CJ MCP rate limits:** tool calls get 429-style errors under load; the
  client retries once after a 2s backoff before surfacing `CjMcpToolError`.
  `CJ_MAX_PRODUCTS=10` means up to 10 detail calls per keyword, so a run
  takes minutes — that is the pacing, not a hang.
- **Why the Apify path was retired (three failures, all costing):** an
  unauthenticated, new-user context is fed subsidised SuperDeals "welcome
  deal" prices — one exported package was costed at **AUD 1.53** against the
  DS Center's **US $7.22 (AUD 11.19)** for the same item, roughly 7x off,
  which the LLM then marked up into a hallucinated retail price; the actor
  priced against a **US** ship-to context and ignored `--country AU`, so
  logistics were validated for the wrong market; and it billed **per result**
  with a cold-container start that could exceed the run timeout.
- **AliExpress cost basis:** the DS Center's AU-market quote is the
  authoritative landed cost. The market pin (`aep_usuc_f`) is what keeps it
  honest — the same item is quoted differently for every market — so never
  run the DS Center calls without it.
- **Static FX, by design:** `price_aud` converts the record's USD with the
  static `USD_TO_AUD` (1.55). The DS Center UI shows its own live daily rate,
  so the operator will see a small gap (observed: AUD 3.86 ingested vs AUD
  3.64 on screen). It is commercially negligible for margin work and is
  deliberately not "fixed" — a live FX feed adds a failure mode for ~6%
  on a cost basis that the relaxed floor of §3 already absorbs.
- **AliExpress pacing:** each search hit costs one item-record round trip
  (~2-5 s), so `ALI_DS_MAX_PRODUCTS=20` means a keyword takes roughly 1-2
  minutes before the PDP harvest, which adds one page load per survivor.
  That is the pacing, not a hang. A failed search exchange raises
  `ExtractorBlockedException` (the chain moves on) rather than reporting an
  empty funnel, and a session/auth refusal raises
  `DsCenterSessionExpiredError`, which halts with re-login instructions.