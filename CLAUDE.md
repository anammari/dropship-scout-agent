# CLAUDE.md - Dropship Scout Agent (Supplier-First Architecture)

> Aligned with the implemented codebase as of 2026-09-23. This file is the
> operating mandate and the single source of operating context: it carries
> the schemas, gate rules, config reference and operational notes. The
> retired `_docs/plan.md` engineering spec was deleted — it described the
> pre-MCP implementation and had drifted out of date.

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
          CJdropshipping MCP → AliExpress Dropshipping Center
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
   `estimated_cogs_aud`, `cogs_estimation_basis`, or `shipping_notice_au` —
   those are mapped or derived programmatically from the verified raw product
   via `ProductCandidateEvaluation.from_raw`. `ProvisionalProductEvaluation`
   forbids unknown fields outright, so a field the LLM must not author cannot
   be silently dropped in development only to vanish in production.
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

   The prompt tells the model **which kind of cost it is judging**, via the
   payload's `shipping_quoted` flag (`RawSupplierProduct.shipping_quoted`):
   a supplier-quoted figure is VERIFIED and final, whereas a supplier that
   quotes no freight leaves a **floor** — the model is instructed to price
   conservatively and to REJECT a product whose case rests on that best case.
   Only the prompt changes on that path; the margin floor itself is unchanged,
   and no freight figure is ever invented for a supplier that will not quote
   one.
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
│   │   ├── cj_mcp_extractor.py      # CJdropshipping MCP: commercial gate, liveness gate, freight quote
│   │   ├── aliexpress_ds.py         # native DS Center ingestion + winner gate
│   │   └── google_shopping.py        # Step-3 gold-research Apify actor wrapper (§13; NOT a supplier extractor)
│   ├── evaluators/
│   │   ├── llm_filter.py    # instructor + ProvisionalProductEvaluation
│   │   └── gold_curator.py  # Step-3 LLM curation: select-by-url, facts code-assembled (§13)
│   ├── keywords/            # Step-4 gold-standard keyword engine (§13.3)
│   │   ├── gold_keyword_prompt.md  # Step-4 prompt resource ({PRODUCT_TABLE} slot)
│   │   └── generator.py     # Batched generation + code-side pool validation
│   ├── pipeline/
│   │   ├── cj_mcp_client.py # CJ MCP client + MCP Payload Liveness Gate
│   │   └── image_sourcing.py# deterministic CDN image download/validation
│   ├── exporter.py          # workspace writer (metadata.json + images/)
│   └── main.py              # CLI orchestrator, funnel counters, exit codes
├── scripts/
│   ├── generate_ali_session.py  # optional saved DS Center login (§6)
│   ├── verify_cj_gate.py        # CJ list-count threshold diagnostic (no LLM, no export)
│   ├── generate_gold_keywords.py     # Step-4 keyword bank runner (§13.3)
│   └── run_gold_standard_research.py  # Step-3 gold-product research runner (§13)
└── tests/                   # 450 hermetic tests, zero network (12 modules + conftest)
```

**Retired pipelines — do not rebuild.** The Meta Ad Library scraper
(`playwright_scraper.py`, `apify_scraper.py`), fuzzy-match sourcing
(`pipeline/supplier_sourcing.py`), the CJ REST client and extractor
(`pipeline/cj_client.py`, `extractors/cj_api_extractor.py`), the
Apify-hosted AliExpress scraper (`extractors/aliexpress_apify.py`, with its
pay-per-result actor, its US-market unauthenticated welcome-deal pricing and
every `APIFY_*` key) and the per-candidate DS Center gate it carried
(`ENABLE_DS_CENTER_GATE`), and the Etsy extractor (`extractors/etsy_api.py`,
with every `ETSY_API_KEY` reference) are deleted. **Etsy is retired as a
source outright: it does not support dropshipping, so it is not a supplier
this pipeline will ever use again** — it is gone from the extractor chain,
the `SUPPLIER_PRIORITY_ORDER` default, `KNOWN_SUPPLIERS`, the CLI's
`--extractor` choices and the config surface. AliExpress is
read natively from the Dropshipping Center (§6), CJ is MCP-only (§5), and
`supplier_retail_url` is the sole retail link in the export contract (§8).
Costing rationale for the replacement: §12.

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
  gate runs, shipping is quoted for the real destination (§6.3), the gallery
  comes from `productImageSet`, the description is
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

### 6.3 CJ FREIGHT QUOTE (MANDATORY)

Shipping is half a landed cost, and CJ used to be costed at **zero** because
its listing tools quote no freight — which understated
`estimated_cogs_aud`, overstated `projected_margin_aud`, and exported a
product that fails the margin floor once freight is real (a live export was
costed at AUD 5.19 when the quoted freight alone was AUD 19.42, making true
markup 1.4x against a 2.5x floor). Every emitted CJ product therefore carries
a quoted freight figure:

* **Primary:** `calculate_freight` with the listing's **cheapest variant**
  (`endCountryCode` = `TARGET_COUNTRY`, origin CN). CJ quotes per variant and
  weight differs between them, so the quote must name a specific one. The
  variant's price is used only to *choose* the cheapest — the cost basis
  stays the listing's own quoted price.
* **Fallback:** `calculate_freight_tip`, quoting by weight (`productWeight`
  low end) plus the listing's logistics attributes (`productProEnSet`), for a
  listing that exposes no usable variant id.
* **Method:** `CJ_FREIGHT_METHOD` pins a service by CJ's own name; blank takes
  the cheapest offered. An unoffered pin logs at WARNING and falls back to the
  cheapest rather than dropping the listing.
* **Fail-closed:** no usable quote from either path → the candidate is dropped
  (`freight quote unavailable`). Costing freight at zero is the understatement
  this gate exists to remove, so "we couldn't quote it" is a DROP, the same
  inverted tolerance §5 and §6.2 apply. A freight *tool* failure is contained
  to the one candidate (it never becomes `ExtractorBlockedException`, which
  would hand the whole keyword to the next engine).
* Both freight tools are resolved at connect time, and a client that cannot
  bind them **fails the connection** rather than proceeding to cost freight at
  zero, so the chain falls through to the next supplier instead.

**Both freight tools are hard requirements, not optional enrichment.** The
figures reach `shipping_cost_aud`, hence COGS, the basis note and both legs of
the margin floor; and `shipping_method` / `shipping_transit_days` carry the
quote onto `RawSupplierProduct` so the basis and the customer-facing
`shipping_notice_au` stay auditable (§8).

**Operator note — CJ's freight quotes are not stable between calls.** The same
variant and destination have returned both USD 9.37 and USD 12.53 within an
hour, presumably from CJ recomputing weight or zone. The pipeline takes the
cheapest offered method on each run, so a COGS figure carries a little
run-to-run variance. That is a property of the source, not of the pipeline;
re-run or pin `CJ_FREIGHT_METHOD` if a cost basis needs to be reproduced.

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
  "suggested_price_aud": 49.95,
  "estimated_cogs_aud": 24.61,
  "cogs_estimation_basis": "Supplier listed price AUD $5.19 plus AUD $19.42 tracked shipping to AU via CJPacket Eub, taken directly from the live CJdropshipping listing and its own freight quote.",
  "projected_margin_aud": 25.34,
  "marketing_ad_copy": "...",
  "features": ["...", "...", "..."],
  "target_tags": ["dropship", "..."],
  "shipping_notice_au": "Standard tracked international shipping to Australia via CJPacket Eub: 6-10 business days.",
  "supplier_name": "CJdropshipping",
  "supplier_retail_url": "https://cjdropshipping.com/product/<pid>.html",
  "image_source": "supplier_gallery"
}
```

`supplier_retail_url` is the **sole fulfillment link** (verified, live,
exact-match PDP — never a search gateway). `cogs_estimation_basis` is an
internal accounting note (zero URLs) that cites the listed price *and* the
quoted freight (§6.3), so a reviewer can see both halves of the landed cost.
`image_source` is always `"supplier_gallery"`.

`shipping_notice_au` is **derived in code** from the supplier's own quote —
the service name and transit window — never LLM-authored (§2.2). It states
only what the quote supports and makes no claim about what the customer pays;
an earlier LLM-authored version invented "Free standard shipping on this
item" against a real AUD 14.52 freight cost. A product with **no quote at all**
(AliExpress) gets `Ships to Australia from the supplier.` and nothing
more: naming a service or a transit window without a quote is the same class
of unsupported claim.

## 9. ORCHESTRATION (`src/main.py`)

```bash
python -m src.main --keyword "kitchen gadgets" --target-count 3 \
                   [--country AU] [--extractor {auto,cjdropshipping,aliexpress}]
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
| `LLM_BASE_URL` / `LLM_API_KEY` | — | llm_filter |
| `LLM_MODEL` | `deepseek-v4-flash:cloud` | llm_filter |
| `SUPPLIER_PRIORITY_ORDER` | `cjdropshipping,aliexpress` | extractor chain |
| `USD_TO_AUD` | `1.55` | all USD→AUD price math |
| `MIN_CJ_LISTED_COUNT` | `150` | CJ commercial gate (minimum `listedNum`; CJ reports no sales figure) |
| `CJ_FREIGHT_METHOD` | *(blank = cheapest)* | CJ freight quote: pin a shipping service by CJ's own name |
| `CJ_MAX_PRODUCTS` | `10` | products expanded per CJ keyword (MCP search cap) |
| `MIN_MARKUP_MULTIPLIER` | `2.5` | margin floor (markup leg), llm_filter + models |
| `MIN_MARGIN_AUD` | `20.0` | margin floor (gross-profit leg), llm_filter + models |
| `TARGET_COUNTRY` | `AU` | extraction/evaluation target; also the AliExpress ship-to market |
| `APIFY_TOKEN` | — | Apify account token, shared by TWO actors: (1) Step-2 trend-research FALLBACK `data_xplorer/google-trends-fast-scraper` via the Apify MCP (`.mcp.json`) — used only if the HasData Google Trends MCP fails or returns an info-poor schema, $2.00/1,000 results; (2) Step-3 gold-research CORE actor (§13), $3.50/1,000 results |
| `APIFY_GS_ACTOR` | `damilo/google-shopping-apify` | Step-3 gold-research actor id (§13) |
| `APIFY_GS_MAX_RESULTS_PER_KEYWORD` | `10` | Step-3 results requested per keyword (actor `num`; closed set 10/20/30/40/50/100) |
| `APIFY_GS_MAX_CHARGE_USD` | `7.5` | Step-3 hard USD spend ceiling per actor run, enforced by Apify itself; sized above the observed full-bank envelope (~$5.60) and within the free-tier remainder |
| `GOLD_PRODUCTS_PATH` | `outputs/step-3-gold-standard-products.json` | Step-3 gold-product deliverable (untracked `outputs/` tree) |
| `KEYWORD_BANK_PATH` | `outputs/step-4-gold-keywords.json` | Step-4 gold-keyword bank deliverable — Step 5's intake (untracked `outputs/` tree) |
| `EXPORT_DIR` | `…/my-store-build/inspiration/dropship-candidates` | exporter |
| `USER_AGENT` | desktop Chrome UA | CDN downloads, Playwright PDP harvest |

Every key in `.env.example` is present in `.env` in the same order with the
same description; a key that is commented out **or present but blank**
resolves to its default in `src/config.py` (blank never means `0`/`False`).

`Settings` never logs or prints its values (no-leak repr); credentials live
only in `.env` / the real environment.

## 11. TESTS & ENVIRONMENT

- Hermetic suite: `source .venv/bin/activate && pytest tests/ -v` — 450
  tests, zero network (httpx.MockTransport + fake MCP sessions + scripted
  Playwright/MTOP fakes + faked Apify SDK / scripted LLM transports).
- `tests/test_keyword_generator.py` covers the Step-4 gold-keyword engine:
  the template prompt (the product table is a `{PRODUCT_TABLE}` slot, never
  a hardcoded list), the table rendering (cells verbatim, `null` → `—`), the
  gold-deliverable read with its remediation errors, the demand-ranked
  selection with its boundary-name exclusion and one-per-`source_keyword`
  type collapse, the adaptive per-product target and floor, batching with the
  per-batch note and the carried do-not-repeat list, the merged-pool
  validation contract (broad/modifier pairing, banned tokens, band,
  coverage, floor), the neediest-first duplicate collapse, and salvaging; it
  uses a tmp_path gold fixture and never reads the real `outputs/` tree —
  see §13.3.
  `tests/test_google_shopping.py`
  covers the Step-3 gold-research scraper (actor input shape incl. the
  closed-set `num`, spend ceiling riding the run options, run-status
  taxonomy, row parsing) and the gold curator (facts assembled from rows,
  invented-URL join drop, pillar/boundary enforcement, dedupe, fenced and
  truncated-response salvage, the reasoning-model empty-content failure,
  batching, transport/HTTP/config errors) — see §13.
- `tests/test_aliexpress_ds.py` covers the payload decoding (plain and
  JSONP), order/rating parsing, the currency guard and AUD conversion, the
  §6.1 gate and each documented drop log, the MTOP priming-then-signed
  handshake, ordering by order volume, dedupe across keywords, the
  session-expiry and blocked-exchange paths, the optional state file, and the
  PDP gallery/description harvest.
- `tests/test_cj_mcp_client.py` covers tool discovery and the alias binding
  (§5.1) including the exact-only rule for the nesting freight tools, the
  liveness gate, the listing-count and freight-quote parsing, and both freight
  call paths; `tests/test_cj_mcp_extractor.py` covers the §6.2 commercial gate
  and its ranking, the §6.3 freight quote with its pin and fallback, the
  liveness drops, and the chain exception mapping.
- `tests/test_models.py` covers the schema validators, the zero-URL basis
  rule, `from_raw` cost and margin math, and the §8 shipping-notice derivation
  including the unquoted case; `tests/test_config.py` covers every key in §10.
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
- **CJ freight evidence (2026-09-23, read-only):** the first live export
  (`coffee accessories`, product-06) was costed with shipping at zero; its
  quoted freight to AU is **AUD 19.42** via CJPacket Eub, making the true
  landed cost AUD 24.61 against a AUD 34.95 retail — 1.42x markup, below both
  margin floors, so the export was a false positive (§6.3). With quoting
  wired in, that same listing is now costed correctly and the gate rejects it.
  `calculate_freight` returned 15 AU methods for it (USD 9.37 to 33.54), and
  the same variant quoted both 9.37 and 12.53 within an hour — hence the
  variance note in §6.3.
- Python 3.13 venv at `.venv/`; install with `pip install -e ".[dev]"`;
  Playwright Chromium: `python -m playwright install chromium` (used by the
  AliExpress DS Center calls and the PDP gallery harvest).
- The `mcp` SDK is a declared dependency (StreamableHTTP client only).

## 12. OPERATIONAL NOTES

- **PR review workflow:** the operator personally requests the Copilot PR
  review when he wants it — a Copilot review is **never auto-requested on
  PR creation or push** (requesting it is his action, only ever on his
  explicit ask). Once a review reports no High-severity findings, he merges
  to `main` himself and decides the fix-now vs follow-up split. The agent
  raises PRs only when he asks (he normally raises them himself).
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
## 13. THE UPDATED MULTI-STEP WINNING-PRODUCT PIPELINE (branch `feature/jev-keyword-gate-multistep-pipeline`)

> Appended as §13 (not renumbered into §5's position) so every existing
> §5–§12 cross-reference across the repo stays valid. This section covers
> the gold-kernel pipeline being built **on this branch**; the supplier core
> (§5–§9) is untouched by it and remains the general intake. The full
> engineering plan is the (untracked) `plans/updated_multistep_pipeline_implementation_plan.md`.

### 13.1 Overview — gold-kernel intake vs general intake

The updated pipeline produces a **gold kernel**: a small set of
proven-demand AU retail products (Google Shopping evidence) that drives
keyword generation, supplier ingestion and Jev ranking, exported to
`my-store-build/inspiration/optimal-dropship-candidates/{aliexpress,cjdropshipping}/`
(Step 5, planned). The existing `dropship-candidates/` intake (§8) stays as
the general flow. **Jev never was a keyword gate** — its role is
post-ingestion product ranking (Step 6).

| Step | What | Status on this branch |
|---|---|---|
| 1 | Google Trends (HasData MCP) research | done (research, `/tmp` scratch — no repo code by design). The Apify fallback `data_xplorer/google-trends-fast-scraper` ($2.00/1,000) was not needed — HasData stayed healthy |
| 2 | Trends → AU search keywords, tagged `curated_home`/`self_care_rituals`/`other`, each with demand evidence | done — deliverable `outputs/step-2-search-keywords.{json,md}` (untracked) |
| 3 | Apify Google Shopping AU scrape of the Step-2 keywords + LLM curation → gold-standard product list | done — §13.2 |
| 4 | Reasoning LLM → 50–70 supplier keywords from the gold list | done — §13.3 |
| 5 | Dual-supplier ingestion (CJ + AliExpress) into the keyword bank → `optimal-dropship-candidates/` | planned |
| 6 | Jev (TypeSafe System One via OpenRouter) ranks supplier candidates against the gold products | planned |
| 7–8 | Human-only: DSers/Zendrop manual supplier search; store curation | no code (deliberately) |

All production deliverables live under `outputs/` — **untracked**
(gitignored; scoping data, not repo artefacts). `plans/` now holds only
the engineering/spec documents (the updated-pipeline plan and its
predecessors). Future Steps 5–6 write their deliverables (ingestion results,
Jev rankings) to `outputs/step-N-*` names in the same gitignored tree, as
Step 4's keyword bank already does
(`outputs/step-4-gold-keywords.{json,md}`).

### 13.2 Step 3 — gold-standard product research (implemented)

```bash
source .venv/bin/activate && python scripts/run_gold_standard_research.py \
    [--limit 2] [--num 10] [--dump-raw /tmp/step3_raw_rows.json] \
    [--from-raw /tmp/step3_raw_rows.json] \
    [--output outputs/step-3-gold-standard-products.json]
```

- **Scrape** (`src/extractors/google_shopping.py`): ONE batched run of the
  `damilo/google-shopping-apify` actor (pay-per-result, ~$3.50/1,000
  results) carries every keyword (`queries` input), `country="au"`,
  `max_pages=1`. Spend is governed three ways: the actor's closed-set `num`
  is validated before any money moves; a hard `max_total_charge_usd`
  ceiling (`APIFY_GS_MAX_CHARGE_USD`) rides the run options and is enforced
  by Apify itself; `--limit N` pilots on the first N keywords. It is
  **extractor-shaped but deliberately NOT a `BaseSupplierExtractor`**: these
  are marketplace retail listings with no supplier PDP, freight quote or
  gallery, so they can never become `RawSupplierProduct` without fabricating
  sourcing fields — the module is never registered in the extractor chain.
- **Curate** (`src/evaluators/gold_curator.py`): the reasoning LLM only
  **selects by verbatim `url`** and annotates `pillar` /
  `compliance_note` / `unit_economics_note` (one of
  `curated_home`/`self_care_rituals`/`other`, strict no-cosmetics /
  no-consumable / no-IP boundaries). Every product fact (name, price,
  merchant, demand evidence, keyword) is **code-assembled from the scraped
  rows** — a hallucinated or edited url can never become a product (dropped
  at the code-side join). Demand evidence comes from the row's
  rating/reviewCount; **rows with neither KPI are filtered out before the
  LLM** (operator decision 2026-09-28), so
  `not_available_from_source` never reaches the deliverable — a gold
  product must carry on-page demand evidence, and the Step-2 Trends
  evidence stays in the keyword file where it belongs.
- **Deliverable**: `outputs/step-3-gold-standard-products.json` (+ `.md`
  digest) — the reference set Steps 4 and 6 measure against. The runner
  prints the planned spend envelope before the first call, exits non-zero
  when nothing usable comes back, and `--from-raw` replays curation over a
  prior `--dump-raw` dump at **zero Apify spend** (the debugging path).
- **Pacing:** the curation batches at `ROWS_PER_CALL=10` rows per LLM call
  — the configured model is a *reasoning* model whose chain-of-thought
  shares the completion budget with the answer, and a larger batch makes it
  spend the whole `max_tokens` budget thinking and return empty `content`
  with `finish_reason="length"` (observed live at 60 rows / 8,000 tokens;
  the empty-content error names the finish reason explicitly). A full
  40-keyword run is therefore ~100–160 reasoning LLM calls after the
  demand-evidence filter shrinks the pool — roughly an hour or two of
  curation after the scrape. That is the pacing, not a hang; the batch
  size buys reliability, not speed, and one retry per batch absorbs the
  model's flaky empty-content mode so a single flaky batch cannot void the
  run.

### 13.3 Step 4 — gold-standard keyword bank (implemented)

```bash
source .venv/bin/activate && python scripts/generate_gold_keywords.py \
    [--gold-products outputs/step-3-gold-standard-products.json] \
    [--batch-size 4] [--max-products 12] \
    [--output outputs/step-4-gold-keywords.json] [--markdown …]
```

Turns the Step-3 gold list into the **50–70 supplier search keywords** Step 5
ingests, typed as `CandidateKeyword(keyword, product, pillar, role, tightens,
rationale)` with `role ∈ {broad, modifier}` and
`pillar ∈ {curated_home, self_care_rituals, other}` (`"other"` was added to
the Literal for this step).

- **Prompt** (`src/keywords/gold_keyword_prompt.md`): carries **no
  products**. Its product table is a literal `{PRODUCT_TABLE}` slot, filled
  at runtime from the live Step-3 deliverable via `str.replace` (never
  `.format()` — the requirements text contains literal braces,
  `{"keywords": […]}`). `load_prompt_parts` raises if the slot is absent, so
  a prompt that has silently lost its slot fails before any spend.
- **The bank is validated in code, not trusted from the prompt.**
  `validate_pool` re-checks every rule the prompt states: required fields
  (`FIELDS`), pillar and role membership, dangling/self `tightens`,
  broad-carries-`tightens`, the AICIS banned-token boundary, duplicates, the
  50–70 band, unknown/unmentioned `product`, and the per-product floor. A
  failing pool raises `KeywordGenerationError` and **writes nothing** —
  same fail-closed posture as §2.
- **Selection is bounded, type-diverse and deterministic.** The live Step-3
  deliverable has **263** products, while the plan's per-product target math
  assumed 8–20. `select_gold_products` therefore caps the table at
  `DEFAULT_MAX_PRODUCTS = 30` (`--max-products`), ranked by demand
  (`-reviews, -rating, name`), collapsed to the strongest row per duplicate
  name, **collapsed to the strongest row per Step-2 `source_keyword`**, then
  **round-robined across `PILLARS`** so a capped table still covers every
  pillar. The 263-product deliverable is untouched — it stays the full
  research record; the cap only bounds what the keyword prompt sees.

  **The type collapse is load-bearing, not tidiness.** The research is
  keyword-driven, so the strongest 30 rows by raw demand were about 15
  distinct types — 5 garlic presses, 5 coffee/French presses, 4 body dry
  brushes, 4 ice rollers. Every table product must be named by *its own*
  keywords, the pool treats a duplicate keyword string as fatal (Step 5 runs
  each once) and brand names are banned, so five garlic presses cannot each
  own two honest keywords: the model writes the shared head term for all of
  them and dedupe starves the rest — exactly how the first cap-30 run failed.
  One row per source keyword spends the table on distinct products instead
  (the live rerun selected 30 distinct types). A row with no source keyword
  falls back to its own name as the group key.

  **30 is not arbitrary either:** the validator requires every table product
  to be named inside a 50–70 pool at ≥ `default_per_product_min`, so 35
  products is the band's hard ceiling (2 each = 70 exactly) and 30 keeps
  headroom (60 of 70). Widening from the original 12 spreads the band across
  more of the researched demand at no extra Step-5 ingestion cost.
- **Boundary names are excluded from the table.** `carries_banned_token`
  drops a gold product whose own *name* carries a banned AICIS token (7 of
  263 measured: jade 4, quartz 2, salt 1 — gua sha tools and a salt product),
  because its honest keyword could never clear the pool validator. They
  remain in the Step-3 deliverable.
- **Adaptive target/floor, bounded by the band at both ends.**
  `per_product_target(n) = min(9, ceil(60/n), 70 // n)`, raised to
  `ceil(50/n)` only when that lift still fits under 70, and never below
  `default_per_product_min(n) = max(2, 50 // n - 1)`. The `70 // n` term is
  what makes a 30-product table work at all: the plan's `[3, 9]` clamp asks
  90 keywords there, past the ceiling. At 30 → **2 per product (60 total)**;
  at 35 → 2 (70, the ceiling). One size has no uniform target: 24 products
  (2 each = 48, 3 each = 72) — the target takes the floor there, and only
  `--max-products` can reach it. 36+ is infeasible; the cap of 30 sits well
  inside that.
- **Uniqueness is enforced at both ends — in the prompt and in code.**
  `validate_pool` treats a duplicate keyword string as fatal (Step 5 runs
  each once), and each batch call is independent, so the model cannot infer
  what a previous batch wrote. `generate()` therefore accumulates every
  keyword emitted so far and `_assemble_user_message` appends them to later
  calls as an explicit do-not-repeat list (requirement 2 says the list is
  there). Without it the rule is unsatisfiable across batches, which is how
  the second cap-30 run failed: three shared head terms were written by two
  products each, and dedupe left three products with 1 of their 2 keywords.
- **Pacing:** batches of `DEFAULT_BATCH_SIZE = 4` products per LLM call,
  `MAX_TOKENS = 16000`. The configured reasoning model shares that budget
  between its chain-of-thought and the visible content, so an oversize batch
  returns empty content with `finish_reason="length"`. `parse_llm_keywords`
  salvages a truncated fenced-JSON tail and the empty-content error names the
  finish reason. `dedupe_keywords` then collapses any repeated keyword still
  written (logging a WARNING) before `validate_pool`, awarding a contested
  string to the product with the fewest keywords of its own rather than to
  the batch that answered first — first-wins always starves the later
  batches. `validate_pool`'s duplicate check stays as the guard for
  directly-called or hand-merged pools.
- **Deliverable**: `outputs/step-4-gold-keywords.json` (+ `.md` digest,
  modifiers nested under their broad term) — Step 5's keyword bank. The
  runner prints the gold-product count, per-product target and batch size
  before the first call, and exits non-zero on `KeywordGenerationError`
  having written nothing.

### 13.4 Step-3 live evidence (2026-09-28)

- Actor input validation is real: `num` accepts ONLY
  10/20/30/40/50/100 — anything else fails the run before spending.
- The actor returns a **full first SERP page per keyword (~40 rows) even at
  `num=10`** — `num` is not a per-keyword cap on this actor. The honest
  spend envelope is keywords × ~40 rows (~$0.14/keyword at
  $0.0035/result); the pilot's 2 keywords returned 80 rows (~$0.28).
- **The `link` field carries Google Shopping SEARCH urls**
  (`google.com/search?ibp=oshop&…`), not merchant PDP urls — the actor
  README's merchant-style example link is misleading. The url is therefore
  a stable row identity for the anti-hallucination join and resolves to the
  listing on Google, not the merchant's page.
- End-to-end (rows cached via `--dump-raw`, then curated via `--from-raw`
  at zero credit): 80 rows → 24 gold products, every product url verified
  verbatim against the scraped rows. The first curation attempt failed
  because the 60-row batch exhausted the reasoning budget (empty content,
  `finish_reason="length"`); the fix — 10 rows/call + `MAX_TOKENS=16000` —
  is covered by hermetic tests.
- The full 40-keyword production run (~$5.60 envelope, under the free-tier
  remainder) is deliberately **deferred until the operator's go**.

**Production run (same day, operator's go):** all 40 Step-2 keywords, run
`rP09Pk3x0nkqlSuLB`, actor usage **$4.06** (1,567 rows ≈ $0.0026/result —
under the README rate; total Apify spend $6.76 of the $10 free tier). The
demand-evidence filter kept 718 rows (46%); 72 curation batches produced
**263 gold products** (157 `curated_home`, 106 `self_care_rituals`, zero
`other`), **every one of the 40 keywords contributed**, all 263 carry
rating+review-count evidence and a price, and the anti-hallucination join
verified all 263 urls verbatim against the raw rows with zero join drops.
The one-retry guard fired exactly once (one flaky empty-content batch,
retried, run continued) — it earned its keep on the first production run.
Deliverables: `outputs/step-3-gold-standard-products.{json,md}` plus the
raw rows at `outputs/step-3-gold-raw-rows.json` (any future re-curation
replays
from that dump at zero Apify spend).

### 13.5 Step-4 live evidence (2026-09-28)

Four green/failed runs, each fix below coming out of a failure:

| Run | Table | Result |
|---|---|---|
| — | the strongest 12 by demand (first attempt) | **failed** — the validator rejected a duplicate keyword written for two garlic presses; `dedupe_keywords` (§13.3) is the fix |
| 1 | the strongest 12 by demand | green: **58 keywords**, `per_product_target = 8`, 3 calls |
| 2 | the strongest 30 by demand | **failed** — the table was ~15 distinct types (5 garlic presses, 5 coffee presses), and five garlic presses cannot each own 2 unique honest keywords |
| 3 | 30, one per Step-2 `source_keyword` | **failed** — 3 cross-batch repeats (`stainless steel garlic press`, `dry body brush`, `pumice stone foot file`), dedupe left 3 products with 1 of their 2 keywords |
| 4 | 30, one per source keyword, with the carried taken list | **green** |

- **Run 2's real cause was table composition, not the model.** One product
  type recurs across several Step-2 `source_keyword` values and several
  merchants, and a duplicate keyword string is fatal while brand names are
  banned, so the collapse to one product per source keyword (§13.3) was the
  fix — the table spent its 30 slots on distinct products.
- **Run 3's real cause was batching.** Each call is independent, so a batch
  cannot know what an earlier one claimed: asking for pool-wide uniqueness
  without saying what was taken is unsatisfiable, not merely unstated. Hence
  the accumulated do-not-repeat list in `_assemble_user_message` (§13.3,
  plan §6.5 delta 6).
- **Green run (run 4):** 30 products, `per_product_target = 2`, batch size 4
  → **8 LLM calls**, **60 keywords in the final bank** (band 50–70 ✓, floor
  2 ✓), **zero duplicates written** so `dedupe_keywords` dropped nothing.
  Roles broad 30 / modifier 30; pillars curated_home 30 /
  self_care_rituals 30; 60 unique keywords, 2–7 words, no uppercase, no
  punctuation; every product at exactly 2. Model `deepseek-v4.1-flash:cloud`,
  ~15 minutes of wall clock.
- **The near-type families are differentiated as intended:** `stainless
  garlic press` / `rocking garlic press` / `garlic rocker`; `pumice stone
  foot file` / `pumice foot tool` / `pumice stone`; `stainless steel gua sha`
  / `scalp massage tool` / `gua sha facial tool` / `gua sha set`; `dry body
  brush` / `body brush with handle`.
- Deliverables written to `outputs/step-4-gold-keywords.{json,md}` on every
  green run — confirmed gitignored (`.gitignore:31` = `outputs/`). A failed
  run writes nothing.
