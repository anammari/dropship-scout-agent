# CLAUDE.md - Dropship Scout Agent (The Morning Anthem, Supplier-First)

> Aligned with the implemented codebase as of **2026-10-12**, on branch
> `feature/morning-anthem-pipeline-update`: **the whole pipeline is implemented —
> §1 (the rebrand sweep) and Steps 2–6** — with §1 and Step 2 live-piloted and
> Steps 3–6 hermetically tested, awaiting their first live run. Only the human
> steps 7–8 remain (see §13.8).
> This file is the operating mandate and the single source of operating
> context: it carries the schemas, gate rules, config reference and
> operational notes.
>
> **The store is The Morning Anthem** — a premium morning-lifestyle brand
> launching **Australia-only, in AUD**, sourcing from **CJdropshipping
> exclusively** and researching through **HasData exclusively**. Both former
> second sources (the AliExpress Dropshipping Center and the Apify Google
> Shopping actor) are retired — see §1's inventory. §12 and the §13.4–13.7
> evidence blocks record what those pipelines found before retirement; they
> are history, not instructions.

## 1. MISSION & HIGH-LEVEL OBJECTIVE

The **Dropship Scout Agent** scouts dropshipping product candidates against
strict commercial and logistical criteria and exports 3–5 validated,
high-margin winning product packages directly into the Shopify workspace:

```
/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/
```

The pipeline ingests **live, verified products directly from the supplier
API** (Supplier-First architecture): every candidate starts with a real
supplier product URL, a real listed price, and the supplier's own image
gallery *before* the LLM ever sees it. CJdropshipping is read through its
official **MCP server** (StreamableHTTP) — see §5 — and it is the pipeline's
**only** supplier engine.

```
[Stage 1] CJdropshipping MCP extractor (sole engine, SUPPLIER_PRIORITY_ORDER)
          hits must pass the CJ Commercial Gate (§6.2), the MCP Payload
          Liveness Gate (§5) and the mandatory Freight Quote (§6.3)
                         │  List[RawSupplierProduct]  (verified ground truth)
                         ▼
[Stage 2] LLM viability & marketing evaluation (deepseek-v4-flash:cloud)
                         ▼
[Stage 3] Deterministic CDN image download (image_sourcing.py, zero LLM)
                         ▼
[Stage 4] Workspace export (exporter.py): product-NN/{metadata.json, images/}
```

**Retired in the §1 rebrand sweep** (do not rebuild): the AliExpress
Dropshipping Center extractor (`extractors/aliexpress_ds.py`), its session
script (`scripts/generate_ali_session.py`), its config keys (`ALI_DS_*`,
`MIN_DS_*`) and its test module; the Apify Google Shopping wrapper
(`extractors/google_shopping.py`, `apify-client`, every `APIFY_*` key, the
`apify-trends` MCP server and its `mcp_headers.py` branch) and its test
module. `ShoppingRow` — the row contract the Step-3 curator reads — moved to
`src/extractors/shopping_row.py` when the wrapper that used to hold it was
deleted.

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
   | CJdropshipping | `/product/<pid>.html` or `/product/<slug>-p-<pid>.html`; `<pid>` is numeric **or** UUID-form (`B03F2DFF-276D-481C-AD18-28DF22E411CC`) |

   CJdropshipping is the only known supplier (`KNOWN_SUPPLIERS`), so a CJ-named
   product pointing at ANY other marketplace's item or search URL is rejected.

6. **Strict schema validation over prompt trust:** instructor/Pydantic
   validators reject and force a retry (or downgrade to REJECT) on any
   violating LLM output.

## 3. THE 5-POINT EVALUATION GATE (LLM system prompt)

Any candidate failing two or more points is marked `REJECT`:

1. **Morning-Ritual Fit** — the product belongs to one of The Morning
   Anthem's five pillars (§1.1). A product that fits no pillar MUST be
   REJECTed on this gate.
2. **Margin Viability (AUD)** — priced for **realistic Australian retail** in
   the store's pillar, against the **strict landed cost** read from the
   supplier record: never a cheaper invented basis, and never a mechanical
   3x–4x multiplier on cost. The honest price must clear
   `MIN_MARKUP_MULTIPLIER` (2.5x) **or** leave `MIN_MARGIN_AUD` (AUD 10, the
   operator's target gross margin per unit — the 5-point gate prompt
   interpolates the live config values so it cannot drift) gross profit per
   unit; a product whose realistic AU price cannot clear that floor is
   REJECTed on this gate rather than priced up to fit (enforced twice:
   `_reconcile` downgrade in `llm_filter.py` and `_enforce_accept_gates` in
   `models.py`, both reading the same config keys the prompt interpolates).

   The prompt tells the model **which kind of cost it is judging**, via the
   payload's `shipping_quoted` flag (`RawSupplierProduct.shipping_quoted`):
   a supplier-quoted figure is VERIFIED and final, whereas a supplier that
   quotes no freight leaves a **floor** — the model is instructed to price
   conservatively and to REJECT a product whose case rests on that best case.
   Only the prompt changes on that path; the margin floor itself is unchanged,
   and no freight figure is ever invented for a supplier that will not quote
   one.
3. **Logistical Feasibility** — < 1.2 kg, durable, non-perishable,
   air-freight compliant to Australia from China (inferred from
   title/description only).
4. **Saturation Resistance** — not an everyday Kmart/Target/Bunnings/Big W/
   Woolworths commodity, nor a Walmart/IKEA one as the store expands.
5. **Demonstration Appeal** — a 3-second visual problem→solution dynamic.

### 1.1 THE FIVE MORNING ANTHEM PILLARS

One importable constant tuple per module, identical everywhere
(`src/keywords/generator.py::PILLARS`, `src/evaluators/gold_curator.py::PILLARS`,
and the keys of `src/ranking/jev_client.py::PILLAR_OPTIONS`):

| Pillar key | Pillar | Product lines |
|---|---|---|
| `brew_fuel` | The Brew & Fuel (core) | Manual coffee tools, espresso distribution gear, portable milk frothers, minimalist tumblers, matcha accessories |
| `sunrise_move` | The Sunrise Move | Aesthetic active gear, commuter backpacks, waterproof pouches, hands-free dog leashes, sand-free towels |
| `mindful_awakening` | The Mindful Awakening | Backflow incense burners, digital gratitude journals, shower steamers, meditation accessories |
| `restful_reset` | The Restful Reset | Silk eye masks, ergonomic recovery pillows, sunrise alarm clocks, facial ice rollers |
| `morning_table` | The Morning Table | Cutting boards, garlic presses/mashers, breakfast preparation and serving kitchen aids |

There is **no `other` pillar**. The retired `curated_home` /
`self_care_rituals` / `other` trio (and the Sabaah Goods "Modern Arab-Aussie
Lifestyle & Cultural Nostalgia" niche copy) must not reappear anywhere.

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
│   │   ├── cj_mcp_extractor.py      # CJdropshipping MCP — the sole supplier: commercial gate, liveness gate, freight quote
│   │   ├── google_shopping_hasdata.py # Step-3 HasData Google Shopping (the only shopping source, §13.2)
│   │   └── shopping_row.py  # the shared ShoppingRow row contract (curator + scraper)
│   ├── research/            # Step-2 trend research (§13.8)
│   │   ├── trend_gate.py    # Jev product-intent gate + banned-token screen
│   │   └── __init__.py
│   ├── evaluators/
│   │   ├── llm_filter.py    # instructor + ProvisionalProductEvaluation
│   │   └── gold_curator.py  # Step-3 LLM curation: select-by-url, facts code-assembled (§13)
│   ├── keywords/            # Step-4 gold-standard keyword engine (§13.3)
│   │   ├── gold_keyword_prompt.md  # Step-4 prompt resource ({PRODUCT_TABLE} slot)
│   │   └── generator.py     # Batched generation + code-side pool validation
│   ├── ranking/             # Jev: Step-2 gate, Step-3 pillar gate, Step-6 ranking (§13.8)
│   │   ├── jev_client.py    # System One transport + all question/threshold constants
│   │   └── jev_product_ranker.py  # Batching, composite score, tiers, report writer
│   ├── pipeline/
│   │   ├── cj_mcp_client.py # CJ MCP client + MCP Payload Liveness Gate
│   │   ├── image_sourcing.py# deterministic CDN image download/validation
│   │   └── keyword_bank.py  # Step-5 bank loader + CJ ingestion (§13.6)
│   ├── exporter.py          # workspace writer (metadata.json + images/)
│   └── main.py              # CLI orchestrator, funnel counters, exit codes
├── scripts/
│   ├── verify_cj_gate.py        # CJ list-count threshold diagnostic (no LLM, no export)
│   ├── gate_trend_keywords.py        # Step-2 Jev product-intent gate runner (§13.8)
│   ├── run_gold_standard_research.py  # Step-3 gold-product research runner (§13)
│   ├── generate_gold_keywords.py     # Step-4 keyword bank runner (§13.3)
│   ├── ingest_keyword_bank.py        # Step-5 CJ ingestion runner (§13.6)
│   └── rank_optimal_candidates.py    # Step-6 Jev ranking runner (report only, no deletion)
└── tests/                   # 534 hermetic tests, zero network (14 modules + conftest)
```

**Retired pipelines — do not rebuild.** The Meta Ad Library scraper
(`playwright_scraper.py`, `apify_scraper.py`), fuzzy-match sourcing
(`pipeline/supplier_sourcing.py`), the CJ REST client and extractor
(`pipeline/cj_client.py`, `extractors/cj_api_extractor.py`), the
Apify-hosted AliExpress scraper (`extractors/aliexpress_apify.py`) and the
per-candidate DS Center gate it carried (`ENABLE_DS_CENTER_GATE`), the Etsy
extractor (`extractors/etsy_api.py`) — **Etsy is retired outright: it does
not support dropshipping** — and, as of the §1 Morning Anthem sweep, the
**AliExpress Dropshipping Center extractor** and the **Apify Google Shopping
wrapper** with every key, dependency and test they carried. CJ is MCP-only
(§5), HasData is the only research and shopping source (§13.8), and
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

**The Morning Anthem registers ONE engine: CJdropshipping (§6.2, §6.3).** The
chain machinery is kept (a second supplier would need no rewrite), but
`SUPPLIER_PRIORITY_ORDER` defaults to `cjdropshipping` alone, the registry
holds one entry, and the CLI's `--extractor` choices are `{auto,
cjdropshipping}`.

Extractors subclass `BaseSupplierExtractor.fetch_products(keywords,
country=None) -> List[RawSupplierProduct]` and share one exception taxonomy:
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
- ~~`aliexpress_ds.py`~~ — **retired in the §1 Morning Anthem sweep.** The
  AliExpress Dropshipping Center extractor, its MTOP/Playwright machinery, its
  AU market pin, its winning-product gate (§6.1), its saved-session script and
  its config keys are all deleted. CJ is the only supplier; do not rebuild it.

### 6.1 WINNING-PRODUCT GATE (AliExpress) — RETIRED, recorded for history

> **Retired with the AliExpress extractor (§1).** No code path enforces this
> gate today. It is kept in the mandate only because the CJ commercial gate
> (§6.2) was modelled on it and the two shared a shape: a quantitative
> pre-LLM gate with fail-closed semantics and a descending sort.

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
gets `Standard tracked international shipping to Australia from China.` and
nothing more: naming a service or a transit window without a quote is the same
class of unsupported claim. (CJ always quotes freight, so this branch is the
defensive one.)

## 9. ORCHESTRATION (`src/main.py`)

```bash
python -m src.main --keyword "kitchen gadgets" --target-count 3 \
                   [--country AU] [--extractor {auto,cjdropshipping}]
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

Current triggers: **Supplier Extraction Stage** (the CJ engine
unavailable/blocked), **LLM Evaluation Filter Configuration** (missing LLM
env config), **Pipeline Funnel Exhausted** (zero exports after all gates).
Drops at individual gates are surfaced in the run summary, not as
interventions. (The AliExpress DS Center Session trigger went with the
extractor.)

### Optional saved-session script — RETIRED

`scripts/generate_ali_session.py` and the whole saved-session concept went
with the AliExpress extractor (§1). CJ's MCP server needs only its token.

## 10. CONFIGURATION (`.env`, read by `src/config.py`)

| Key | Default | Used by |
|---|---|---|
| `CJ_MCP_TOKEN` | — | cj_mcp_client / cj_mcp_extractor (the sole supplier; token in URL path) |
| `CJ_MCP_BASE_URL` | `https://developers.cjdropshipping.com/mcp` | cj_mcp_client (token appended) |
| `LLM_BASE_URL` / `LLM_API_KEY` | — | llm_filter |
| `LLM_MODEL` | `deepseek-v4-flash:cloud` | llm_filter |
| `SUPPLIER_PRIORITY_ORDER` | `cjdropshipping` | extractor chain (one engine; the chain machinery stays) |
| `USD_TO_AUD` | `1.55` | all USD→AUD price math (CJ quotes USD; the store sells in AUD) |
| `MIN_CJ_LISTED_COUNT` | `150` | CJ commercial gate (minimum `listedNum`; CJ reports no sales figure) |
| `CJ_FREIGHT_METHOD` | *(blank = cheapest)* | CJ freight quote: pin a shipping service by CJ's own name |
| `CJ_MAX_PRODUCTS` | `10` | products expanded per CJ keyword (MCP search cap) |
| `CJ_DEFAULT_DEST_COUNTRY` | `AU` | the CJ freight quote's destination lane (AU launch; CJPacket EUB-class lanes) and the liveness gate's preferred stock country |
| `MIN_MARKUP_MULTIPLIER` | `2.5` | margin floor (markup leg), llm_filter + models |
| `MIN_MARGIN_AUD` | `10.0` | margin floor (gross-profit leg), llm_filter + models — the operator's target gross margin per unit |
| `TARGET_COUNTRY` | `AU` | extraction/evaluation target |
| `HASDATA_API_KEY` | — | **the exclusive data provider**, on one shared credit balance: (1) the Step-2 Google Trends MCP server, read from `.env` by `scripts/mcp_headers.py` for `.mcp.json` (NOT by `src/config.py`); (2) the Step-3 HasData Google Shopping source (§13.2), read by `src/config.py` to call the REST API directly. Free tier 1,000 credits/month |
| `HASDATA_GS_BASE_URL` | `https://api.hasdata.com` | Step-3 HasData source (§13.2): REST base; the client appends `/scrape/google/shopping` |
| `HASDATA_GS_MAX_RESULTS_PER_KEYWORD` | `20` | Step-3 **local** cap on rows kept per keyword. NOT an API parameter — one request returns the whole grid (~65 rows observed) for a flat 10 credits, so this bounds downstream LLM curation only, never HasData spend (§13.2) |
| `TRENDS_GEO` | `AU` | Step-2 Trends geography (AU-first launch) |
| `SHOPPING_GL` / `SHOPPING_DOMAIN` | `au` / `google.com.au` | Step-3 Google Shopping market pin (AUD retail benchmarks) |
| `HASDATA_MONTHLY_BUDGET` | `1000` | the free-tier ceiling the production cap is checked against |
| `HASDATA_PRODUCTION_CAP` | `400` | max credits one full production run may plan (the launch-run profile, §13.8) |
| `HASDATA_GS_MAX_KEYWORDS` | `30` | Step-3 hard keyword-count cap (fail-closed before any credit moves) |
| `HASDATA_TRENDS_CREDITS_PER_CALL` | `10` | Trends rate the guardrail arithmetic uses — **10 per request, confirmed on the HasData dashboard 2026-10-12** |
| `HASDATA_SHOPPING_CREDITS_PER_REQUEST` | `10` | Shopping rate the guardrail arithmetic uses |
| `TRENDS_MAX_CALLS` | `10` | Step-2 Trends call ceiling (up to 5 comma-separated terms per call) |
| `MORNING_ANTHEM_TREND_KEYWORDS_PATH` | `outputs/json/morning-anthem-step-2-trend-keywords.json` | Step-2 deliverable (Jev-gated) — Step 3's intake |
| `MORNING_ANTHEM_GOLD_PRODUCTS_PATH` | `outputs/json/morning-anthem-step-3-gold-products.json` | Step-3 gold-product deliverable — Steps 4 + 6 read it |
| `MORNING_ANTHEM_KEYWORD_BANK_PATH` | `outputs/json/morning-anthem-step-4-keyword-bank.json` | Step-4 bank deliverable — Step 5's intake |
| `BANK_TARGET_PER_KEYWORD` | `2` | Step-5 packages exported per (keyword, engine) leg — a PER-LEG target, so the bank's keyword count multiplies it (§13.6) |
| `MORNING_ANTHEM_EXPORT_DIR` | `…/my-store-build/inspiration/morning-anthem-candidates` | Step-5/6 gold-kernel root; the `cjdropshipping/` subfolder is numbered independently (§13.6) |
| `OPENROUTER_API_KEY` | — | Jev (TypeSafe System One via OpenRouter), shared by the Step-2 product-intent gate, the Step-3 pillar gate and the Step-6 ranking (never logged) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Jev client base; it appends `/systemone` |
| `JEV_MODEL` | `typesafe/jev-1.13` | System One model id (pinned so a ranking is reproducible) |
| `JEV_BATCH_SIZE` | `4` | Step-6 packages per System One call (three questions each; ~13-question native batch) |
| `JEV_SHORTLIST_MIN_SCORE` | `3.5` | Step-6 tier floor: `rank_score` ≥ this → shortlist (1–5 scale) |
| `JEV_REVIEW_MIN_SCORE` | `2.5` | Step-6 tier floor: `rank_score` ≥ this → review, else disregard |
| `TREND_GATE_BATCH_SIZE` | `8` | Step-2 Jev product-intent gate: candidate strings per System One call |
| `TREND_SELECT_BATCH_SIZE` | `12` | Step-2 Jev **selection** pass: candidates per System One call |
| `TREND_SELECT_TARGET` | `30` | how many keywords the selection picks for the Step-3 scrape — **this IS the Step-3 budget** (10 credits each) |
| `TREND_SELECT_MIN_PER_PILLAR` | `2` | per-pillar floor inside each pillar's proportional selection quota, so a crowded pillar cannot consume every paid slot |
| `PILLAR_GATE_BATCH_SIZE` | `4` | Step-3 Jev pillar gate: candidate products per System One call |
| `EXPORT_DIR` | `…/my-store-build/inspiration/dropship-candidates` | exporter (general intake §9) |
| `USER_AGENT` | desktop Chrome UA | CDN downloads |

**Removed keys** (with their pipelines): `ALI_DS_STATE_PATH`,
`ALI_DS_MAX_PRODUCTS`, `MIN_DS_ORDER_COUNT`, `MIN_DS_RATING`, `APIFY_TOKEN`,
`APIFY_GS_ACTOR`, `APIFY_GS_MAX_RESULTS_PER_KEYWORD`,
`APIFY_GS_MAX_CHARGE_USD`.

Every key in `.env.example` is present in `.env` in the same order with the
same description; a key that is commented out **or present but blank**
resolves to its default in `src/config.py` (blank never means `0`/`False`).

`Settings` never logs or prints its values (no-leak repr); credentials live
only in `.env` / the real environment.

## 11. TESTS & ENVIRONMENT

- Hermetic suite: `source .venv/bin/activate && pytest tests/ -v` — **534
  tests, zero network** (httpx.MockTransport + fake MCP sessions + scripted
  LLM/System One transports). Every module's count is pinned against the
  pre-rebrand baseline except the two retired test modules, and the conftest
  blanks the LLM/HasData/OpenRouter credentials **on the `settings` singleton
  as well as in `os.environ`** — scrubbing the environment alone left the real
  keys reachable, which is how a stray live call got made once while the
  Step-2 gate was being written. A test that needs a credential patches it
  back explicitly.
- `tests/test_trend_gate.py` covers the Step-2 Jev product-intent gate
  (§13.8): the closed four-way verdict set, only-`product_search`-survives,
  the one-question-per-candidate batch envelope and its index-addressed
  instruction, the batch split, the settings-driven batch size, the
  retry-once-then-drop containment (a missing `answers` object degrades one
  batch, never the run), unparsable and out-of-set verdicts dropping the
  string, verbatim carry-through of keyword/pillar/evidence, that the question
  offers no slot for the model to author a string, the banned-token screen,
  the fail-closed loader (including a pillar outside the five Morning Anthem
  keys), and the deliverable + pre-gate audit-trail writers.
- `tests/test_keyword_bank.py` covers the Step-5 bank and its CJ ingestion:
  the fail-closed loader (missing/malformed/empty/non-list payloads,
  non-object and non-string rows, the banned-token defence in depth, the
  missing-`match` default and the exact-before-fallback ordering), and
  `ingest_keyword_bank` over fake extractors/evaluator/exporters — every
  registered engine runs for every keyword, one shared evaluator, the per-leg
  failure skip, the all-engines-failed `BankIngestionFailedError`, the
  empty-catalogue accounting, the per-leg early stop (it uses the real
  `run_pipeline`, so the funnel counters and the early-stop rule are
  inherited, not re-implemented), and the cumulative-exporter-counter delta
  fold — including the empty-funnel leg, which must fold as 0 rather than as a
  negative. The generic fan-out/leg-failure machinery is exercised with a
  **synthetic second engine patched in**, so coverage survives the retirement
  of the only other supplier. It writes to `tmp_path` and never touches the
  real supplier trees — see §13.6.
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
  (`tests/test_google_shopping.py` — the retired Apify wrapper's module —
  and `tests/test_aliexpress_ds.py` are deleted with their subjects; the gold
  curator's coverage lives in `tests/test_google_shopping_hasdata.py`.)
- `tests/test_google_shopping_hasdata.py` covers the Step-3 HasData source
  (§13.2): the request shape (the AU market pin — `gl`/`domain` — the
  `x-api-key` header, one request per keyword), row parsing against **both**
  the live payload shape (`source`/`reviews`, pinned by a verbatim row from
  the 2026-10-07 probe) and the reference guide's documented shape
  (`link`/`seller`/`ratingCount`), the canonical-`productId` URL fallback and
  the shared-identity collapse, the local row cap touching curation but never
  the request count, the credit telemetry, every fail-closed path (missing
  key, non-200 with the key absent from the logs, transport error, non-JSON,
  error `requestMetadata.status`, empty grid, a later keyword failing the
  run), the shared-`ShoppingRow` round-trip, the downstream curator
  compatibility (a parsed high-rating row clears the demand-evidence filter),
  and the runner's credit banner, keyword-cap/pilot-cap refusal, and the
  absence of any second source (HasData is the only shopping scraper).
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
- Python 3.13 venv at `.venv/`; install with `pip install -e ".[dev]"`.
  (Playwright and its Chromium download are no longer dependencies — the only
  consumer was the AliExpress DS Center path, retired in §1.)
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
- **[HISTORICAL] Why the Apify path was retired (three failures, all costing)
  — and then the whole path went with the §1 sweep:** an
  unauthenticated, new-user context is fed subsidised SuperDeals "welcome
  deal" prices — one exported package was costed at **AUD 1.53** against the
  DS Center's **US $7.22 (AUD 11.19)** for the same item, roughly 7x off,
  which the LLM then marked up into a hallucinated retail price; the actor
  priced against a **US** ship-to context and ignored `--country AU`, so
  logistics were validated for the wrong market; and it billed **per result**
  with a cold-container start that could exceed the run timeout.
- **[HISTORICAL] AliExpress cost basis:** the DS Center's AU-market quote was
  the authoritative landed cost, and its market pin (`aep_usuc_f`) was what
  kept it honest. Both went with the extractor. The transferable lesson is the
  one `CJ_DEFAULT_DEST_COUNTRY` now carries: a landed cost is only meaningful
  against a named destination market.
- **Static FX, by design (still live):** `price_aud` converts the supplier's
  USD with the static `USD_TO_AUD` (1.55). A marketplace UI shows its own live
  daily rate, so the operator will see a small gap. It is commercially
  negligible for margin work and is deliberately not "fixed" — a live FX feed
  adds a failure mode for ~6% on a cost basis that the margin floor absorbs.
- **Jev's `score` is 0-based, not 1-based (live probe 2026-09-29).** The plan
  (§8.0) recorded the score position as 1-indexed; the live probe returned
  `score = 3.24` against a 5-entry criteria list whose `legend` keys were
  `"0".."4"`. `jev_client.answer_score` therefore shifts the raw position by
  `JEV_SCORE_INDEX_SHIFT = 1.0` onto the 1–5 scale the thresholds are written
  against, so no config default had to move (§13.7).
- **[HISTORICAL] AliExpress pacing:** ~1–2 minutes per keyword before the PDP
  harvest. Retired with the extractor; recorded because the same "that is the
  pacing, not a hang" caution applies to the CJ leg (§6.3's note above).
- **[HISTORICAL] AliExpress PDP shell pages and the harvest retry
  (2026-09-30).** Ali's
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
## 13. THE MULTI-STEP WINNING-PRODUCT PIPELINE (The Morning Anthem)

> Appended as §13 (not renumbered into §5's position) so every existing
> §5–§12 cross-reference across the repo stays valid. This section covers the
> gold-kernel pipeline; the supplier core (§5–§9) remains the general intake.
> The engineering plan is the (untracked)
> `plans/technical/morning_anthem_pipeline_implementation_plan.md`, whose
> **LAUNCH OVERRIDES** section supersedes its body: the store launches
> **Australia-only, in AUD**, so the plan's body's USD rebase and `gl=us`
> defaults are **cancelled** — read AUD for USD and `au`/`google.com.au` for
> `us`/`google.com` throughout it.
>
> **§13.8 is the current state of this pipeline. §13.2–13.7 describe the
> superseded Sabaah-era implementation and its live evidence — read them as
> history**, with the two changes that matter called out inline: the pillar
> set is now the five Morning Anthem pillars (§1.1), and the supplier side is
> CJ-only while the research side is HasData-only.

### 13.1 Overview — gold-kernel intake vs general intake

The pipeline produces a **gold kernel**: a small set of proven-demand AU
retail products (Google Shopping evidence) that drives keyword generation,
supplier ingestion and Jev ranking, exported to
`my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/`
(Step 5, §13.6). The existing `dropship-candidates/` intake (§8) stays as
the general flow.

**Jev's role changed with the Morning Anthem plan: it is now used at three
steps** — the Step-2 product-intent gate, the Step-3 pillar gate, and the
Step-6 product ranking (§13.8). The earlier rule that "Jev is ranking-only
and never a keyword gate" is **superseded**; the two-tier keyword gate the
older spike proposed is still dead and must still never be built.

| Step | What | Status on this branch |
|---|---|---|
| 1 | Google Trends (**HasData MCP only**) research | done — the Apify fallback is retired; the live pilot used two HasData Trends calls |
| 2 | Trends → candidate strings → **Jev product-intent gate** → AU product-search keywords, tagged with the five Morning Anthem pillars | **done — §13.8**, deliverable `outputs/json/morning-anthem-step-2-trend-keywords.json` (untracked) |
| 3 | Google Shopping AU scrape (**HasData only**, no `--source`) + LLM select-by-url curation + **Jev pillar gate** → gold-standard product list | **done — §13.8**; §13.2 describes the superseded design |
| 4 | **Exact-title-first** keyword bank: each product's own title in code, plus one optional LLM fallback | **done — §13.8**; §13.3 describes the superseded design |
| 5 | **CJ-only** ingestion of the keyword bank → `morning-anthem-candidates/cjdropshipping/` | **done — §13.8**; §13.6 describes the superseded design |
| 6 | Jev ranks supplier candidates against the gold products | **done — §13.8**; §13.7 describes the superseded design |
| 7–8 | Human-only: CJ app linking; store curation | no code (deliberately) |

All research deliverables live under `outputs/` — **untracked**
(gitignored; scoping data, not repo artefacts) — while the Step-5 gold-kernel
packages are written into the Shopify workspace tree
(`morning-anthem-candidates/`, §13.6), like the general intake (§8).

**`plans/` is tracked** (operator decision 2026-10-12) and holds only plans that
describe live codebase behaviour, in two subfolders: `plans/technical/` (this
pipeline's implementation spec and the CJ gate directive) and
`plans/simplified/` (plain-English walkthroughs of the same two). Superseded
plans — the whole Sabaah-era multistep set and every AliExpress plan — were
removed. Step 6's Jev rankings live under the same gitignored tree as
`outputs/json/step-6-ranked-candidates.json` + `outputs/md/step-6-ranked-candidates.md` (§13.7), as Step 4's keyword bank
already does there (`outputs/json/step-4-gold-keywords.json` + `outputs/md/step-4-gold-keywords.md`).

**The `outputs/` tree is split by file kind.** Structured deliverables go to
`outputs/json/`, their readable digests to `outputs/md/`, and run logs to
`outputs/logs/` (stamped `…-YYYY-MM-DD-HHMM.log` from the log's own first
timestamp). The log folder is **hand-filled**: no runner installs a file
handler — they log to stderr and the operator tees the stream in (README
§"Where the log lines go"), so `logs/` is a capture convention, not a
runner behaviour. Every runner's `--output` default sits in `json/` and its
digest default is `config.digest_path(output)`, which resolves to the sibling
`md/` folder — a plain `with_suffix(".md")` would drop the digest back into
`json/` and undo the split. `digest_path` specialises the `outputs/json` pair
only: a custom `--output` anywhere else (including a folder that merely
shares the name `json`) keeps its digest beside it, so an off-tree
destination is never silently relocated.

### 13.2 Step 3 — gold-standard product research (SUPERSEDED — see §13.8)

> **Historical**, and now **re-implemented** — see §13.8 for the live design.
> Written when Apify and HasData were interchangeable sources and the pillar set
> was the retired three. Live today: HasData is the only shopping source (no
> `--source` flag); the market pin is `SHOPPING_GL`/`SHOPPING_DOMAIN`
> (`au`/`google.com.au`); the curator **no longer assigns a pillar**; and a
> **Jev pillar gate** runs last, admitting a product only if it fits one of the
> five Morning Anthem pillars.

```bash
source .venv/bin/activate && python scripts/run_gold_standard_research.py \
    [--source {apify,hasdata}] \
    [--limit 2] [--num 10] [--dump-raw /tmp/step3_raw_rows.json] \
    [--from-raw /tmp/step3_raw_rows.json] \
    [--output outputs/json/step-3-gold-standard-products.json]
```

- **Scrape — two interchangeable sources, selected by `--source`.** Both are
  drop-in peers: same `scrape_keywords(keywords) -> List[ShoppingRow]`
  contract, the same `ShoppingRow` dataclass, the same downstream curator and
  the same source-agnostic `--dump-raw` / `--from-raw` replay, so a deliverable
  is equivalent whichever free tier paid for it. `apify` stays the default, so
  an existing invocation is unchanged by the second source existing.

  * `src/extractors/google_shopping.py` (`apify`, default): ONE batched run
    of the `damilo/google-shopping-apify` actor (pay-per-result, ~$3.50/1,000
    results) carries every keyword (`queries` input), `country="au"`,
    `max_pages=1`. Spend is governed three ways: the actor's closed-set `num`
    is validated before any money moves; a hard `max_total_charge_usd`
    ceiling (`APIFY_GS_MAX_CHARGE_USD`) rides the run options and is enforced
    by Apify itself; `--limit N` pilots on the first N keywords.
  * `src/extractors/google_shopping_hasdata.py` (`hasdata`): one HasData
    Google Shopping **request per keyword** (10 credits each, the published
    rate) for operators spending HasData's free tier rather than Apify
    credit. One request returns the whole grid for a query (~65 rows observed
    live 2026-10-07), so `--num` is a **local** slice
    (`HASDATA_GS_MAX_RESULTS_PER_KEYWORD`, default 40 — parity with the Apify
    path's ~40-row SERP page) that bounds LLM curation cost only: **the
    request count is the keyword count, and the keyword count is the spend**.
    Fail-closed: a non-200, a non-JSON body, an error `requestMetadata.status`,
    an empty grid, or any keyword with no usable rows fails the run rather
    than yielding a half-scraped gold list that silently under-represents the
    Step-2 keywords.

  Both are **extractor-shaped but deliberately NOT a `BaseSupplierExtractor`**:
  these are marketplace retail listings with no supplier PDP, freight quote or
  gallery, so they can never become `RawSupplierProduct` without fabricating
  sourcing fields — neither module is registered in the extractor chain.

  **Why the HasData source is a Python HTTP client** (route finding, revised
  2026-10-07 after installing the skills — the first pass got half of this
  wrong, so both halves are recorded):

  * **The MCP route is closed.** The HasData MCP gateway
    (`https://mcp.hasdata.com/mcp?apis=…`) enumerates 27 API groups and
    `google_shopping` is **not** one of them — a live `tools/list` probe
    against `?apis=google_shopping` answers `No HasData tools match
    ?apis=google_shopping`, listing `google_serp`, `google_images`,
    `google_travel`, `walmart`, `amazon` and others. The endpoint is reachable
    only as REST.
  * **The skill/CLI route DOES cover Google Shopping** (an earlier draft of
    this section claimed otherwise — that was wrong, and the correction is
    deliberately kept visible so the mistake is not re-made): the installed
    `hasdata` skill lists `/scrape/google/shopping` in `references/search.md`
    ("Shopping carousel") with a Python recipe in `references/ecommerce.md`,
    and the installed `hasdata-cli` skill documents `google-shopping` as a
    first-class subcommand (`hasdata google-shopping --q … --gl au`, 10
    credits/call).

    So why not the skill? Because a skill is an **agent-facing instruction
    bundle, not an importable dependency** — it cannot be called by
    `scripts/run_gold_standard_research.py`. The CLI could be shelled out to,
    but that buys a per-machine binary (`~/.local/bin/hasdata`, plus an
    interactive `hasdata configure`) that a hermetic, CI-runnable, zero-network
    test suite cannot exercise, in exchange for nothing the REST call does not
    already give.

  The reference guide's own rule for code integration therefore applies —
  *"standard HTTP client integration is preferred"* — and the module calls
  `GET /scrape/google/shopping` directly with the `x-api-key` header. The
  request shape is independently confirmed by the official CLI, whose
  `--verbose` output builds the identical URL:
  `https://api.hasdata.com/scrape/google/shopping?domain=google.com.au&gl=au&q=…`.

  The skills remain installed and useful **for agent-side research** (ad-hoc
  `google-shopping` lookups while scoping keywords, for instance) — they are
  just not the pipeline's transport. See §15.

  **Schema note (live 2026-10-07) — the live payload is NOT the guide's
  example.** The guide documents `link`, `productLink`, `seller` and
  `ratingCount`; **none of those four appear live**. The real fields are
  `title`, `category`, `productId`, `price`, `extractedPrice`, `rating`,
  `reviews` (the review count), `source` (the seller), `thumbnail`,
  `delivery`, `immersiveProductPageToken`, `hasdataLink`. The parser reads both
  the documented and the live spellings rather than trusting either alone, and
  prefers a real `link`/`productLink` when one is supplied. Absent both (the
  live case), the row identity is derived from its own `productId` as
  `https://www.google.com.au/shopping/product/<productId>` — the same
  derived-not-invented shape as the CJ extractor's canonical
  `/product/{pid}.html` fallback (§6). `url` is the anti-hallucination join key
  and a human pointer only: Steps 4 and 6 never read it (Step 4 uses `name`,
  Step 6's `gold_reference` uses `name`/`description`/`retail_price_text`/
  `demand_evidence`), which is why a compact derived identity beats the
  per-row `hasdataLink` — that one is a ~2 KB opaque token pointing at
  HasData's own API, and a 2 KB exact-echo join key would be fragile and
  waste curation tokens. Rows sharing a `productId` share an identity, which is
  what lets the curator collapse the same product listed by several merchants.
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
- **Deliverable**: `outputs/json/step-3-gold-standard-products.json` (+ `.md`
  digest) — the reference set Steps 4 and 6 measure against. The runner
  prints the planned spend envelope **in the active source's own cost unit**
  (USD for Apify, credits for HasData — a shared cost model would misstate one
  of them), exits non-zero when nothing usable comes back, and `--from-raw`
  replays curation over a prior `--dump-raw` dump at **zero scrape spend** (the
  debugging path; the dump is source-agnostic, so a HasData dump replays free).
  The payload records the source it was scraped with (`source`, plus `actor`
  naming the actor or the HasData source).
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

### 13.3 Step 4 — gold-standard keyword bank (SUPERSEDED — see §13.8)

> **Historical, and now re-implemented** — see §13.8 for the live design. The
> broad/modifier pairing, the 50–70 pool band, the chunked pools, the adaptive
> per-product target and the merged bank band are all **deleted**: they existed
> to spread a *generated* pool across products it could not otherwise name, and
> when the primary keyword is the product's own title there is nothing left for
> a band to enforce. What survives is the template prompt with its
> `{PRODUCT_TABLE}` slot, the demand-ranked / type-diverse / pillar-balanced
> selection, the AICIS boundary, the batched calls with JSON-tail salvage, and
> the neediest-first duplicate collapse.

```bash
source .venv/bin/activate && python scripts/generate_gold_keywords.py \
    [--gold-products outputs/json/step-3-gold-standard-products.json] \
    [--batch-size 4] [--max-products 150] [--chunk-size 30] \
    [--output outputs/json/step-4-gold-keywords.json] [--markdown …]
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
- **Deliverable**: `outputs/json/step-4-gold-keywords.json` (+ `.md` digest,
  modifiers nested under their broad term) — Step 5's keyword bank; the
  payload records the chunk sizes it was generated from. The runner prints
  the bank plan (chunk sizes, per-chunk target, summed target, bank band and
  the LLM-call estimate) before the first call, and exits non-zero on
  `KeywordGenerationError` having written nothing.

### 13.4 Step-3 live evidence (2026-09-28) — HISTORICAL

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
`outputs/json/step-4-gold-keywords.json` + `outputs/md/step-4-gold-keywords.md` (roles 159 broad / 161 modifier).

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
Deliverables: `outputs/json/step-3-gold-standard-products.json` + `outputs/md/step-3-gold-standard-products.md` plus the
raw rows at `outputs/json/step-3-gold-raw-rows.json` (any future re-curation
replays
from that dump at zero Apify spend).

### 13.5 Step-4 live evidence (2026-09-28) — HISTORICAL

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
- Deliverables written to `outputs/json/step-4-gold-keywords.json` + `outputs/md/step-4-gold-keywords.md` on every
  green run — confirmed gitignored (`.gitignore:31` = `outputs/`). A failed
  run writes nothing.

### 13.6 Step 5 — gold-kernel ingestion (SUPERSEDED — see §13.8)

> **Historical: this was the dual-supplier (CJ + AliExpress) ingestion.** Step 5
> is now **CJ-only**: `BANK_ENGINES` holds one engine, the gold-kernel root is
> `MORNING_ANTHEM_EXPORT_DIR` (`…/morning-anthem-candidates`) with one
> `cjdropshipping/` subfolder, the runner has no `--only` flag, and the
> AliExpress DS Center Session intervention block is gone. The per-leg fold,
> the shared evaluator, the exporter-counter delta arithmetic and the
> fail-closed loader all still stand as described below.

```bash
source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
    [--keywords outputs/json/step-4-gold-keywords.json] \
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

### 13.7 Step 6 — Jev product ranking (SUPERSEDED — see §13.8)

> **Historical.** The transport, batching, composite score and tiering stand.
> Live today: `SUPPLIER_DIRS` is `("cjdropshipping",)`, `PILLAR_OPTIONS` is the
> five Morning Anthem pillars plus `discard`, the compliance gate scans the
> extended banned-token set (the AU big-box names plus walmart/target/amazon
> basics/ikea), and the deliverable is
> `outputs/json/morning-anthem-step-6-ranked-candidates.{json,md}`.

```bash
source .venv/bin/activate && python scripts/rank_optimal_candidates.py \
    [--gold-products outputs/json/step-3-gold-standard-products.json] \
    [--export-root <OPTIMAL_EXPORT_DIR>] [--batch-size 4] \
    [--output outputs/json/step-6-ranked-candidates.json] [--markdown …]
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

**Deliverables**: `outputs/json/step-6-ranked-candidates.json` (per-package verdicts
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
deliverables were written to `outputs/json/step-6-ranked-candidates.json` + `outputs/md/step-6-ranked-candidates.md` and
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

## 14. SHOPIFY TOOLING FOR THE STORE SIDE (Claude-facing, MANDATORY)

> Appended as §14 so every §1–§13 cross-reference stays valid — the same
> convention §13 used. This section governs how a session talks to the
> **Shopify store** that the exports of §8 and §13.6 feed. It changes no
> pipeline behaviour; it retires a broken MCP path and names the three routes
> that actually work.

Target store: **The Morning Anthem** (rebranded from Sabaah Goods on this
branch), domain `2giusj-zm.myshopify.com` (shop
`gid://shopify/Shop/83810844888`, org `233715106`, AUD,
Australia/Melbourne). The storefront is rebranded; the domain, shop id and
currency are unchanged, and the launch sells to Australia only. `shopify store
list -j` is what settles the domain — the `balawalashi.myshopify.com` spelling
that appears in some operator task text is **not** the canonical FQDN.

### 14.1 Ignore the small-business Shopify MCP outright

**`plugin:small-business:shopify` is retired here and must not be used for
anything.** It is not connected on this machine: every tool call answers

```
Incompatible auth server: does not support dynamic client registration
```

even though `claude mcp list` reports it `✓ Connected` — the CLI health check
probes the endpoint rather than completing the MCP handshake, so it is
systematically misleading. The server can also vanish from a session entirely
between restarts (observed 2026-10-01). Its instructions may still be
injected into a session before it disconnects.

Do not diagnose it, do not try to re-authenticate it, and never report a
store task as blocked on it. Use the three routes below instead — each is
already proven end to end.

### 14.2 Dev MCP — developer docs and schema validation

Installed as `shopify-dev-mcp` (stdio, **no authentication**, runs locally):

```bash
claude mcp add --transport stdio shopify-dev-mcp -- npx -y @shopify/dev-mcp@latest
```

Five tools. **`learn_shopify_api` must be called first** — it resolves the API
surface and version and returns a `conversationId` that every other Shopify
tool call requires:

| Tool | Use |
|---|---|
| `learn_shopify_api` | Mandatory first call; resolves surface + version, returns the `conversationId` |
| `search_docs_chunks` | Search shopify.dev docs/schema for the operation or field needed |
| `validate` | Validate a GraphQL operation or UI component against the schema before it runs |
| `validate_theme` | Validate Liquid / theme files |
| `feedback` | Report a toolkit scorecard; call once, at the end of the turn |

**This MCP is context and validation only — it has no store read/write
tools.** Never ask it to fetch a product, list orders, or write anything; it
cannot. It also does not validate `shopify.app.toml` /
`shopify.extension.toml` (use `shopify app config validate --json` for
that). For Admin work, pass `api_name: "admin"` to `search_docs_chunks` and
ALWAYS run generated GraphQL through `validate` with `api: "admin"` — that
call is what removes field hallucination from an Admin API operation.

### 14.3 The 21 Shopify AI Toolkit skills — context and validation

The toolkit's skills are installed and appear to the Skill tool under the
`shopify-plugin:` prefix. Each carries the current guidance for one surface;
read the one matching the surface being touched rather than working from
model memory.

| Area | Skills (all `shopify-plugin:`-prefixed) |
|---|---|
| Admin / data | `shopify-admin`, `shopify-custom-data`, `shopify-shopifyql`, `shopify-storefront-graphql`, `shopify-customer` |
| CLI / dev | `shopify-use-shopify-cli`, `shopify-dev` |
| Apps | `shopify-functions`, `shopify-payments-apps`, `shopify-app-store-review` |
| Extensions (Polaris) | `shopify-polaris-admin-extensions`, `shopify-polaris-app-home`, `shopify-polaris-checkout-extensions`, `shopify-polaris-customer-account-extensions`, `shopify-pos-ui` |
| Storefront / themes | `shopify-liquid`, `shopify-hydrogen` |
| Onboarding / partner | `shopify-onboarding-dev`, `shopify-onboarding-merchant`, `shopify-partner`, `ucp` |

Newer toolkit releases consolidate this set toward a single `shopify` skill
(see the doc's "Migrate from individual skills"); if a
`shopify-plugin:shopify` skill appears, prefer it.

### 14.4 Execution — Shopify CLI

```bash
shopify store execute -s 2giusj-zm.myshopify.com -q '<graphql>'
```

- Read-only queries run as-is; **mutations require `--allow-mutations`**.
- One-time auth per store and scope set:
  `shopify store auth --store 2giusj-zm.myshopify.com --scopes write_products,read_products,write_files,read_files`
  — the OAuth click is the operator's. Without it: "No stored app
  authentication found".

For batch writes the programmatic route is preferred: the CLI parks the
offline Admin token in
`~/Library/Preferences/shopify-cli-store-nodejs/config.json`, under
`<clientId>::<store>/sessionsByUserId/<uid>/accessToken` (`shpat_…`, ~24h
validity), POSTed as `X-Shopify-Access-Token` to
`https://<store>/admin/api/<version>/graphql.json`. Store-mutation utilities
live in `my-store-build/scripts/` (`shopify_admin.py`, whose `delete` is
`--yes`-gated because it is irreversible) — written untracked, never
committed.

**Never print, log, echo or embed the Admin token in an exception message.**
Credentials live only in `.env` / the real environment. Re-run the
`shopify store auth` flow when it 401s; do not try to exchange the refresh
token programmatically.

### 14.5 Repair notes

- The toolkit's own install paths — and the plugin-vs-skills-vs-MCP choice —
  are documented in the operator-local, gitignored
  `docs/Shopify-ai-plugin-doc.md`.
- If `shopify-dev-mcp` fails to connect, the usual cause is a **corrupt npx
  cache**, not the package: `Cannot find module '@shopify/liquid-html-parser'`
  means the cached tree under `~/.npm/_npx/<hash>/` is incomplete. Delete that
  one hash directory and it reinstalls clean — a clean `npm install` in a temp
  dir proves the package itself is fine.
- Requires Node.js 18+.

## 15. HASDATA AGENT SKILLS (Claude-facing, installed)

> Appended as §15 for the same reason §13 and §14 were: every existing
> §1–§14 cross-reference stays valid. This section covers the agent-facing
> HasData skills; it changes no pipeline behaviour.

Installed at project scope with the `skills` CLI (2026-10-07):

```bash
npx skills add hasdata/agent-skills -y
```

| Where | What |
|---|---|
| `.agents/skills/hasdata/` | The REST-API skill: `SKILL.md` + 10 `references/` (search, ecommerce, web-scraping, scraper-jobs, code-recipes, …). Documents `/scrape/google/shopping` and carries a Python recipe for it. |
| `.agents/skills/hasdata-cli/` | The CLI skill: `SKILL.md` + 10 `references/`. Drives `hasdata <subcommand>`; documents `google-shopping` at 10 credits/call. |
| `.claude/skills/{hasdata,hasdata-cli}` | Relative symlinks into `.agents/skills/`, which is how Claude Code discovers them. |
| `skills-lock.json` | Source + `SKILL.md` content hash per skill, so `skills experimental_install` can restore the set. **Committed.** |

**The CLI prerequisite lives outside the repo.** The `hasdata-cli` skill drives
the operator's own install (`~/.local/bin/hasdata`, v0.2.2) with the key at
`~/.hasdata/config.yaml` (mode 0600), written by an interactive
`hasdata configure`. Neither path is in this repo and neither is a pipeline
dependency — the Step-3 pipeline (§13.2) reaches HasData over HTTP from
Python, never through the CLI.

**These skills need a session restart to appear.** Claude Code registers its
skill list at startup, so a skill installed mid-session is on disk and
symlinked but **not invokable** until the session restarts (`Skill(…)` answers
`Unknown skill`). That is expected, not a broken install — to verify an install
without restarting, check the symlink resolves and that the `SKILL.md`
frontmatter `name:` matches its directory name.

**Scope of use.** The skills are for *agent-side research* — ad-hoc
`google-shopping` lookups while scoping keywords, market checks, one-off price
reads — and never for the pipeline's own ingestion, which stays hermetic and
repo-native. Every skill/CLI call spends real HasData credits on the same
monthly balance as the Step-2 Trends server and the §13.2 HasData source
(Trends 5/call, Shopping 10/call), so treat a `google-shopping` call as a
whole smoke-test budget and prefer replaying saved payloads over re-querying.

### 13.8 THE MORNING ANTHEM PIPELINE — current state (2026-10-12)

Branch `feature/morning-anthem-pipeline-update`. **§1 (rebrand sweep) and
Step 2 are implemented, tested and live-piloted. Steps 3–6 are not started.**

#### What is live

* **Supplier: CJdropshipping only** (§6.2, §6.3). One registry entry, one
  engine in `SUPPLIER_PRIORITY_ORDER`, `--extractor {auto,cjdropshipping}`.
* **Research: HasData only.** The Step-2 Trends MCP (`.mcp.json`) and the
  Step-3 Google Shopping REST source; the `apify-trends` server is gone.
* **Store: AU-only, AUD.** `CJ_DEFAULT_DEST_COUNTRY=AU`, `TRENDS_GEO=AU`,
  `SHOPPING_GL=au`, `SHOPPING_DOMAIN=google.com.au`; every `*_aud` field,
  `USD_TO_AUD` and `MIN_MARGIN_AUD` ($10.00) are retained.
* **Pillars: the five Morning Anthem keys** (§1.1).
* `src/research/trend_gate.py` + `scripts/gate_trend_keywords.py` — the
  Step-2 gate (below).

#### Step 2 — the Jev product-intent gate

Google Trends answers "what are people searching", not "what are people
buying", so every candidate string is classified by Jev before it may cost a
HasData Shopping request:

1. **One `choice` question per candidate**, `<i>__product_intent`, over a
   closed four-way set: `product_search` / `informational` / `news_or_brand` /
   `service_or_digital`. Only `product_search` survives.
2. **Batched** at `TREND_GATE_BATCH_SIZE` (8) candidates per System One call.
3. **Contained**: a failed call retries once, then drops *only that batch*
   with an explicit note. A missing `OPENROUTER_API_KEY` raises
   `JevConfigError` → the runner's intervention block.
4. **Jev authors nothing.** The keyword, its pillar and its Trends evidence are
   carried through verbatim; the question offers no slot for the model to write
   a string into.
5. **A survivor is also screened by `carries_banned_token`** — the keyword
   engine's own boundary. This was added *because of the pilot*: Jev rightly
   reads `milk frother kmart` as a product search (it is one), but that string
   could never become a bank keyword, so scraping it would waste a credit.
   Brand-*manufacturer* queries (`nespresso frother`) still pass by design —
   they are genuine product searches, and excluding branded goods is the
   Step-3 curator's job.

The pre-gate candidates are kept as `…-raw.json` with the verdict on each row,
so every drop is auditable.

#### Step 2 — the Jev selection pass (`src/research/trend_select.py`)

The gated pool is wider than the Step-3 scrape can afford (one Google Shopping
request per keyword, 10 HasData credits each), so the narrowing is a **decision
made by Jev**, not a hand-pick — the same call shape the Step-6 ranker uses.

**Jev's two Step-2 objectives:** **(A)** filter out non-products and irrelevant
Trends — the **gate** pass enforces it (four-way intent verdict + banned-token
screen); **(B)** pick the best 30 of the ~60 usable candidates across all five
pillars, **representing the researched pool so the outcome is as close to
pillar-unbiased as the data allows** — shared between the selection pass and
code. The split is the design: Jev judges one candidate at a time (a
per-candidate score cannot see the pool, so balance is not a judgement it can
make, and asking it to balance would make the outcome depend on batching), and
**code owns representation**.

* **One score question per candidate**, `<i>__keyword_quality`, over five
  ordered levels measuring one axis: how good the string is as a
  supplier-sourcing keyword for its pillar.
* **The state carries the context a decision model needs**: per candidate the
  query, its pillar, its Trends evidence **verbatim** and the *kind* of evidence
  (5-year mean vs rising/related-query figure — not the same claim); once per
  call the brand line, the task, the selection size and the five pillar
  descriptions.
* **The cut is code, and representative by construction**: each pillar gets a
  **quota** — its proportional share of the target, given a floor of
  `TREND_SELECT_MIN_PER_PILLAR` (2) and never more than the pool holds,
  reconciled to `TREND_SELECT_TARGET` (30) by largest remainder; within each
  pillar the best rows take the quota, and a shortfall fills by score across the
  pool. A pillar supplying 40 of 60 candidates takes ~20 of 30 slots rather than
  all 30, and no pillar that supplied candidates is dropped. Ties break on
  evidence-then-string, so a selection is reproducible. Batched at
  `TREND_SELECT_BATCH_SIZE` (12), inside the vendor's ~13-question envelope.
  `--no-pillar-quota` reverts to a pure best-of, and a floor that cannot fit the
  target **raises** rather than silently overspending the budget.
* **Fail-closed**: a failed batch retries once, then its candidates are
  unclassified and rank last. If **nothing** scored, **nothing is selected** and
  the runner raises `Step 2 Selection Failed` — spending the Step-3 budget on an
  unranked pool is the failure this pass prevents.
* Deliverables: the selected N go to
  `MORNING_ANTHEM_TREND_KEYWORDS_PATH` (Step 3's intake, so its credit envelope
  is exactly that list); the full gated pool goes to `…-gated.json` as the
  **re-run reserve** — it already cost its HasData credits, so a different
  `--target` can be tried without paying for trends again.

**Pilot evidence (2026-10-12, live, 3 Jev calls, no HasData credits)** over the
13-candidate pool: gate kept 12; selection scored them 4.95 (`hand held milk
frother`), 4.69 (`matcha whisk`), 4.51, 4.00, 3.67, 3.67, 3.20, 3.10, 3.08,
2.80, 2.24, 1.56 (`breville barista express`). Two findings:

1. **The brand-query problem is solved here, not by a blacklist.** The brand and
   comparison strings were not merely outscored for the top N — they scored at
   the *bottom of the pool* (`breville barista express` lowest of all twelve),
   because a brand-plus-model query is a poor sourcing keyword. No brand rule is
   needed.
2. **The pillar floor works**: three low-scoring rows from three pillars entered
   on the floor while four higher-scoring `brew_fuel` rows stayed in reserve.

#### Live pilot evidence (2026-10-12) — read-only, 20 HasData credits

Two live Trends calls (10 credits each, the observed rate) then two Jev
batches over 13 curated candidates:

| Finding | Detail |
|---|---|
| Gate verdicts | **13/13 classified `product_search`**; the banned-token screen then dropped `milk frother kmart` → **12 kept** |
| Demand is dominated by one pillar | `milk frother` 5-y AU mean **61** vs `incense burner` 5, `silk eye mask` 3, `garlic press` 3 |
| One seed had no usable AU demand | `commuter backpack` — 16/262 weekly points had data, max 5, no mean reported. **`sunrise_move` still has no demand signal** and needs its own seeds |
| Related queries are brand-heavy | of 50 AU related queries for `milk frother`, roughly half name a manufacturer or retailer (nespresso, breville, delonghi, smeg, kmart, aldi, big w, ikea) — they pass the intent gate legitimately, which is why the banned-token screen and the curator carry the load |
| Yield ratio | 5 input terms + 50 related queries → 13 curated candidates → 12 kept |
| **Second AU call (same day, 10 credits): the `sunrise_move` pillar is not trend-dead, its seed was too narrow** | Head terms carry real AU demand — `beach towel` 5-y mean 25 at peak 100, `dog leash` 16 — while phrase-level product names do not (`sand free towel` 2, `commuter backpack` peak 2, `hands free dog leash` 8/262 weeks). Google Trends reports head terms, not long-tail phrasings, and AU is a small market, so a specific phrase falls under the reporting floor. **Seed Trends with head terms; the long-tail phrasings belong downstream as Step-4 supplier keywords.** |

#### Step 3 — the HasData scrape, the curator and the Jev pillar gate

Three stages, in this order, and the order is the design:

1. **Scrape** (`src/extractors/google_shopping_hasdata.py`) — one HasData Google
   Shopping request per selected keyword, 10 credits each, pinned to the AU
   market (`SHOPPING_GL`/`SHOPPING_DOMAIN`). One request returns the whole grid
   (~65 rows); `HASDATA_GS_MAX_RESULTS_PER_KEYWORD` (20) bounds curation only,
   never spend. The runner refuses to start above `HASDATA_GS_MAX_KEYWORDS` (30)
   or `HASDATA_PRODUCTION_CAP` (400).
2. **Curate** (`src/evaluators/gold_curator.py`) — the reasoning LLM **selects
   rows by verbatim `url`** and writes only `compliance_note` /
   `unit_economics_note`. Rows with neither a rating nor a review count are
   filtered out before the LLM, so a gold product always carries on-page demand
   evidence. Every product fact is code-assembled from the row; an invented or
   edited url is dropped at the code-side join.
3. **Pillar gate** (`src/research/pillar_gate.py`) — Jev classifies each curated
   product into exactly one of the five pillars, or `discard`s it. **The gate is
   the pillar's single author**, which is why the curator no longer assigns one.

**The gate runs last on purpose (operator decision 2026-10-12).** A decision
model is only as good as its context, and a pillar is not an honest question
without commercial context — "is this a Restful Reset product?" reads
differently when the row carries a price and 4.7/5 from 1,200 reviews than when it
carries a bare title. So the state hands it the name, description, **the price
as both its display string and its number**, the rating, the review count, the
demand-evidence line and the source keyword, plus the five pillar descriptions.
The numeric `price_value` needed a new field on `ShoppingRow`, taken from the
payload's own `extractedPrice` with a defensive parse of the display string
falling back behind it — `None` rather than a fabricated zero.

The curator's own pillar guess is deliberately **not** passed to the gate: it
would bias the classification the gate exists to make. (A stray `pillar` in a
curation response is ignored rather than fatal — unlike
`ProvisionalProductEvaluation`, where an undeclared field would be a *lost fact*
rather than an unauthorised opinion.)

**Failure containment**: a failed batch retries once, then its products are
dropped with a note; one `discard` is a normal verdict. If the gate keeps
**nothing**, the runner halts *before writing anything* — an empty gold list
would leave Steps 4–6 reading a file that looks like a result.

**Tests**: `tests/test_pillar_gate.py` (20 tests) covers the verdict set, the
enriched state, the batching, the join, the retry containment, verbatim
carry-through, `attach_pillars`, and the runner's wiring and zero-survivor halt.

#### Step 4 — the keyword bank, exact title first

Turns the gold list into the supplier search strings Step 5 feeds to
CJdropshipping. **The strategy is exact-title first**, and it moves as much of the
work as possible back into code:

1. **The primary keyword is the product's own title**, produced by
   `exact_keyword()` — a deterministic transform (lowercase, apostrophes removed,
   every other run of non-alphanumerics collapsed to one space). Copying a string
   is not a job for a model, and a code-derived string cannot hallucinate. That
   exact string is the highest-precision query there is for the identical product.
2. **The model's only job is the optional fallback**: one tightly-specific variant
   of that title for a product whose exact title finds nothing — the same object
   named through its mechanics, material, form factor or size. At most one per
   product, and plenty of products get none; the prompt says an empty list is a
   valid and common answer.

So the bank is roughly one row per product plus the fallbacks that earned their
place, and every row is typed `match ∈ {exact, tight_fallback}`.

**Validation splits by who could have caused the problem** (`validate_bank`
returns `BankProblem`s carrying the row index and a `droppable` flag):

* **Code invariants** — an exact keyword that does not match its product's title,
  a product with no exact row, a row naming an unknown product or another
  product's pillar, a duplicate that reached validation — **fail the run**,
  because a code invariant broke and nothing is written.
* **Model slips** — a fallback that is too long or short, punctuated or
  capitalised, carries a banned token, or shares no concrete word with its own
  title (the cheap code-side proxy for "specific to THIS product") — cost **their
  own row**, and the bank is re-validated without them. The product still has its
  exact keyword, so one bad fallback must not void a run.
* A model row that names an unknown product, or claims `match="exact"`, is
  ignored on arrival: the model does not get to decide either.

**What this deletes:** the 50–70 per-pool band, the chunked pools, the adaptive
per-product target, the merged bank band, the broad/modifier pairing and its
`tightens` links. All of them existed to spread a *generated* pool across
products it could not otherwise name; with the title as the primary keyword,
uniqueness is free and coverage is one row per product.

#### Step 5 — CJ-only ingestion, product-major

Every bank **product** runs through the CJdropshipping MCP pipeline — the sole
supplier engine — under the full gate stack: the commercial gate (§6.2), the MCP
payload liveness gate (§5), the mandatory freight quote to
`CJ_DEFAULT_DEST_COUNTRY` (§6.3), the LLM viability gate, the AUD margin floor
(`MIN_MARGIN_AUD`) and the 3-image gallery gate. Survivors are written to
`<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/`.

**The unit of ingestion is the product, not the keyword** (operator decision
2026-10-12). A product's rows run in order — its **exact title first**, then its
**fallback only to fill whatever target slots the exact keyword left open** — so
a fallback is never paid for when the exact title already worked. Each engine
gets its own pass at a product's keywords, since they write to separate supplier
folders.

Because the unit changed, the config key did too: `BANK_TARGET_PER_PRODUCT`
(formerly `..._PER_KEYWORD`) is the number of ACCEPTed packages one product may
contribute through one engine, and the summary reports
`products_attempted` / `products_empty` with a per-engine `keywords_skipped`
counter for the fallbacks that were not needed.

#### Step 6 — Jev ranking (the gold kernel's last automated step)

Reads the packages from `<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/`
plus the Step-3 gold list, and ranks each package with Jev on three questions:
`<slug>__similarity` (score — is it the same object as a gold product?),
`<slug>__winning_value` (score — its own **AUD** retail, landed cost and
projected margin weighed against the gold reference's demand evidence) and
`<slug>__pillar` (choice — one of the five pillars, or `discard`). Tiers:
`rank_score = 0.6·similarity + 0.4·value` ≥ `JEV_SHORTLIST_MIN_SCORE` (3.5) →
shortlist, ≥ `JEV_REVIEW_MIN_SCORE` (2.5) → review, else disregard.

**Ranking is a report, not a deletion**: no package is moved or removed, and Jev
authors no product fact — every field in the report is copied verbatim from the
package's `metadata.json`. A post-evaluation compliance gate then forces any
package whose own text carries a banned token to `disregard` regardless of score.
Deliverable: `outputs/json/morning-anthem-step-6-ranked-candidates.json` (+ `.md`).

#### The launch run (one-off, operator decision 2026-10-12)

A single full run that buys the whole gold list at once, because the CJ side
costs no credits and only the research does:

| Stage | Shape | Credits |
|---|---|---|
| Step 2 — Trends | ≤ 10 calls (up to 5 terms each) | ≤ 100 |
| Step 3 — Google Shopping | **30 keywords** × 10 | **300** |
| **Total** | | **≤ 400** |

`HASDATA_PRODUCTION_CAP=400` and `HASDATA_GS_MAX_KEYWORDS=30` encode this, so
the two halves sum to the cap exactly and ~600 credits of the monthly tier stay
in reserve for the per-step pilots and reruns. Step 2's research yields ~60
usable strings; the operator **hand-picks 30** (pillar-balanced, demand-ranked,
brand/retailer strings excluded) and the other ~30 stay as the re-run reserve.
Expected yield **~55 gold products** (~2 per keyword — the ratio the pipeline's
own runs have produced); those become the reference set Steps 4–6 chase, with
the Step-4 bank targeting each by exact title first and Step 5 looping over CJ
(free in credits, bounded by pacing) to find the identical item or a close
equivalent.

#### Open items

1. ~~Confirm the Trends credit rate~~ — **RESOLVED 2026-10-12: 10 credits per
   request**, confirmed on the HasData dashboard. The launch-run envelope of 400
   stands as written.
2. **Step 4's exact-title-first strategy is not built** — the bank still emits
   broad/modifier pairs.
3. ~~`sunrise_move` seeds~~ — **RESOLVED 2026-10-12** by a second live Trends
   call (10 credits): the pillar is not trend-dead, its *head terms* carry real
   AU demand (`beach towel` 5-y mean 25 at peak 100, `dog leash` 16) while its
   phrase-level product names do not (`sand free towel` 2, `commuter backpack`
   peak 2, `hands free dog leash` 8/262 weeks). **Seed Trends with head terms,
   not phrase-level product names** — the long-tail phrasings belong downstream
   as Step-4 supplier keywords, once demand is proven.
4. **`TREND_SELECT_TARGET` is the Step-3 budget.** 30 keywords = 300 credits;
   changing it changes the scrape size, not just the list.
5. **The production Step-2 research pass** (≤ 10 Trends calls) has not been run
   — the pilot pool is 13 candidates, so the selection has only been exercised
   on a small pool (a `--target` wider than the pool selects everything).

