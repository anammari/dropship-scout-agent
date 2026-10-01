# Dropship Scout Agent

An autonomous eCommerce intelligence pipeline that discovers, validates, and
packages winning dropshipping product candidates for an Australian Shopify
store. The agent ingests **live, verified products directly from supplier
APIs** (Supplier-First architecture), filters every candidate through an
LLM-enforced **5-point commercial gate**, downloads validated product
photography deterministically from the supplier's own CDN gallery, and writes
complete, sourcing-ready product packages into the Shopify store workspace.

```
[Supplier extractors — SUPPLIER_PRIORITY_ORDER chain]
  CJdropshipping MCP → AliExpress Dropshipping Center
            │  (CJ hits must pass the MCP Payload Liveness Gate)
            ▼
┌──────────────────────────────────────┐
│ 1. EXTRACTION — live supplier APIs   │
│    every candidate starts as a real  │
│    supplier listing: real URL, real  │
│    listed price, real gallery        │
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 2. EVALUATION — LLM 5-point gate via │
│    structured (Pydantic/instructor)  │
│    output; margin math in code       │
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 3. IMAGE SOURCING — deterministic,   │
│    single source: the supplier's CDN │
│    gallery; pixel-validated, zero    │
│    LLM input                         │
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 4. EXPORT — direct to the Shopify    │
│    workspace: product-NN/ with       │
│    metadata.json + validated images  │
└──────────────────────────────────────┘
```

Two intake paths share this core: the **general intake** (§2.1–2.2), one
keyword at a time into `dropship-candidates/`, and the **updated multi-step
gold-kernel pipeline** (§2.3–2.6, Steps 3–6), which researches proven AU retail
demand, generates a supplier keyword bank, ingests it through both suppliers,
and ranks the result with Jev.

**Key design contracts** (enforced by schema validators, not prompt wording):

- **Candidates are real before the LLM sees them.** Every candidate is a
  `RawSupplierProduct`: a live supplier product-detail URL, a listed price
  converted to AUD, and ≥ 3 gallery URLs — or it never enters the pipeline.
- **The LLM never touches images, supplier data, logistics, or costs.** No
  image-URL field exists on any evaluation model; `supplier_name` /
  `supplier_retail_url` / `estimated_cogs_aud` / `cogs_estimation_basis` are
  mapped programmatically from the verified raw product, and
  `shipping_notice_au` is derived from the supplier's own freight quote.
- **Every supplier URL must be a direct product page** — search/category/
  gateway URLs are rejected at the schema level.
- **LLM arithmetic is never trusted** — margins and markups are recomputed in
  code, and an ACCEPT that misses the margin floor (markup ≥ 2.5 OR margin
  > AUD 10) is downgraded to REJECT.
- **Shipping is quoted, never assumed** — a CJ product carries its real freight
  cost to the target country, or it is dropped. Where a supplier cannot quote
  freight, the evaluator is told the landed cost is a floor rather than a
  verified figure, and the customer-facing shipping line asserts no service or
  transit window.
- **No incomplete packages** — a candidate with fewer than 3 validated images
  is dropped entirely; an `images/`-less directory is never written.
- **Ranking is a report, not a deletion** — Step 6 ranks the gold kernel against
  the gold products and writes a tiered shortlist/review/disregard report. It
  never moves or removes a package, and it cannot author or edit a product fact:
  Jev only scores similarity and winning value and picks a pillar.

---

## 1. Prerequisites & Environment Setup

### 1.1 System requirements

- **macOS** (Apple Silicon — `darwin-arm64`)
- **Python 3.13** via Homebrew (`brew install python@3.13`; the project requires
  ≥ 3.11, and 3.13 is the version the shipped `.venv` was built with)
- An **Ollama Cloud** account (the LLM evaluation endpoint)
- A supplier credential: `CJ_MCP_TOKEN`. The AliExpress Dropshipping Center
  engine needs no credential — it reads the DS Center's own APIs anonymously.
- Research credentials for the updated multi-step pipeline: `APIFY_TOKEN` for
  the Step-3 Google Shopping scrape, `HASDATA_API_KEY` for Step-2 trend
  research, and `OPENROUTER_API_KEY` for the Step-6 Jev ranking. All three are
  optional for the general intake (§2.1) — only the Steps that use them need
  them.
- Internet access to `cjdropshipping.com`, `aliexpress.com`, `ollama.com`,
  `apify.com`, `openrouter.ai`, and supplier CDN hosts (`cdn.alibabaimg.com`,
  etc.)

### 1.2 Create and activate the virtual environment

```bash
cd /Users/ahmadammari/PD/dropship-scout-agent

# Create the venv with Homebrew's Python 3.13
/opt/homebrew/bin/python3.13 -m venv .venv

# Activate it (zsh)
source .venv/bin/activate
```

All commands below assume the venv is active. Alternatively, invoke the venv's
interpreter directly (`.venv/bin/python ...`) without activating.

### 1.3 Install dependencies

```bash
python -m pip install -e ".[dev]"
```

Dependencies are pinned to the versions verified working together (see
`pyproject.toml`); notably `playwright-stealth` must stay **≥ 2.0** (the v1
`stealth_async()` API no longer exists).

### 1.4 Install the Chromium browser binary

```bash
python -m playwright install chromium
```

Chromium is used only by the AliExpress Dropshipping Center engine (its MTOP
calls and its per-item PDP gallery harvest) — the CJ (MCP) engine never opens
a browser.

### 1.5 Configure `.env`

Copy the template and fill in your credentials:

```bash
cp .env.example .env
```

`.env.example` is the authoritative template — the block below mirrors it, in
the same order, with the same comment state. A key that is **commented out or
present but blank** resolves to its default in `src/config.py`, so you only
need to uncomment what you want to override.

```ini
# .env — runtime configuration (NEVER commit this file)

# --- Supplier extractors (tried in SUPPLIER_PRIORITY_ORDER) ---

# CJdropshipping MCP token (primary engine). Generate it on CJ's API
# Authorization page; the client appends it to the remote MCP endpoint as a
# path segment (never logged).
CJ_MCP_TOKEN="YOUR_COPIED_MCP_TOKEN"
# Remote MCP endpoint WITHOUT the token (the token is appended at connect time)
#CJ_MCP_BASE_URL="https://developers.cjdropshipping.com/mcp"

# AliExpress Dropshipping Center — no credential needed; the DS Center answers
# these calls anonymously. The saved login below is optional and is injected
# only when the file exists. Refresh it with:
# python scripts/generate_ali_session.py
#ALI_DS_STATE_PATH="ali_ds_state.json"
#ALI_DS_MAX_PRODUCTS="20"
#MIN_DS_ORDER_COUNT="500"
#MIN_DS_RATING="4.5"

# --- Extractor chain & pricing ---
#SUPPLIER_PRIORITY_ORDER="cjdropshipping,aliexpress"
#USD_TO_AUD="1.55"
#MIN_CJ_LISTED_COUNT="150"
#CJ_FREIGHT_METHOD="CJPacket Eub"
#CJ_MAX_PRODUCTS="10"

# Margin floor (deterministic half of evaluation gate 2)
#MIN_MARKUP_MULTIPLIER="2.5"
#MIN_MARGIN_AUD="10.0"

#TARGET_COUNTRY="AU"

# --- LLM evaluation (Ollama Cloud, OpenAI-compatible) ---
LLM_BASE_URL="https://ollama.com/v1"
LLM_API_KEY="your_ollama_cloud_api_token"
LLM_MODEL="deepseek-v4-flash:cloud"

# --- Trend research (keyword brainstorming; MCP servers) ---
# HasData is consumed by the project MCP config (.mcp.json) via
# scripts/mcp_headers.py, not by src/config.py; APIFY_TOKEN is also read by
# src/config.py for the Step-3 scrape.
HASDATA_API_KEY="YOUR_HASDATA_API_KEY"
APIFY_TOKEN="YOUR_APIFY_API_TOKEN"

# --- Step 3 gold-standard research (Apify Google Shopping actor) ---
#APIFY_GS_ACTOR=""
#APIFY_GS_MAX_RESULTS_PER_KEYWORD="10"
#APIFY_GS_MAX_CHARGE_USD="7.5"
#GOLD_PRODUCTS_PATH="outputs/json/step-3-gold-standard-products.json"

# --- Step 4 gold-standard keyword bank ---
#KEYWORD_BANK_PATH="outputs/json/step-4-gold-keywords.json"

# --- Step 5 dual-supplier ingestion (CJ MCP + AliExpress DS Center) ---
#BANK_TARGET_PER_KEYWORD="2"
#OPTIMAL_EXPORT_DIR=""

# --- Export destination (Shopify workspace) ---
#EXPORT_DIR="/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates"

# --- Misc ---
#USER_AGENT=""

# --- Step 6: Jev product ranking (updated multi-step pipeline) ---
# OpenRouter API key for the System One endpoint (model typesafe/jev-1.13).
# Sent as a Bearer token; never logged.
OPENROUTER_API_KEY="YOUR_OPENROUTER_API_KEY"
#OPENROUTER_BASE_URL=""
#JEV_MODEL=""
#JEV_BATCH_SIZE="4"
#JEV_SHORTLIST_MIN_SCORE="3.5"
#JEV_REVIEW_MIN_SCORE="2.5"
```

| Variable | Required | Purpose |
|---|---|---|
| `CJ_MCP_TOKEN` | for the CJ engine | CJdropshipping MCP token (primary supplier; token in URL path, never logged) |
| `CJ_MCP_BASE_URL` | — | MCP endpoint without the token (defaults to `https://developers.cjdropshipping.com/mcp`) |
| `LLM_BASE_URL` | ✅ | OpenAI-compatible chat-completions endpoint (Ollama Cloud: `https://ollama.com/v1`) |
| `LLM_API_KEY` | ✅ | Ollama Cloud API token |
| `LLM_MODEL` | — | Defaults to `deepseek-v4-flash:cloud` |
| `HASDATA_API_KEY` | for trend research | HasData Google Trends MCP key — the default trend source for Step 2 keyword brainstorming. Read from `.env` by `scripts/mcp_headers.py` for the `.mcp.json` servers, not by `src/config.py` |
| `ALI_DS_STATE_PATH` | — | Optional saved AliExpress login (`storage_state`), injected only when the file exists; the DS Center answers anonymously, so it is never required. Refresh with `python scripts/generate_ali_session.py` |
| `ALI_DS_MAX_PRODUCTS` | — | DS Center search page size / per-keyword expansion cap (default `20`) |
| `MIN_DS_ORDER_COUNT` | — | AliExpress winning-product gate: minimum historical orders (default `500`) |
| `MIN_DS_RATING` | — | AliExpress winning-product gate: minimum rating out of 5 (default `4.5`) |
| `SUPPLIER_PRIORITY_ORDER` | — | Comma-separated chain order (default `cjdropshipping,aliexpress`) |
| `USD_TO_AUD` | — | USD→AUD rate for all price math (default `1.55`) |
| `MIN_CJ_LISTED_COUNT` | — | CJ commercial gate: minimum dropshipper listing count (`listedNum`). CJ publishes no historical-sales figure on any tool, so this is the gate's only metric (default `150`) |
| `CJ_FREIGHT_METHOD` | — | Pin the shipping service the CJ landed cost is based on, by CJ's own name (e.g. `CJPacket Eub`). Blank takes the cheapest method the quote offers (default) |
| `CJ_MAX_PRODUCTS` | — | CJ products expanded (detail + gallery) per keyword (default `10`) |
| `MIN_MARKUP_MULTIPLIER` | — | Margin floor, markup leg: an ACCEPT must clear this **or** `MIN_MARGIN_AUD` against the real landed cost (default `2.5`) |
| `MIN_MARGIN_AUD` | — | Margin floor, gross-profit leg, in AUD per unit (default `10.0`, widened from 20.0 to match the Step-3 $10 target) |
| `TARGET_COUNTRY` | — | Extraction/evaluation target (default `AU`); also the AliExpress DS Center's ship-to market, which decides both its catalogue and its quoted price. CJ's MCP search `countryCode` is pinned to the China warehouse (`CN`) instead |
| `APIFY_TOKEN` | for Step 3/2 research | Apify token shared by the Google Trends fallback actor (Step 2, via `scripts/mcp_headers.py`) and the Step-3 gold-research actor (`apify-client`) |
| `APIFY_GS_ACTOR` | — | Step-3 gold-research actor id (default `damilo/google-shopping-apify`) |
| `APIFY_GS_MAX_RESULTS_PER_KEYWORD` | — | Step-3 results requested per keyword; the actor's closed set is 10/20/30/40/50/100 (default `10`) |
| `APIFY_GS_MAX_CHARGE_USD` | — | Step-3 hard USD spend ceiling per run, enforced by Apify itself (default `7.5`) |
| `GOLD_PRODUCTS_PATH` | — | Step-3 gold-product deliverable Steps 4 and 6 read (default `outputs/json/step-3-gold-standard-products.json`) |
| `KEYWORD_BANK_PATH` | — | Step-4 deliverable — the Step-5 keyword bank (default `outputs/json/step-4-gold-keywords.json`) |
| `BANK_TARGET_PER_KEYWORD` | — | Step-5 packages exported per **(keyword, engine)** leg — the bank's keyword count multiplies it (default `2`) |
| `OPTIMAL_EXPORT_DIR` | — | Step-5 gold-kernel destination root; each engine writes into its own subfolder (default `…/my-store-build/inspiration/optimal-dropship-candidates`) |
| `EXPORT_DIR` | — | Destination workspace for the general intake §2.1 (defaults to the Shopify path below) |
| `USER_AGENT` | — | Desktop UA used by CDN downloads and the Playwright PDP gallery harvest |
| `OPENROUTER_API_KEY` | for Step 6 | OpenRouter key for the System One endpoint (model `typesafe/jev-1.13`); sent as a Bearer token, never logged |
| `OPENROUTER_BASE_URL` | — | OpenRouter API base; the client appends `/systemone` (default `https://openrouter.ai/api/v1`) |
| `JEV_MODEL` | — | System One model id, pinned so a ranking is reproducible (default `typesafe/jev-1.13`) |
| `JEV_BATCH_SIZE` | — | Packages per System One call, three questions each (default `4`, matching the documented ~13-questions-per-call envelope) |
| `JEV_SHORTLIST_MIN_SCORE` | — | Tier floor: `rank_score` at or above this is shortlist (default `3.5`, on the 1–5 scale; widened from 4.0) |
| `JEV_REVIEW_MIN_SCORE` | — | Tier floor: `rank_score` at or above this is review, else disregard (default `2.5`) |

**Security:** secrets from `.env` are never printed or logged by the agent
(`Settings` holds a no-leak repr). Keep `.env` out of version control (it is
already covered by `.gitignore`).

---

## 2. Functional Components & How to Run Them

### 2.1 Supplier-first intake — the general pipeline

```bash
python -m src.main --keyword "desk organizer" --target-count 3 --country AU
```

| Option | Default | Meaning |
|---|---|---|
| `--keyword` | `desk organizer` | Seed niche keyword for supplier searches |
| `--target-count` | `3` | Stop as soon as this many ACCEPTed products are exported |
| `--country` | `AU` | Extraction target market |
| `--extractor` | `auto` | Force one engine: `auto`, `cjdropshipping`, or `aliexpress` |

The extractor chain follows `SUPPLIER_PRIORITY_ORDER`; a chain engine that is
unconfigured, blocked or timing out falls through to the next, and `--extractor
<key>` forces one. Products are evaluated in scrape order until `target_count`
ACCEPTs are exported.

```bash
# Standard validated intake of 3 winning products
python -m src.main --keyword "desk organizer" --target-count 3

# Force a single engine
python -m src.main --keyword "kitchen gadgets" --target-count 3 --extractor cjdropshipping
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Target count reached (`[PIPELINE COMPLETE]`), or fewer packages landed but ≥ 1 (`[PARTIAL]` note printed) |
| `1` | Human intervention required: all extractors unavailable/blocked, LLM config missing, or the funnel was exhausted with **zero** exports |
| `2` | Unexpected error (traceback logged) |

The LLM filter is constructed before extraction, so a misconfigured `.env`
fails immediately without burning supplier quota.

### 2.2 Supplier extractors

All extractors subclass
`BaseSupplierExtractor.fetch_products(keywords, country) -> List[RawSupplierProduct]`
and share one exception taxonomy: `ExtractorNotConfiguredError` (missing
credential — the chain moves on), `ExtractorBlockedException` (auth/rate-limit/
anti-bot wall — the chain moves on), and `ExtractorTimeoutException`. Every
extractor **skips, never fabricates**, hits that cannot yield a fully-formed
product (real PDP URL, usable price, ≥ 3 gallery URLs).

**CJdropshipping (`src/extractors/cj_mcp_extractor.py`, primary).** Connects to
the official CJ MCP server over StreamableHTTP, discovers the tool catalog, and
per keyword runs the product-search tool with the China-warehouse mapping and
the inventory filter. Hits then pass, in order:

1. **The CJ commercial gate** — `listedNum` must clear `MIN_CJ_LISTED_COUNT`.
   CJ publishes no historical-sales figure on any tool, so the dropshipper
   listing count is the only demand proof available; a hit reporting no count
   is dropped as unproven. Survivors are ranked by listing count descending
   before any detail call, so the most widely listed products meet the LLM
   first.
2. **The MCP Payload Liveness Gate** — the payload must explicitly confirm an
   *active* listing **and** positive stock, checked once on the search hit and
   again on the merged search-hit + product-detail payload. "We couldn't tell"
   is a drop, not a maybe. Because CJ fronts every product page with a
   Cloudflare Turnstile wall for automated clients, the MCP stream is the sole
   arbiter of liveness on this path — no HTML is fetched and no bot challenge
   is answered.
3. **The freight quote** — `calculate_freight` for the cheapest variant, with a
   weight-based fallback, so every emitted product carries a real shipping cost
   to the target country. A listing whose freight cannot be quoted is dropped
   rather than costed at zero.

The gallery comes from `productImageSet`, the description is HTML-stripped, and
`price_aud = listed USD × USD_TO_AUD`.

**AliExpress Dropshipping Center (`src/extractors/aliexpress_ds.py`, native).**
Reads the DS Center's own MTOP H5 APIs through a stealth Playwright context's
request jar — the same internal calls its UI makes, so no HTML scraping and no
per-result billing. Both calls ride MTOP's token-then-sign handshake, and the
context carries AliExpress's ship-to cookie pinned to `TARGET_COUNTRY`, because
the DS Center's catalogue and prices are market-specific. The record's
minor-unit price plus its quoted currency drive `price_aud`; any other currency
is skipped rather than mispriced. Every hit must then clear the
**winning-product gate** — `MIN_DS_ORDER_COUNT` historical orders and
`MIN_DS_RATING` — where a metric the DS Center does not report counts as
unproven and drops the item. Survivors are sorted by order volume descending,
and each survivor's PDP is harvested once for its gallery and description.

### 2.3 Step 3 — gold-standard product research

Builds the gold list that Steps 4–6 measure against: AU retail products with
proven demand, scraped from live Google Shopping AU and curated by the
reasoning LLM.

```bash
source .venv/bin/activate && python scripts/run_gold_standard_research.py \
    [--keywords outputs/json/step-2-search-keywords.json] \
    [--limit 2] [--num 10] [--dump-raw /tmp/step3_raw_rows.json] \
    [--from-raw /tmp/step3_raw_rows.json] \
    [--output outputs/json/step-3-gold-standard-products.json]
```

- **Scrape** (`src/extractors/google_shopping.py`): ONE batched run of the
  `damilo/google-shopping-apify` actor (pay-per-result) carries every keyword,
  `country="au"`, `max_pages=1`. Spend is governed three ways: the actor's
  closed-set `num` is validated before any money moves; a hard
  `max_total_charge_usd` ceiling rides the run options and is enforced by Apify
  itself; `--limit N` pilots on the first N keywords. It is extractor-shaped but
  deliberately **not** a `BaseSupplierExtractor` — these are marketplace retail
  listings with no supplier PDP, freight quote or gallery, so they can never
  become `RawSupplierProduct` without fabricating sourcing fields.
- **Curate** (`src/evaluators/gold_curator.py`): the LLM only **selects by
  verbatim `url`** and annotates pillar/compliance/economics. Every product fact
  is **code-assembled from the scraped rows**, so a hallucinated or edited url
  can never become a product. Rows carrying no rating/review KPI are filtered
  out before the LLM — a gold product must carry on-page demand evidence.
- **Deliverable**: `GOLD_PRODUCTS_PATH` (+ a `.md` digest) in the untracked
  `outputs/` tree. The runner prints the planned spend envelope before the first
  call and exits non-zero when nothing usable comes back; `--from-raw` replays
  curation over a prior dump at **zero Apify spend** (the debugging path).

### 2.4 Step 4 — gold-standard keyword bank

Turns the Step-3 gold list into the **supplier search keyword bank** Step 5
ingests — generated **in pool chunks**: the table is capped at 150 eligible
products (`DEFAULT_MAX_PRODUCTS`, raiseable/lowerable via `--max-products`)
and each `--chunk-size` 30 slice generates ONE pool validated under the
single-pool rules, so the default run writes ~300 keywords. The reasoning LLM
authors the keywords; every structural rule is re-checked in code
(broad/modifier pairing, pillar membership, the AICIS banned-token boundary,
the 50–70 band per pool, the merged bank band around the chunks' summed
target — 250–320 at the default sizing — and per-product coverage and
floor), and a pool that breaks any rule exits non-zero with nothing written.

```bash
source .venv/bin/activate && python scripts/generate_gold_keywords.py \
    [--gold-products outputs/json/step-3-gold-standard-products.json] \
    [--batch-size 4] [--max-products 150] [--chunk-size 30] \
    [--output outputs/json/step-4-gold-keywords.json] [--markdown …]
```

- **The prompt carries no products.** `src/keywords/gold_keyword_prompt.md` has a
  literal `{PRODUCT_TABLE}` slot filled at runtime from the live Step-3
  deliverable, so the gold products are ground truth the prompt never hardcodes.
  Each chunk's table is exactly the slice it covers, so one prompt serves all
  pools.
- **Selection is bounded, type-diverse and deterministic.** The gold deliverable
  can hold far more products than any single 50–70 pool can cover, so the table
  is capped at `DEFAULT_MAX_PRODUCTS` (`--max-products`), ranked by demand,
  collapsed to the strongest row per duplicate name, collapsed to **one product
  per Step-2 source keyword**, then round-robined across pillars so a capped
  table still covers every pillar.
- **The one-per-source-keyword collapse is load-bearing.** The research is
  keyword-driven, so the strongest rows by raw demand cluster into a few
  distinct product types. Every table product must be named by its *own*
  keywords, a duplicate keyword string is fatal and brand names are banned — so
  several garlic presses cannot each own two honest keywords. One row per source
  keyword spends the table on distinct products instead.
- **Uniqueness is enforced at three levels.** `validate_pool` treats a
  duplicate keyword as fatal; within a pool, the neediest-first dedupe salvages
  a contested string; and across pools a repeat is **fail-closed** — every
  chunk's call is handed the strings earlier chunks claimed as an explicit
  do-not-repeat list, and a chunk that re-writes one anyway fails the run
  (a post-validation silent dedupe would void the earlier pool's validated
  per-product floor).
- **Deliverable**: `KEYWORD_BANK_PATH` (+ a `.md` digest with modifiers nested
  under their broad term) in the untracked `outputs/` tree — Step 5's intake.
  The runner prints the bank plan (chunk sizes, targets, band and the LLM-call
  estimate) before the first call.

### 2.5 Step 5 — dual-supplier ingestion (the gold kernel)

Ingests **every** Step-4 keyword through **both** supplier pipelines and exports
what survives into the gold-kernel tree:

```
<OPTIMAL_EXPORT_DIR>/cjdropshipping/product-NN/{metadata.json, images/}
<OPTIMAL_EXPORT_DIR>/aliexpress/product-NN/{metadata.json, images/}
```

**This is not the general pipeline's fallback chain.** `--extractor auto` stops
at the first engine that answers; Step 5 needs *both* engines to run for *every*
keyword, because the point of the step is to obtain the optimal product from
each supplier and let Step 6 rank them against one another. So each
(keyword, engine) *leg* is its own call into the existing `run_pipeline` with a
single-engine list — **no supplier gate is forked or relaxed**. Every leg still
runs CJ's commercial gate, the MCP liveness gate and the mandatory freight
quote; the DS Center's winning-product gate with its AU market pin; and, for
both, the LLM viability gate, the margin floor and the 3-image gallery gate.

Failure is **per-leg, not per-run**: an engine that is unconfigured, blocked or
timed out is skipped for that leg only while the other engine still runs the
keyword; only when *every* registered engine failed *every* keyword does
ingestion raise, and the runner renders the intervention block. The two supplier
folders are numbered independently and each dedupes only against itself, so the
same product landing from both suppliers is exported twice — intentionally, as
two fulfilment options.

```bash
source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
    [--keywords outputs/json/step-4-gold-keywords.json] \
    [--target-per-keyword 2] [--limit 4] \
    [--only {both,cjdropshipping,aliexpress}] [--export-root <dir>]
```

| Option | Default | Meaning |
|---|---|---|
| `--keywords` | `KEYWORD_BANK_PATH` | Step-4 bank to ingest |
| `--target-per-keyword` | `BANK_TARGET_PER_KEYWORD` (`2`) | Packages per **(keyword, engine)** leg — the bank's keyword count multiplies it |
| `--limit` | *(all)* | Ingest only the first N bank keywords (pilot runs) |
| `--only` | `both` | Restrict to one engine (single-leg pilot) |
| `--export-root` | `OPTIMAL_EXPORT_DIR` | Gold-kernel intake root |

```bash
# Pilot: 4 keywords, one package per leg
python scripts/ingest_keyword_bank.py --limit 4 --target-per-keyword 1

# Single-leg pilot, to isolate one supplier
python scripts/ingest_keyword_bank.py --limit 1 --only cjdropshipping

# Full run over the whole bank
python scripts/ingest_keyword_bank.py
```

The runner prints the pilot banner (keyword count × engines × per-leg target,
plus the pacing note) before the first call, then the per-leg progress lines and
the folded funnel summary per engine with the supplier's export root and every
exported package directory. Zero exports overall → intervention block and exit 1;
fewer packages than the target → `[STEP 5 COMPLETE]` and exit 0.

**Pacing is the point of `--limit` and `--only`.** One CJ keyword costs up to
`CJ_MAX_PRODUCTS` product-detail calls plus a freight quote each, all under CJ's
MCP rate limits; one DS Center keyword costs a search page, a per-item record
round trip for every hit, and a PDP harvest per survivor. A full bank across
both engines is a long run — that is the pacing, not a hang. The two supplier
trees are production deliverables, not repo artefacts, and stay untracked.

### 2.6 Step 6 — Jev product ranking (the gold kernel's last automated step)

Ranks **every** Step-5 package against the Step-3 gold-standard product list with
Jev (TypeSafe **System One** via OpenRouter) and writes a tiered report:

```
outputs/json/step-6-ranked-candidates.json   # the structured ranking
outputs/md/step-6-ranked-candidates.md     # the tier-grouped digest
```

```bash
source .venv/bin/activate && python scripts/rank_optimal_candidates.py \
    [--gold-products outputs/json/step-3-gold-standard-products.json] \
    [--export-root <OPTIMAL_EXPORT_DIR>] [--batch-size 4] \
    [--output outputs/json/step-6-ranked-candidates.json]
```

- **Intake is both supplier trees plus the gold list.** Every
  `product-NN` under `<OPTIMAL_EXPORT_DIR>/{cjdropshipping,aliexpress}/` is
  ranked; nothing is filtered on the way in. The Step-3 gold products are the
  reference set the ranking measures against.
- **Three questions per package, one batch per call.** Each System One call
  carries a single shared state — the gold reference plus `JEV_BATCH_SIZE`
  products — with `<slug>__similarity` and `<slug>__winning_value` (score) and
  `<slug>__pillar` (choice) per product. One state + many independent questions
  is the native batch: the vendor cites ~13 questions in one call as 11.5×
  cheaper and 9.6× faster than separate calls, which is why the default is 4
  packages (12 questions) per call.
- **Scoring and tiers.** `rank_score = 0.6·similarity + 0.4·value` on a 1–5
  scale; `≥ JEV_SHORTLIST_MIN_SCORE` (3.5, widened from 4.0) is **shortlist**,
  `≥ JEV_REVIEW_MIN_SCORE` (2.5) is **review**, otherwise **disregard**. A
  post-evaluation compliance gate overrides the tier: title/marketing
  text/features matching the AICIS banned tokens or `electric`/`usb`/
  `rechargeable` forces **disregard** with an explicit note, whatever the
  score. Jev's raw `score` is a **0-based** level position on the criteria list
  (a live probe returned `3.24` for a 5-entry legend keyed `"0".."4"`), so the
  client shifts it by +1 onto 1–5 before any threshold is compared — with the
  shift, a shortlist score means "close match or better".
- **A report, not a deletion.** No package directory is moved or removed — the
  ranker writes the tiered report and Step 7 (human) decides what to validate
  and link. A failed batch call retries once; if it still fails, only that
  batch's packages become `disregard` with a note, and the run continues.
- **Deliverable**: `step-6-ranked-candidates.{json,md}` in the untracked
  `outputs/` tree. The JSON carries the per-package verdicts plus a `tiers`
  grouping; the digest groups the packages by tier with each one's similarity,
  value, rank score and pillar.

---

## 3. Data Contract & Output Structure

### 3.1 Where outputs land

The **general intake** (§2.1) accumulates into the Shopify workspace:

```
/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/
├── product-01/
│   ├── metadata.json       # complete sourcing + pricing contract (below)
│   └── images/
│       ├── image-1.jpg     # pixel-validated product photography
│       ├── image-2.jpg     # (extension follows the real encoded format)
│       └── image-3.jpg
├── product-02/
└── product-03/…
```

Numbering continues past whatever already exists, so successive runs across
different keywords **accumulate** into the same directory without overwriting
earlier work. A candidate that fails the image gates produces **no directory at
all** — an incomplete package is never written.

The **Step-5 gold kernel** (§2.5) lands in its own tree, one subfolder per
supplier, each numbering and deduping only against itself:

```
/Users/ahmadammari/PD/my-store-build/inspiration/optimal-dropship-candidates/
├── cjdropshipping/product-NN/{metadata.json, images/}
└── aliexpress/product-NN/{metadata.json, images/}
```

The two trees never interleave: `dropship-candidates/` is the general intake,
`optimal-dropship-candidates/` is the gold-kernel intake where **both** engines
run every gold keyword. Each supplier subfolder is created on its **first
export**, so an engine that verifies nothing leaves no folder behind.

**Step 6 ranks that gold kernel without touching it** (§2.6): it reads the
packages and writes its tiered report to the untracked `outputs/` tree beside
the other research deliverables —

```
outputs/
├── json/                                     # structured deliverables
│   ├── step-2-search-keywords.json
│   ├── step-3-gold-raw-rows.json             # the Apify scrape dump (--dump-raw)
│   ├── step-3-gold-standard-products.json    # the gold reference Step 6 measures against
│   ├── step-4-gold-keywords.json             # the Step-5 keyword bank
│   └── step-6-ranked-candidates.json         # Step 6's tiered ranking
├── md/                                       # the readable digests of the above
│   ├── step-2-search-keywords.md
│   ├── step-3-gold-standard-products.md
│   ├── step-4-gold-keywords.md
│   └── step-6-ranked-candidates.md
└── logs/                                     # run logs, stamped with the run date-time
```

Each runner's `--output` defaults into `json/` and its digest is written to the
sibling `md/` folder (`config.digest_path`), so the split survives a default
run — a custom `--output` keeps its digest beside it instead.

The `outputs/` tree is git-ignored (scoping data, not repo artefacts), and Step
6 never moves or deletes a package — it only reports.

### 3.2 `metadata.json` schema

Exactly these 13 keys, every run:

```json
{
  "product_title": "Kitchen Sink Caddy Organiser",
  "category": "Kitchen & Household",
  "suggested_price_aud": 49.95,
  "estimated_cogs_aud": 24.61,
  "cogs_estimation_basis": "Supplier listed price AUD $5.19 plus AUD $19.42 tracked shipping to AU via CJPacket Eub, taken directly from the live CJdropshipping listing and its own freight quote.",
  "projected_margin_aud": 25.34,
  "marketing_ad_copy": "...",
  "features": [
    "Rust-resistant stainless steel construction",
    "Adjustable width fits standard AU sink sizes",
    "Sponge + brush storage with drainage"
  ],
  "target_tags": ["dropship", "kitchen", "organisation"],
  "shipping_notice_au": "Standard tracked international shipping to Australia via CJPacket Eub: 6-10 business days.",
  "supplier_name": "CJdropshipping",
  "supplier_retail_url": "https://cjdropshipping.com/product/2097985041113341954.html",
  "image_source": "supplier_gallery"
}
```

| Field | Provenance / guarantee |
|---|---|
| `product_title`, `category`, `marketing_ad_copy`, `features`, `target_tags` | LLM-authored, grounded only in the real supplier listing (no invented specs) |
| `suggested_price_aud` | LLM verdict under the 5-point gate, reconciled against the real cost |
| `estimated_cogs_aud` + `cogs_estimation_basis` | **Not LLM-authored** — the supplier's real listed price **plus its own quoted freight**; the basis string is zero-URL (any URL substring fails schema validation) and cites both halves of the landed cost |
| `projected_margin_aud` | Recomputed deterministically in code (`retail − COGS`) — the LLM's arithmetic is never trusted |
| `shipping_notice_au` | **Not LLM-authored** — derived in code from the freight quote's service name and transit window. Where the supplier quoted no shipping (AliExpress) it is the bare literal `Ships to Australia from the supplier.` and asserts nothing about tracking or transit, because nothing verified them. It makes no claim about what the customer pays, because the pipeline does not know the store's shipping policy |
| `supplier_name`, `supplier_retail_url` | **Not LLM-authored** — copied verbatim from the verified `RawSupplierProduct`; the URL must match the supplier's direct-product-page shape |
| `image_source` | Always `"supplier_gallery"` — imagery provenance for the files in `images/` |

The landed cost is only as honest as the freight quote behind it, so the CJ path
quotes real shipping for the target country on every product and drops a listing
whose freight cannot be quoted. Freight figures move between calls, so a re-run
can shift a cost basis slightly; pin `CJ_FREIGHT_METHOD` when you need one
reproduced.

AliExpress does not quote freight at all, so its landed cost is a **floor, not a
verified figure**. The evaluator is told which it is holding — the prompt payload
carries a `shipping_quoted` flag — and is instructed to price conservatively and
reject a product whose case rests on that best case. The margin floor itself is
unchanged, and nothing invents a freight figure for a supplier that will not give
one.

### 3.3 Image validation gates (every image)

Downloaded bytes are decoded with PIL — HTML `width`/`height` attributes are
never trusted. An image ships only if it passes **all** gates:

1. HTTP 200 with content-type `image/jpeg`, `image/png`, or `image/webp`
   (AVIF thumbnails — the format alicdn often serves — are rejected)
2. Decoded shortest side **≥ 800 px**
3. Byte size in the **15 KiB – 15 MiB** band (rejects icons/sprites/tracking pixels)
4. **SHA-256 byte-hash dedupe** — the same image served twice counts once
5. At least **3 distinct** validated images, or the product is dropped entirely

Before download, alicdn thumbnail URLs are upgraded to the original asset via
`strip_size_suffix()` (covering both the legacy trailing `_640x640.jpg` form and
alicdn's mid-filename `_960x960q75.jpg_.avif` marker); the upgrade is tried first
with the as-served URL as fallback. At most **64 candidate URLs** are considered
and at most **8 images** are kept per product.

### 3.4 Image provenance

Images come from **one exclusive source**: `RawSupplierProduct.image_urls` — the
extractor-captured CDN gallery of the live, verified supplier listing. There is
no fallback tier to ad creatives, competitor stores, or search thumbnails; if the
gallery can't yield 3 validated images, the candidate is dropped and counted,
never exported.

---

## 4. Operations

### 4.1 Batch-sourcing across niches

A single keyword yields a bounded supplier pool, and after the funnel losses
(5-point rejections, liveness drops, image hard-fails) one keyword realistically
yields a handful of packages. Rotate **complementary niches** and let the
incremental `product-NN` numbering accumulate the intake:

```bash
python -m src.main --keyword "desk organizer"     --target-count 3
python -m src.main --keyword "kitchen gadgets"    --target-count 3
python -m src.main --keyword "pet grooming"       --target-count 3
python -m src.main --keyword "camping accessory"  --target-count 3
```

Working niche pool for the AU home/utility angle: desk ergonomics, cable
management, laptop stands, under-desk foot rests, monitor risers, bedside
organisers, pet grooming, car-seat organisers, camping gadgets, home-barista
accessories. Rules of thumb:

- Prefer **several smaller runs across different keywords** over one run with a
  huge `--target-count` — each keyword surfaces a genuinely different supplier
  pool.
- If a keyword lands 0 ACCEPTs, swap it rather than re-running it.
- Verify accumulated intake with `ls $EXPORT_DIR`.

For the store's **self-care & bath pillar**, source in ascending compliance-risk
waves: non-cosmetic ritual accessories first (towels, mats, exfoliating gloves,
body brushes, caddies, soap dishes, robes — no AICIS obligation at all), then
rinse-off cosmetics, and leave-on cosmetics last. **Do not use source-qualifier
keywords** ("dead sea", "himalayan salt", any named mineral, raw material or
branded formulation) — an ingredient-specific keyword surfaces precisely the
products that need full AICIS categorisation first.

### 4.2 Reading the funnel counters

Every run prints a summary with these counters — your first diagnostic:

| Counter | Rises when… | What to do |
|---|---|---|
| `candidates_scraped` | A fully-formed `RawSupplierProduct` was extracted | Baseline — funnel size |
| `evaluated` / `accepted` / `rejected` | The LLM judged the candidate | `rejected` rising is working as intended (commodities, heavy items, saturated goods). If it dominates with plausible winners, retune the keyword |
| `dropped_llm_validation_failed` | The LLM's output stayed schema-invalid after bounded retries, or the call errored | Occasional = fine. Persistent = check `LLM_BASE_URL`/`LLM_MODEL` health |
| `dropped_no_valid_images` | The supplier gallery couldn't yield 3 validated images (no directory was written) | Occasional = fine. Persistent = the supplier's gallery is thin or CDN-hostile for that keyword |
| `skipped_duplicate` | The candidate's `supplier_retail_url` is already in an exported package | Expected when re-running a keyword |
| `candidates_exported` | A complete, validated package landed in the workspace | The number that matters |

Zero exports prints a `Pipeline Funnel Exhausted` intervention block; fewer than
target prints a `[PARTIAL]` note but still exits 0.

**Step 5 folds the same counters per engine** (§2.5), reading
`skipped_duplicate` and `dropped_no_valid_images` off the **exporter itself** —
snapshot before each leg, difference after — because the exporter's own
attributes are cumulative across the bank while `run_pipeline` only copies them
onto a leg's summary on its normal exit. A leg that scraped nothing therefore
contributes exactly 0, and no counter can go negative. The bank adds two of its
own: `keywords_run` (legs that reached the supplier without a configuration or
block error) and `leg_failures` (legs skipped because that engine was
unconfigured, blocked or timed out — the leg is skipped, the keyword is not).

**Where the log lines go.** No runner writes a log file: `main()` calls
`logging.basicConfig(level=INFO, …)` with no handler, so everything goes to
**stderr**, and the `outputs/logs/` tree is filled by hand-capturing that
stream. The runners do not create the folder either — make it once with
`mkdir -p outputs/logs` — and the convention is to stamp the file with the
run's own first timestamp:

```bash
mkdir -p outputs/logs && python -m src.main --keyword "coffee accessories" --target-count 2 --extractor cjdropshipping 2>&1 | tee "outputs/logs/general-intake-run-$(date +%Y-%m-%d-%H%M).log"
```

`*.log` is git-ignored, so the capture stays local.

Each gate announces itself in that stream. The CJ commercial gate, for example,
logs every gated-out hit at WARNING and each keyword's survivors once at INFO in
descending listing-count order — the direct evidence that the ranking ran, and
the list of hits that cost a detail call:

```
WARNING src.extractors.cj_mcp_extractor: Skipping 2601230843431638300: Insufficient CJ list count (15)
WARNING src.extractors.cj_mcp_extractor: Skipping <pid>: Insufficient CJ list count (unavailable)
INFO src.extractors.cj_mcp_extractor: CJ commercial gate passed 3/10 hit(s) for 'garlic grater' (listed counts, strongest first: [1071, 480, 439])
```

To see a keyword's whole threshold distribution *without* spending an LLM call or
writing a package, run `scripts/verify_cj_gate.py`.

### 4.3 CJ liveness: the manual verification step (important)

CJ fronts every product page with a Cloudflare Turnstile wall for automated
clients, so no frontend read can settle whether a listing is still live. CJ
ingestion therefore reads the official **MCP server** instead, and every hit must
pass the **MCP Payload Liveness Gate**: the payload must explicitly confirm an
active listing *and* positive stock. "We couldn't tell" is a drop, not a maybe.

**Therefore: after every run that exports a CJ candidate, open the exported
`supplier_retail_url` in a real browser and confirm the product page renders
normally and is in stock.** The gate is strict, but the browser check remains the
cheap conclusive safeguard. The cross-repo QA audit
(`my-store-build/scripts/audit_dropship_candidates.py`) also re-verifies exported
URLs against the geo-block signals.

### 4.4 Blocks and the human-intervention protocol

When every extractor in the chain is unconfigured, blocked, or timing out — or
the LLM configuration is missing — the pipeline stops and prints an instruction
block:

```
[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]
------------------------------------------------------------
Step: <step name>
Reason: <concrete cause>

Instructions for You:
1. <exact remediation action>
2. <command/config to run>
3. <confirmation to paste back>
------------------------------------------------------------
```

Playbook:

- **`CJ_MCP_TOKEN` issues:** re-copy the token from CJdropshipping's API
  Authorization page. It is a static MCP token embedded in the endpoint URL as a
  path segment — there is no access-token cache or refresh.
- **AliExpress blocks:** the DS Center needs no credential, so a block is
  AliExpress refusing this client rather than a config fault; the chain otherwise
  falls through to the next engine. If the refusal is a session/auth error, the
  run halts with re-login instructions — re-run
  `python scripts/generate_ali_session.py`, or delete `ALI_DS_STATE_PATH` and run
  anonymously again. Keep `TARGET_COUNTRY` on the market you actually sell into:
  the DS Center quotes prices per ship-to market, and the pin is what keeps the
  landed cost honest.
- **LLM config:** verify `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` against your
  Ollama Cloud account.
- **CJ MCP rate limits:** tool calls return 429-style errors under load — the
  client retries once after a 2s backoff before surfacing; space out keyword
  runs. A dropped transport connection mid-call surfaces as a tool error
  (contained to the current candidate/leg, never fatal).

Step 5 raises its own blocks (§2.5), all with the same rendered shape:

- **Step 5 Keyword Bank** — the Step-4 deliverable is missing, malformed, empty,
  or carries a banned token; the block names
  `scripts/generate_gold_keywords.py` as the fix.
- **Step 5 Supplier Ingestion** — *every* registered engine failed *every*
  keyword (both suppliers down). A single engine failing is **not** a block.
- **AliExpress Dropshipping Center Session** — the DS Center refused the client
  mid-bank (the leg raises `DsCenterSessionExpiredError`, deliberately **not**
  swallowed by `run_pipeline`); re-run `scripts/generate_ali_session.py` or
  delete `ALI_DS_STATE_PATH` and run anonymously.
- **AliExpress PDP shells** — Ali's anti-bot can serve empty shell pages to the
  gallery harvest while the DS Center APIs keep answering; the run stays
  healthy and every blocked item is now a visible WARNING plus a per-keyword
  harvest tally (`PDP gallery harvest for '<kw>': N candidate(s), X upgraded,
  Y empty after retry, Z page load(s)`). Shells at ~100% are an Ali-side
  block, not a code fault; they decay with time or clear on a different IP
  (a VPN exit IP cleared them instantly in testing). A collapsed Ali yield
  with a healthy report is what to look at first.
- **Step 5 Funnel Exhausted** — the bank produced **zero** exported packages
  across both engines.
- **LLM config** — the shared evaluator is built before the first leg, so a bad
  `.env` fails before supplier quota is spent.

Step 6 raises two blocks (§2.6), same rendered shape:

- **Step 6 Ranking Intake** — the Step-3 gold list or the Step-5 gold-kernel tree
  is missing or empty; the block names the runner to re-run
  (`scripts/run_gold_standard_research.py` / `scripts/ingest_keyword_bank.py`).
- **LLM Evaluation Filter Configuration** — `OPENROUTER_API_KEY` is unset. The
  gold set is settled before the client is built, so a missing Step-3
  deliverable fails before any billable System One call.

### 4.5 Running the test suite

```bash
python -m pytest tests/ -v
```

The suite is **hermetic — zero network**: every external service is faked
(`httpx.MockTransport`, fake MCP sessions, scripted Playwright/MTOP fakes, a
faked Apify SDK and scripted LLM transports). Coverage spans the schema
validators and supplier gates, the LLM prompt/reconcile contract, the image
gates against real PIL-encoded fixtures, the exporter hard-fail rules, the
orchestrator counters and CLI exit codes, and the Step-3/4/5/6 engines.

---

## 5. Repository Layout

```
dropship-scout-agent/
├── README.md
├── CLAUDE.md                  # Operating mandate (current architecture)
├── pyproject.toml             # Pinned dependencies + pytest configuration
├── .env.example               # Template for runtime configuration
├── .env                       # Actual credentials — never committed
├── .mcp.json                  # Trend-research MCP servers (Step 2)
├── src/
│   ├── config.py              # Env-driven settings singleton
│   ├── models.py              # Pydantic schemas + sourcing/anti-hallucination validators
│   ├── main.py                # CLI orchestrator, extractor chain, funnel counters
│   ├── exporter.py            # Workspace writer (delegates all imagery)
│   ├── evaluators/
│   │   ├── llm_filter.py      # 5-point gate via instructor (no image/supplier fields)
│   │   └── gold_curator.py    # Step-3 gold-product LLM curation (select-by-url only)
│   ├── extractors/
│   │   ├── base.py            # Extractor ABC + block/timeout/not-configured exceptions
│   │   ├── cj_mcp_extractor.py    # CJdropshipping MCP + commercial gate + liveness gate
│   │   ├── aliexpress_ds.py       # Native Dropshipping Center ingestion + winner gate
│   │   └── google_shopping.py     # Step-3 gold-research Apify actor wrapper (not a supplier extractor)
│   ├── keywords/              # Step-4 gold-standard keyword engine
│   │   ├── gold_keyword_prompt.md  # Step-4 prompt resource ({PRODUCT_TABLE} slot)
│   │   └── generator.py       # Batched generation + code-side pool validation
│   ├── ranking/               # Step-6 Jev product ranking
│   │   ├── jev_client.py      # System One transport + all question/threshold constants
│   │   └── jev_product_ranker.py  # Batching, composite score, tiers, report writer
│   └── pipeline/
│       ├── cj_mcp_client.py   # CJ MCP client (token-in-URL auth, log redaction, liveness gate)
│       ├── image_sourcing.py  # Deterministic supplier-gallery image engine
│       └── keyword_bank.py    # Step-5 bank loader + dual-supplier ingestion
├── scripts/
│   ├── generate_ali_session.py  # Optional saved DS Center login
│   ├── verify_cj_gate.py        # CJ list-count threshold diagnostic (no LLM, no export)
│   ├── mcp_headers.py           # MCP headersHelper: emits the trend-server auth headers
│   ├── generate_gold_keywords.py      # Step-4 keyword bank runner
│   ├── ingest_keyword_bank.py         # Step-5 dual-supplier ingestion runner
│   ├── rank_optimal_candidates.py     # Step-6 Jev ranking runner (report only, no deletion)
│   └── run_gold_standard_research.py  # Step-3 gold-product research runner (Apify + LLM)
└── tests/                     # Hermetic test suite, zero network
```

---

## 6. Design Guarantees (the anti-hallucination contract in brief)

1. **Candidates are pre-verified supplier listings** — real PDP URL, real listed
   price, ≥ 3 gallery URLs before the LLM runs; CJ hits additionally pass the MCP
   Payload Liveness Gate.
2. **The LLM sees and produces no image URLs** and never authors supplier, COGS,
   or basis fields — imagery and sourcing data are attached deterministically,
   after evaluation.
3. **Every supplier URL is a direct product page** — per-supplier URL-shape
   validation; search gateways are structurally impossible in the export.
4. **ACCEPT = complete, reconciled sourcing data** — enforced by Pydantic
   `model_validator`s, with bounded instructor retries before a drop; margins are
   recomputed in code and the margin floor is enforced twice.
5. **No incomplete packages** — a candidate failing the 3-image minimum is
   dropped and counted, never exported with empty `images/`.
6. **Funnel drops are always counted** — the run summary surfaces every gate's
   drop count so data-quality regressions are visible immediately.
