# CLAUDE.md - Dropship Scout Agent (Supplier-First Architecture)

> Aligned with the implemented codebase as of 2026-09-29 (Step 6 Jev ranking
> implemented and live-run; see §13.7). This file is the
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
   (`MIN_MARKUP_MULTIPLIER` ≥ 2.5 OR margin > `MIN_MARGIN_AUD` 10) is
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
   `MIN_MARGIN_AUD` (AUD 10, widened from AUD 20 on 2026-09-29 to match the
   operator's Step-3 $10 target — the 5-point gate prompt interpolates the
   live config values so it cannot drift) gross profit per unit; a product
   whose realistic AU price cannot clear that floor is REJECTed on this gate
   rather than priced up to fit (enforced twice: `_reconcile` downgrade in
   `llm_filter.py` and `_enforce_accept_gates` in `models.py`, both reading
   the same config keys the prompt interpolates).

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
│   ├── ranking/             # Step-6 Jev product ranking (§13.7)
│   │   ├── jev_client.py    # System One transport + all question/threshold constants
│   │   └── jev_product_ranker.py  # Batching, composite score, tiers, report writer
│   ├── pipeline/
│   │   ├── cj_mcp_client.py # CJ MCP client + MCP Payload Liveness Gate
│   │   ├── image_sourcing.py# deterministic CDN image download/validation
│   │   └── keyword_bank.py  # Step-5 bank loader + dual-supplier ingestion (§13.6)
│   ├── exporter.py          # workspace writer (metadata.json + images/)
│   └── main.py              # CLI orchestrator, funnel counters, exit codes
├── scripts/
│   ├── generate_ali_session.py  # optional saved DS Center login (§6)
│   ├── verify_cj_gate.py        # CJ list-count threshold diagnostic (no LLM, no export)
│   ├── generate_gold_keywords.py     # Step-4 keyword bank runner (§13.3)
│   ├── ingest_keyword_bank.py        # Step-5 dual-supplier ingestion runner (§13.6)
│   ├── rank_optimal_candidates.py    # Step-6 Jev ranking runner (report only, no deletion)
│   └── run_gold_standard_research.py  # Step-3 gold-product research runner (§13)
└── tests/                   # 535 hermetic tests, zero network (14 modules + conftest)
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
| `MIN_MARGIN_AUD` | `10.0` | margin floor (gross-profit leg), llm_filter + models — widened from AUD 20 on 2026-09-29 to match the operator's Step-3 $10 target |
| `TARGET_COUNTRY` | `AU` | extraction/evaluation target; also the AliExpress ship-to market |
| `APIFY_TOKEN` | — | Apify account token, shared by TWO actors: (1) Step-2 trend-research FALLBACK `data_xplorer/google-trends-fast-scraper` via the Apify MCP (`.mcp.json`) — used only if the HasData Google Trends MCP fails or returns an info-poor schema, $2.00/1,000 results; (2) Step-3 gold-research CORE actor (§13), $3.50/1,000 results |
| `APIFY_GS_ACTOR` | `damilo/google-shopping-apify` | Step-3 gold-research actor id (§13) |
| `APIFY_GS_MAX_RESULTS_PER_KEYWORD` | `10` | Step-3 results requested per keyword (actor `num`; closed set 10/20/30/40/50/100) |
| `APIFY_GS_MAX_CHARGE_USD` | `7.5` | Step-3 hard USD spend ceiling per actor run, enforced by Apify itself; sized above the observed full-bank envelope (~$5.60) and within the free-tier remainder |
| `GOLD_PRODUCTS_PATH` | `outputs/step-3-gold-standard-products.json` | Step-3 gold-product deliverable (untracked `outputs/` tree) |
| `KEYWORD_BANK_PATH` | `outputs/step-4-gold-keywords.json` | Step-4 gold-keyword bank deliverable — Step 5's intake (untracked `outputs/` tree) |
| `BANK_TARGET_PER_KEYWORD` | `2` | Step-5 packages exported per **(keyword, engine)** leg — a PER-LEG target, so the bank's keyword count multiplies it (§13.6) |
| `OPTIMAL_EXPORT_DIR` | `…/my-store-build/inspiration/optimal-dropship-candidates` | Step-5 gold-kernel root: each engine writes into its own subfolder, numbered independently (§13.6) |
| `OPENROUTER_API_KEY` | — | Step-6 Jev ranking: Bearer token for the OpenRouter System One endpoint (never logged) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Step-6 client base; it appends `/systemone` |
| `JEV_MODEL` | `typesafe/jev-1.13` | Step-6 System One model id (pinned so a ranking is reproducible) |
| `JEV_BATCH_SIZE` | `4` | Step-6 packages per System One call (three questions each; ~13-question native batch) |
| `JEV_SHORTLIST_MIN_SCORE` | `3.5` | Step-6 tier floor: `rank_score` ≥ this → shortlist (1–5 scale), widened from 4.0 on 2026-09-29 |
| `JEV_REVIEW_MIN_SCORE` | `2.5` | Step-6 tier floor: `rank_score` ≥ this → review, else disregard |
| `EXPORT_DIR` | `…/my-store-build/inspiration/dropship-candidates` | exporter (general intake §9) |
| `USER_AGENT` | desktop Chrome UA | CDN downloads, Playwright PDP harvest |

Every key in `.env.example` is present in `.env` in the same order with the
same description; a key that is commented out **or present but blank**
resolves to its default in `src/config.py` (blank never means `0`/`False`).

`Settings` never logs or prints its values (no-leak repr); credentials live
only in `.env` / the real environment.

## 11. TESTS & ENVIRONMENT

- Hermetic suite: `source .venv/bin/activate && pytest tests/ -v` — 535
  tests, zero network (httpx.MockTransport + fake MCP sessions + scripted
  Playwright/MTOP fakes + faked Apify SDK / scripted LLM transports).
- `tests/test_keyword_bank.py` covers the Step-5 bank and its dual-supplier
  ingestion: the fail-closed loader (missing/malformed/empty/non-list
  payloads, non-object and non-string rows, the banned-token defence in
  depth), and `ingest_keyword_bank` over fake extractors/evaluator/exporters
  — both engines run for every keyword, one shared evaluator, the per-leg
  failure skip with the other engine continuing, the all-engines-failed
  `BankIngestionFailedError`, the empty-catalogue accounting, the per-leg
  early stop (it uses the real `run_pipeline`, so the funnel counters and the
  early-stop rule are inherited, not re-implemented), and the
  cumulative-exporter-counter delta fold — including the empty-funnel leg,
  which must fold as 0 rather than as a negative. It writes to `tmp_path` and
  never touches the real supplier trees — see §13.6.
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
- `tests/test_jev_ranker.py` covers the Step-6 ranker: the System One
  transport envelope (URL, Bearer auth, attribution headers, body shape), the
  transport/HTTP/non-JSON/missing-`answers` failures, the `JevConfigError`
  path, both question builders, the **0-based → 1–5 score shift**
  (`answer_score`), the judgement constants and thresholds, both-supplier
  collection with engine-qualified slugs, the unreadable/non-object metadata
  skips, the empty-tree and missing-gold remediation, batching with one shared
  state, the tier boundaries, the descending sort, the composite weights, a
  missing answer key landing as `disregard` with a note, the discard-pillar
  note, the retry-then-`disregard` batch failure, later batches continuing, and
  the JSON/Markdown report writers — see §13.7.
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
- **CJ MCP transport drops (live 2026-09-30):** the StreamableHTTP response
  stream can die mid-call, surfacing as a `BaseException`-derived
  `CancelledError` that escapes `except Exception` and, uncontained, killed a
  whole bank run (first full-bank attempt, ~34 keywords in). `_call_tool`
  converts it to `CjMcpToolError` (never retried — a dead stream cannot
  recover in-session) and the connect handshake to `CjMcpConnectionError`, so
  the blast radius is the current candidate/leg and the next connect opens a
  fresh session. The subsequent full-bank run hit three such drops, all
  contained (0 unhandled errors across ~11h).
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
- **Jev's `score` is 0-based, not 1-based (live probe 2026-09-29).** The plan
  (§8.0) recorded the score position as 1-indexed; the live probe returned
  `score = 3.24` against a 5-entry criteria list whose `legend` keys were
  `"0".."4"`. `jev_client.answer_score` therefore shifts the raw position by
  `JEV_SCORE_INDEX_SHIFT = 1.0` onto the 1–5 scale the thresholds are written
  against, so no config default had to move (§13.7).
- **AliExpress pacing:** each search hit costs one item-record round trip
  (~2-5 s), so `ALI_DS_MAX_PRODUCTS=20` means a keyword takes roughly 1-2
  minutes before the PDP harvest, which adds one page load per survivor.
  That is the pacing, not a hang. A failed search exchange raises
  `ExtractorBlockedException` (the chain moves on) rather than reporting an
  empty funnel, and a session/auth refusal raises
  `DsCenterSessionExpiredError`, which halts with re-login instructions.
- **AliExpress PDP shell pages and the harvest retry (2026-09-30).** Ali's
  anti-bot can serve a PDP as an HTTP-200 shell (≈19 characters of body, no
  carousel, no `runParams`) while the DS Center's own MTOP APIs keep
  answering — the whole 320-keyword bank run harvested 1,592 gate survivors
  under shells, silently, until the fix. The harvest now re-loads every PDP
  once from a fresh page after a short settle delay, keeps the better
  gallery, and both the per-candidate drops and a per-keyword tally are
  WARNING/INFO logged (`PDP gallery harvest for '<kw>': N candidate(s), X
  upgraded, Y empty after retry, Z page load(s)`). Refreshing the saved
  session did NOT change the shells (tested live: anonymous == stale file);
  the block is per-URL selective and volume/state-driven — a same-day
  isolated probe showed one family (coffee maker) harvesting 13-image
  galleries while others were shelled. A decay/re-probe (or a different
  IP) is the recovery path; the visible logging is the early-warning
  system.
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
(Step 5, §13.6). The existing `dropship-candidates/` intake (§8) stays as
the general flow. **Jev never was a keyword gate** — its role is
post-ingestion product ranking (Step 6).

| Step | What | Status on this branch |
|---|---|---|
| 1 | Google Trends (HasData MCP) research | done (research, `/tmp` scratch — no repo code by design). The Apify fallback `data_xplorer/google-trends-fast-scraper` ($2.00/1,000) was not needed — HasData stayed healthy |
| 2 | Trends → AU search keywords, tagged `curated_home`/`self_care_rituals`/`other`, each with demand evidence | done — deliverable `outputs/step-2-search-keywords.{json,md}` (untracked) |
| 3 | Apify Google Shopping AU scrape of the Step-2 keywords + LLM curation → gold-standard product list | done — §13.2 |
| 4 | Reasoning LLM → a pool-chunked supplier keyword bank (~300 keywords) from the gold list | done — §13.3 |
| 5 | Dual-supplier ingestion (CJ + AliExpress) into the keyword bank → `optimal-dropship-candidates/` | done — §13.6 |
| 6 | Jev (TypeSafe System One via OpenRouter) ranks supplier candidates against the gold products | done — §13.7 |
| 7–8 | Human-only: DSers/Zendrop manual supplier search; store curation | no code (deliberately) |

All research deliverables live under `outputs/` — **untracked**
(gitignored; scoping data, not repo artefacts) — while the Step-5 gold-kernel
packages are written into the Shopify workspace tree
(`optimal-dropship-candidates/`, §13.6), like the general intake (§8). `plans/`
now holds only the engineering/spec documents (the updated-pipeline plan and
its predecessors). Step 6's Jev rankings live under the same gitignored tree as
`outputs/step-6-ranked-candidates.{json,md}` (§13.7), as Step 4's keyword bank
already does there (`outputs/step-4-gold-keywords.{json,md}`).

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
    [--batch-size 4] [--max-products 150] [--chunk-size 30] \
    [--output outputs/step-4-gold-keywords.json] [--markdown …]
```

Turns the Step-3 gold list into the **supplier search keyword bank** Step 5
ingests, typed as `CandidateKeyword(keyword, product, pillar, role, tightens,
rationale)` with `role ∈ {broad, modifier}` and
`pillar ∈ {curated_home, self_care_rituals, other}` (`"other"` was added to
the Literal for this step). The bank is generated **in pool chunks** (the
operator's widening, 2026-09-29): the table is capped at
`DEFAULT_MAX_PRODUCTS = 150` eligible products and sliced into
`DEFAULT_CHUNK_SIZE = 30`-product pools, each generating ONE pool validated
under the same rules a single-pool run always had, and the merged bank must
sit inside its **bank band** — `[summed target − 50, summed target + 20]`,
i.e. **250–320 keywords at the default sizing** (5 chunks × 60). The old
uncapped-single-pool design (one 30-product table, one 50–70 pool, 60
keywords) was the funnel's biggest bottleneck; the fixes doc's
chunk-all-263 / ~500+-keyword version was superseded by this operator
sizing, which keeps the widened Step-5 ingestion near ~10–11h instead of 18h+.

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
  pool band (50–70 per chunk; the bank band on the merged set), and
  unknown/unmentioned `product`. A failing pool raises
  `KeywordGenerationError` and **writes nothing** — same fail-closed posture
  as §2. The merged pass gives cross-chunk duplicate strings to the same
  fatal duplicate rule: **a repeat across chunks is FAIL-CLOSED, never
  silently deduped post-validation** — removing a keyword after a chunk's
  own validation could void that chunk's per-product floor guarantee.
- **Selection is bounded, type-diverse and deterministic.** The live Step-3
  deliverable has **263** products, while the plan's per-product target math
  assumed 8–20. `select_gold_products` therefore caps the table at
  `DEFAULT_MAX_PRODUCTS = 150` (`--max-products`), ranked by demand
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

  **Why a cap at all (superseded design note kept for history):** the
  original single-pool design capped at 30 because one 50–70 pool can
  hard-cover at most 35 products (2 each = 70 exactly). The widening lifts
  the table to 150, but any *single pool* is still bounded by that ceiling —
  which is exactly what chunking preserves: each chunk faces the same
  satisfiable target math.
- **Boundary names are excluded from the table.** `carries_banned_token`
  drops a gold product whose own *name* carries a banned AICIS token (7 of
  263 measured: jade 4, quartz 2, salt 1 — gua sha tools and a salt product),
  because its honest keyword could never clear the pool validator. They
  remain in the Step-3 deliverable.
  drops a gold product whose own *name* carries a banned AICIS token (7 of
  263 measured: jade 4, quartz 2, salt 1 — gua sha tools and a salt product),
  because its honest keyword could never clear the pool validator. They
  remain in the Step-3 deliverable.
- **Adaptive target/floor, bounded by the band at both ends.**
  `per_product_target(n) = min(9, ceil(60/n), 70 // n)`, raised to
  `ceil(50/n)` only when that lift still fits under 70, and never below
  `default_per_product_min(n) = max(2, 50 // n - 1)`. The `70 // n` term is
  what makes a 30-product chunk work at all: the plan's `[3, 9]` clamp asks
  90 keywords there, past the ceiling. At 30 → **2 per product (60 total)**;
  at 35 → 2 (70, the ceiling). 36+ is infeasible for a single pool — which
  is why the widened table exists only as chunks. 24 products is the one
  unsatisfiable chunk size (2 each = 48, 3 each = 72);
  `chunk_sizes()` never produces one — a tailing 24-chunk borrows one
  product from the previous chunk (29+25), and a chunk size whose target
  cannot reach 50 (e.g. `--chunk-size 45`) fails the plan before any
  LLM call spends.
- **Uniqueness is enforced at three levels — in the prompt, within a
  pool, and across pools.** `validate_pool` treats a duplicate keyword
  string as fatal (Step 5 runs each once), and each batch call is
  independent, so the model cannot infer what a previous batch wrote.
  `_generate_pool` accumulates every keyword emitted so far and
  `_assemble_user_message` appends them to later calls as an explicit
  do-not-repeat list (requirement 2 says the list is there) — seeded from
  `generate_bank` with every earlier chunk's claims, so the list spans the
  whole bank. In-pool repeats are still the neediest-first `dedupe_keywords`
  salvage; **cross-chunk repeats are fatal**, caught before the merge
  (requirement 2's unsatisfiable-without-the-list history — the second
  cap-30 run's three shared head terms — is why the list exists).
- **Pacing:** batches of `DEFAULT_BATCH_SIZE = 4` products per LLM call,
  `MAX_TOKENS = 16000`. The configured reasoning model shares that budget
  between its chain-of-thought and the visible content, so an oversize batch
  returns empty content with `finish_reason="length"`. `parse_llm_keywords`
  salvages a truncated fenced-JSON tail and the empty-content error names the
  finish reason. `dedupe_keywords` then collapses any repeated keyword still
  written (logging a WARNING) before the pool `validate_pool`, awarding a
  contested string to the product with the fewest keywords of its own rather
  than to the batch that answered first — first-wins always starves the later
  batches. `validate_pool`'s duplicate check stays as the guard for
  directly-called or hand-merged pools (and is what makes a cross-chunk
  repeat fatal on the merged pass).
- **Deliverable**: `outputs/step-4-gold-keywords.json` (+ `.md` digest,
  modifiers nested under their broad term) — Step 5's keyword bank; the
  payload records the chunk sizes it was generated from. The runner prints
  the bank plan (chunk sizes, per-chunk target, summed target, bank band and
  the LLM-call estimate) before the first call, and exits non-zero on
  `KeywordGenerationError` having written nothing.

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

**Widened-bank live run (2026-09-29, branch `fix/gold-funnel-bottlenecks`, operator's
go):** the regenerated deliverable holds **320 unique keywords across 40 gold
products** — the table is capped at 40 because the one-per-source-keyword
collapse leaves exactly 40 distinct type groups in the whole 263-row
deliverable (rows w/o `source_keyword`: 0; banned-name rows: 7), so
`DEFAULT_MAX_PRODUCTS=150` selects all of them. The operator chose
`--chunk-size 8` (5 pools × 8 products × 8 keywords/product, summed target
320) to keep the bank inside his 250–320 intent; the merged pass validated
against its auto-scaled band **[270, 340]** with zero cross-chunk repeats.
~12 reasoning-LLM calls, zero Apify spend, written as
`outputs/step-4-gold-keywords.{json,md}` (roles 159 broad / 161 modifier).

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

### 13.6 Step 5 — dual-supplier gold-kernel ingestion (implemented)

```bash
source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
    [--keywords outputs/step-4-gold-keywords.json] \
    [--target-per-keyword 2] [--limit 4] \
    [--only {both,cjdropshipping,aliexpress}] [--export-root <dir>]
```

Ingests **every** Step-4 bank keyword through **both** supplier pipelines — CJ
MCP and the AliExpress Dropshipping Center — and exports what survives into
the gold-kernel tree:

```
<OPTIMAL_EXPORT_DIR>/cjdropshipping/product-NN/…
<OPTIMAL_EXPORT_DIR>/aliexpress/product-NN/…
```

`src/pipeline/keyword_bank.py` is the engine; `scripts/ingest_keyword_bank.py`
is the runner. **This is not `run_pipeline`'s fallback chain.** Auto mode
(§9) stops at the first engine that answers; Step 5 needs *both* engines to run
for *every* keyword, because the point of the step is to obtain the optimal
product from each supplier and let Step 6 (§13.1) rank them against each other.
So each **(keyword, engine) leg** is its own call into the existing
`run_pipeline` with a **single-engine list**, a **shared evaluator** and a
**per-engine exporter** — the supplier core (§5–§9) is reused, never forked,
and **no gate is relaxed**: every leg still runs CJ's commercial gate (§6.2),
MCP payload liveness gate (§5) and mandatory freight quote (§6.3); the DS
Center's winning-product gate (§6.1) with its AU market pin; and, for both, the
LLM viability gate, the margin floor and the 3-image gallery gate.

- **Failure is per-leg, not per-run.** An engine that is unconfigured,
  blocked or timed out for a leg is logged at WARNING and skipped **for that
  leg only** — the other engine still runs that keyword, and `leg_failures`
  records the skip. Only when **every** registered engine failed **every**
  keyword does ingestion raise `BankIngestionFailedError`, which the runner
  renders as the intervention block.
- **`--only` narrows without forking the engine.** The script narrows the
  extractor-factory dict to one engine before calling `ingest_keyword_bank`;
  the plan's signature gains no extra parameter for it.
- **The loader is fail-closed** (plan §7.1). `load_keyword_bank` rejects a
  missing, malformed, empty or non-list bank, a non-object or non-string row,
  and — defence in depth on top of the Step-4 pool validator — any keyword
  carrying a banned AICIS token, each with remediation text naming
  `scripts/generate_gold_keywords.py`.
- **Numbering and dedupe are per exporter, hence per supplier.** Each engine
  gets its own `CandidateExporter` bound to its own subfolder, so the two
  suppliers number their `product-NN` sequences independently and the same
  product landing from both suppliers is exported **twice — intentionally, as
  two fulfilment options** (plan §7.2). The subfolder is created on the first
  write, so a run that exports nothing leaves no empty directory.
- **Counters fold as deltas, read off the exporter.** `skipped_duplicate` and
  `dropped_no_valid_images` are cumulative *instance attributes* on
  `CandidateExporter` (the exporter is reused across an engine's legs, which is
  what makes duplicate detection span the whole bank), and `run_pipeline`
  copies them onto each leg's `PipelineSummary` only on its normal exit — so a
  leg reports the running total, and an **empty-funnel leg reports zero** (the
  early return at `src/main.py:191` precedes that copy). The bank therefore
  snapshots the **exporter's own** attributes before each leg and folds
  `after - before`, which sums to the exporter's real count without
  double-counting a leg and without going negative on a leg that scraped
  nothing — the two failure modes the leg-summary fold had. Guarded by
  `test_ingest_folds_exporter_counters_as_deltas_not_running_totals` and
  `test_ingest_does_not_go_negative_when_a_leg_scrapes_nothing` (§11). The
  other counters (`candidates_scraped`, `evaluated`, `accepted`, `rejected`,
  `dropped_llm_validation_failed`, `candidates_exported`) are per-call and fold
  directly. `BankIngestionSummary` adds `keywords_run` and `leg_failures` per
  engine, plus `keywords_empty` (keywords for which neither engine returned a
  verified product) and `total_exports`.
- **Config:** `BANK_TARGET_PER_KEYWORD` (`2`) is a **per-leg** target, so the
  keyword count multiplies it; `OPTIMAL_EXPORT_DIR` is the gold-kernel root
  (§10; both keys are in `.env.example` and `.env`, blank → default).
- **Runner blocks** (same rendered shape as §9): *Step 5 Keyword Bank*,
  *Step 5 Supplier Ingestion* (both engines down), *AliExpress Dropshipping
  Center Session* (`DsCenterSessionExpiredError` is deliberately **not**
  swallowed by `run_pipeline`, so it halts the bank with re-login steps),
  *LLM Evaluation Filter Configuration* (the shared evaluator is built before
  the first leg), and *Step 5 Funnel Exhausted* (zero exports overall). A run
  with fewer packages than the target still prints `[STEP 5 COMPLETE]` and
  exits 0.
- **Deliverables stay untracked.** The two supplier trees are production
  deliverables, not repo artefacts (plan §13.1: production deliverables live
  under the gitignored paths).

**Step-5 live evidence (2026-09-29):** three green pilots established the path
end to end — AliExpress-only, CJ-only, and the plan's mandated
`--limit 4 --target-per-keyword 1` both-engines pilot, which ended
`[STEP 5 COMPLETE]` with both supplier roots populated. Two live behaviours
were confirmed against the real suppliers on the pilot: the per-engine
`skipped_duplicate` skip correctly re-skipped products an earlier single-keyword
pilot had already exported (the duplicate check spans a supplier folder across
runs), and the counter fold was found wrong *because* the pilot's `duplicate`
count was impossible as a true total.

**Full 60-keyword run (operator's go, same day, 08:31→10:45, exit 0).**
`[STEP 5 COMPLETE]`: 60 keywords × 2 engines, **0 failed legs**, 5 keywords
empty (no verified product from either supplier), **33 packages exported** —
cjdropshipping 22 (`scraped=186 evaluated=174 accepted=41 rejected=133`) and
aliexpress 11 (`scraped=67 evaluated=48 accepted=16 rejected=32`); TOTAL
`scraped=253 evaluated=222 accepted=57 rejected=165`. All **38 packages now on
disk** (the 33 + the 5 the earlier pilots had already written, which the full
run re-offered and skipped as duplicates) are contract-complete: all 13
metadata keys, a freight-itemised basis on every CJ package, an honest
unquoted basis on every AliExpress one, `image_source=supplier_gallery`
throughout, and every package clearing the margin floor.

**The printed summary's `no_images` / `duplicate` figures were wrong on that
run** (CJ `no_images=-8 duplicate=-43`; Ali `-50 / -200`). Cause: the bank
folded `leg.counter - before`, and an empty-funnel leg returns from
`run_pipeline` *before* the exporter's cumulative counters are copied onto the
leg summary — so that leg contributed `0 - before`. The run log carries the
truthful counts (21 duplicate skips: CJ 17, Ali 4; 3 no-image drops). Fixed as
described above; `candidates_exported` was never affected, since it comes from
`summary.exports`, and all 33 exports in the summary were verified against the
`Exported` log lines.

**Full 320-keyword widened run (2026-09-29 → 09-30, branch, operator's go,
exit 0).** First attempt aborted ~1.2h in (34 keywords) on an uncontained CJ
transport drop — the `CancelledError` containment above is the fix; the
restart finished in ~11h20m: `[STEP 5 COMPLETE]`, 639 legs (319 CJ run +
320 Ali, 1 failed leg), TOTAL `scraped=1061 evaluated=1048 accepted=391
rejected=657 exported=82` — **100 CJ packages on disk** (82 new + the 18
carried over from the crashed attempt, numbering continuous, zero
overwrites; the contract audit on all 100 is clean: 13 keys, freight-itemised
bases, no duplicate URLs, ≥3 images each). The margin-floor widening is
visible in the funnel (an ACCEPT at AUD 15.47 / 1.39x that the old AUD 20
arm auto-rejected). AliExpress contributed **0 exports**: every leg answered
~21 search hits, but the anti-bot harvested shells (see §12's AliExpress
note) — `scraped=6 evaluated=6 accepted=1` and the `aliexpress/` subfolder
never created, as it is written only on its first export. The operator
cleared the 38 PR#3-run packages before this run; the general-intake
`dropship-candidates/` tree was untouched.

### 13.7 Step 6 — Jev product ranking (implemented)

```bash
source .venv/bin/activate && python scripts/rank_optimal_candidates.py \
    [--gold-products outputs/step-3-gold-standard-products.json] \
    [--export-root <OPTIMAL_EXPORT_DIR>] [--batch-size 4] \
    [--output outputs/step-6-ranked-candidates.json] [--markdown …]
```

Ranks **every** Step-5 gold-kernel package against the Step-3 gold-standard
product list with **Jev** (TypeSafe **System One** via OpenRouter) and writes a
tiered report. This is Jev's *new* role: **post-ingestion product ranking** — a
product ranker, never a keyword gate (§13.1, plan §0.1). The two-tier keyword
gate it replaces (and every `supplier_probe` / `[AICIS-T1-DROP]`-style tag) was
never built and must never be.

**Intake** is the gold-kernel tree (`<OPTIMAL_EXPORT_DIR>/{cjdropshipping,
aliexpress}/product-NN/`) plus the Step-3 deliverable. Every package in both
supplier folders is ranked; nothing is filtered on the way in. A package whose
`metadata.json` is missing or non-object is skipped with a warning (the
exporter's own duplicate-scan tolerance); nothing found at all raises
`JevRankingError` naming `scripts/ingest_keyword_bank.py`, and a missing gold
deliverable raises the same naming Step 3. The gold set is settled and the
client built **before** the first System One call, so a bad intake never costs a
billable request.

**One shared state + many independent questions is the native batch (§8.0).**
Each call's `state` is an object — `{"gold_reference": <Step-3 fields>,
"products": {<slug>: <Step-5 metadata fields>}}` — and every question in the
call sees that whole state. Three typed questions ride each package:
`<slug>__similarity` (score: 5 ordered levels), `<slug>__winning_value` (score:
5 ordered levels) and `<slug>__pillar` (choice: `curated_home` /
`self_care_rituals` / `other` / `discard`). `JEV_BATCH_SIZE` defaults to **4**,
so a call carries 12 questions — right at the docs' cited ~13-questions-per-call
envelope (11.5× cheaper, 9.6× faster than separate calls). The slug is
engine-qualified (`cjdropshipping_product_04`, `aliexpress_product_04`): the two
suppliers number their folders independently, so an unqualified slug would
collide in one state; and it never contains `__`, which the question-id
`<slug>__<question>` scheme reserves.

**Scoring and tiers.** `rank_score = 0.6·similarity + 0.4·value` on a 1–5 scale;
`≥ JEV_SHORTLIST_MIN_SCORE` (3.5, widened from 4.0 on 2026-09-29 — the old
floor needed near-perfect similarity and left the viable review band
unharvested) → `shortlist`, `≥ JEV_REVIEW_MIN_SCORE` (2.5)
→ `review`, else `disregard`. A package whose answer keys are missing lands as
`disregard` with a note; a `discard` pillar adds its own note without moving the
tier. The weights and thresholds live in `jev_client.py` (`SIMILARITY_LEVELS`,
`VALUE_LEVELS`, `PILLAR_OPTIONS`, `QUESTIONS`, `SHORTLIST_MIN_SCORE`,
`REVIEW_MIN_SCORE`) — "judgement in one place", per the vendor's review
principle.

**Post-evaluation compliance gate (override, not a suggestion).** After the
scores land, the ranker scans each package's human text — `product_title`,
`marketing_ad_copy` and `features` (metadata.json has no description field,
so the marketing payload is the description role) — against
`COMPLIANCE_BANNED_TOKENS`: the keyword engine's own `BANNED_TOKENS` tuple
(one source of truth, the AICIS boundary) plus `electric`, `usb`,
`rechargeable`. A match forces the tier to `disregard` with an explicit
`compliance gate: banned token '<tok>' matched in <field>` note, no matter
the score, so contraband (mineral/stone tools like jade and quartz,
cosmetics/consumables, battery-powered devices) never reaches shortlist or
review. The scan is case-insensitive substring matching — the same semantics
as every other banned-token check in the repo — so an innocent word holding
a token (e.g. copy that explains "soak the knife in oil") is caught too; the
note names the token and field so Step 7 can see exactly why.

**Jev's raw `score` is 0-BASED, and the client shifts it onto 1–5.** The plan
(§8.0) assumed a 1-indexed position; the live probe (2026-09-29) returned
`score = 3.24` against a 5-entry criteria list whose `legend` keys were
`"0".."4"` — i.e. the position is zero-based and may fall between levels.
`jev_client.answer_score` therefore adds `JEV_SCORE_INDEX_SHIFT = 1.0` so 0 maps
to 1.0 and 4 maps to 5.0, keeping the config defaults (3.5/2.5 since the
2026-09-29 shortlist widening) meaningful on the intended 1–5 scale: with the
shift, a shortlist score means "close match or better". The finding is recorded in the
`jev_client.py` docstring and in §12.

**A failed call retries once, and only that batch degrades.** `_decide_with_retry`
retries one `JevError`; if the second attempt fails, only that batch's packages
become `disregard` with `Jev batch call failed: <exc>` — the run never aborts on
one bad call and later batches still rank.

**Ranking is a report, not a deletion (plan §8.3).** No package directory is
moved or removed — the ranker writes the tiered report and lets Step 7 (human)
decide what to validate and link. Jev authors no product fact either: it returns
only a similarity score, a winning-value score and a pillar; every field in the
report is copied verbatim from the package's `metadata.json`.

**Deliverables**: `outputs/step-6-ranked-candidates.json` (per-package verdicts
plus a `summary` and a `tiers` grouping) and `.md` (tier-grouped digest) — the
**untracked** `outputs/` tree (plan §11.8: production deliverables live in the
gitignored tree, not `plans/`).

**Runner blocks** (same rendered shape as §9/§13.6): *Step 6 Ranking Intake*
(gold list or gold-kernel tree missing/empty) and *LLM Evaluation Filter
Configuration* (`OPENROUTER_API_KEY` unset). Exit `0` complete, `1` intervention,
`2` unexpected error; a run always prints `[STEP 6 COMPLETE]` with the tier
counts.

**Step-6 live run (2026-09-29, operator's go):** the 38 Step-5 packages
(cjdropshipping 25, aliexpress 13) were ranked against **263** Step-3 gold
products in **10 System One calls** of 4 packages each — **0 failed batches**,
no retries needed. Result: **shortlist 12, review 8, disregard 18**. The
shortlist leads with body brushes (`aliexpress/product-02`, 4.43) and garlic
presses (`aliexpress/product-09`, 4.41; `aliexpress/product-08`, 4.30;
`cjdropshipping/product-06`, 4.25) plus gua sha boards (`aliexpress/product-10`,
4.36; `cjdropshipping/product-25`, 4.33) — exactly the Step-3/Step-4 pillars the
gold list is built on, which is the signal the similarity axis works. The
deliverables were written to `outputs/step-6-ranked-candidates.{json,md}` and
confirmed gitignored (`.gitignore:31` = `outputs/`).

**Step-6 live re-rank (2026-09-30, branch, operator's go, exit 0):** the 100
CJ packages of the 320-keyword widened run against the 263 gold products in
**25 System One calls** of 4 packages each — **0 failed batches, ~65s wall
clock**. Result at the widened floors: **shortlist 13, review 17, disregard
70** (16.1% shortlist rate on a 2.6x wider cohort vs the old run's 12 of 38).
The shortlist leads with self-care face tools (ice rollers, gua sha boards)
and curated-home kitchen items (garlic press, tea-infuser glassware). The
report does not itemize OpenRouter billing; the 25-call spend is ~$0.02 at
the vendor's published input rate. With the compliance gate live the re-run
demoted **43 of 100** packages to `disregard` (16 of them out of
shortlist/review — including the then-#2 `jade` gua sha board), landing
**shortlist 9, review 4, disregard 87**.
