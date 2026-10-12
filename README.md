# Dropship Scout Agent — The Morning Anthem

An autonomous eCommerce intelligence pipeline that discovers, validates, and
packages winning dropshipping product candidates for **The Morning Anthem**,
a premium morning-lifestyle Shopify store launching in Australia (AUD). The
agent ingests **live, verified products directly from the supplier API**
(Supplier-First architecture), filters every candidate through an LLM-enforced
**5-point commercial gate**, downloads validated product photography
deterministically from the supplier's own CDN gallery, and writes complete,
sourcing-ready product packages into the Shopify store workspace.

**Source scope:** supplier = **CJdropshipping only** (MCP); research and
shopping data = **HasData only**. The AliExpress Dropshipping Center extractor
and the Apify Google Shopping actor were retired in the 2026-10 rebrand — see
`CLAUDE.md` §1.

```
[Supplier extractor — CJdropshipping MCP (the only engine)]
            │  (hits must pass the CJ Commercial Gate, the MCP
            │   Payload Liveness Gate and the Freight Quote)
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
keyword at a time into `dropship-candidates/`, and the **multi-step gold-kernel
pipeline** (§2.3–2.7, Steps 2–6), which researches proven AU retail demand,
gates the trend strings through Jev, scrapes Google Shopping for the winners,
generates a supplier keyword bank, ingests it through CJdropshipping, and ranks
the result with Jev.

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
- **Ranking reports; Step 7 deletes.** Step 6 ranks the gold kernel against
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
- A supplier credential: `CJ_MCP_TOKEN` — the pipeline's only supplier.
- Research credentials for the multi-step pipeline: `HASDATA_API_KEY` for the
  Step-2 Trends MCP and the Step-3 Google Shopping source (the only research
  and shopping provider), and `OPENROUTER_API_KEY` for the three Jev gates
  (Step-2 product-intent, Step-3 pillar, Step-6 ranking). Both are optional
  for the general intake (§2.1) — only the steps that use them need them.
- Internet access to `cjdropshipping.com`, `api.hasdata.com`, `ollama.com`,
  `openrouter.ai`, and supplier CDN hosts.

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
`pyproject.toml`). Playwright, playwright-stealth and `apify-client` were
removed with their pipelines.

### 1.4 Browser binary — no longer needed

The Chromium download step is gone with the AliExpress Dropshipping Center
extractor (its MTOP calls and PDP harvest were the only consumers). The
remaining CJ (MCP) engine never opens a browser.

### 1.5 Configure `.env`

Copy the template and fill in your credentials:

```bash
cp .env.example .env
```

`.env.example` is the authoritative template: it carries every key in order
with its description, and `src/config.py` reads exactly those keys. **This
README deliberately does not duplicate the list** — the copy that used to live
here drifted out of step with the code (it still described the retired
AliExpress and Apify keys). For the full reference see `.env.example` and
`CLAUDE.md` §10.

The keys that matter for The Morning Anthem:

| Key | Why |
|---|---|
| `CJ_MCP_TOKEN` | the sole supplier engine (CJdropshipping MCP) |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | the viability + curation LLM |
| `HASDATA_API_KEY` | Step-2 Trends MCP + Step-3 Google Shopping (the only research provider) |
| `OPENROUTER_API_KEY` | Jev (System One) — the Step-2 product-intent gate, Step-3 pillar gate and Step-6 ranking |
| `USD_TO_AUD`, `MIN_MARGIN_AUD` | AUD pricing: CJ quotes USD, the store sells in AUD |
| `CJ_DEFAULT_DEST_COUNTRY`, `TRENDS_GEO`, `SHOPPING_GL`, `SHOPPING_DOMAIN` | the AU launch geography |
| `HASDATA_PRODUCTION_CAP`, `HASDATA_GS_MAX_KEYWORDS`, `TRENDS_MAX_CALLS` | the credit guardrails (see §2.3) |

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
| `--extractor` | `auto` | Force the engine: `auto` or `cjdropshipping` |

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

**AliExpress Dropshipping Center — RETIRED.** The native DS Center extractor,
its MTOP/Playwright machinery, its AU market pin, its winning-product gate and
its saved-session script were deleted in the 2026-10 rebrand: CJ became the
pipeline's only supplier. See `CLAUDE.md` §1 and §6.1.

### 2.3 Step 2 — trend keywords: Jev gate, then Jev selection

Turns the HasData Google Trends research into the AU product-search strings
Step 3 scrapes, through **two Jev passes**: a product-intent gate, then a
quality-ranked selection. **Google Trends answers "what are people searching",
not "what are people buying"** — the same series carries how-to queries, news
topics, brand controversies and service/digital goods — so every candidate
string is classified before it may cost a Google Shopping request.

```bash
source .venv/bin/activate && python scripts/gate_trend_keywords.py \
    [--input outputs/json/morning-anthem-step-2-trend-keywords.json] \
    [--batch-size 8]
```

- **Research (agent-side, HasData Trends MCP).** Up to 5 comma-separated terms
  per call; the candidate file is
  `{"keywords": [{keyword, pillar, demand_evidence}, …]}` with each string
  tagged to one of the five Morning Anthem pillars.
- **The gate** (`src/research/trend_gate.py`): one `choice` question per
  candidate (`<i>__product_intent`) over a closed four-way set —
  `product_search` / `informational` / `news_or_brand` / `service_or_digital`.
  Only `product_search` survives. Batched at `TREND_GATE_BATCH_SIZE` (8) per
  System One call; a failed call retries once, then drops only that batch with
  a note. Jev authors nothing — the keyword, pillar and Trends evidence are
  carried through verbatim.
- **The banned-token screen.** A survivor is *also* screened with
  `carries_banned_token`, the keyword engine's own boundary. The pilot proved
  why: Jev correctly reads `milk frother kmart` as a product search, but that
  string could never become a bank keyword, so scraping it would waste a
  credit. Brand-manufacturer queries (`nespresso frother`) still pass — they are
  genuine product searches, and excluding branded *goods* is Step 3's curator's
  job.
- **The selection pass** (`src/research/trend_select.py`). The gated pool is
  wider than the scrape can afford, so Jev scores every survivor along five
  ordered *sourcing-quality* levels — one `score` question per candidate — and
  **code** cuts the best `TREND_SELECT_TARGET` (30). The state hands the model
  everything it can use: each query, its pillar, its Trends evidence verbatim
  and the kind of figure it is (a 5-year mean and a rising query are different
  claims), plus the brand line and all five pillar descriptions. **Code then
  owns representation**: each pillar's quota is its proportional share of the
  target (floor `TREND_SELECT_MIN_PER_PILLAR`, 2; never more than the pool
  holds), reconciled to the target by largest remainder, and within each pillar
  the best rows take the quota. So a pillar supplying 40 of 60 candidates takes
  ~20 of 30 slots rather than all 30 — the cut mirrors the researched demand
  instead of the loudest pillar's scores. `--no-pillar-quota` reverts to a pure
  best-of. Scoring is batched at 12 per call; a failed batch retries once, then
  its rows rank last — and if *nothing* scored, nothing is selected and the
  runner halts, because scraping an unranked pool would spend the budget on no
  basis.
- **Deliverables**: the **selected** keywords go to
  `MORNING_ANTHEM_TREND_KEYWORDS_PATH` (Step 3's intake, so its credit envelope
  is exactly that list); the full gated pool goes beside it as `…-gated.json`
  (the re-run reserve — it already cost its trends credits, so a different
  `--target` needs no new Trends spend); the pre-gate candidates as `…-raw.json`
  with the verdict on every row. Jev is billed by OpenRouter in **USD**, not
  HasData credits — neither pass touches the 1,000-credit envelope.
- **`--skip-selection`** keeps every survivor as the intake (gate only), and
  `--target` / `--min-per-pillar` override the cut for a pilot. `--target` **is**
  the Step-3 budget: 10 credits per keyword.

### 2.4 Step 3 — gold-standard product research

Builds the gold list that Steps 4–6 measure against: AU retail products with
proven demand, scraped from live Google Shopping AU and curated by the
reasoning LLM.

```bash
source .venv/bin/activate && python scripts/run_gold_standard_research.py \
    [--keywords outputs/json/morning-anthem-step-2-trend-keywords.json] \
    [--limit 1] [--num 20] [--dump-raw /tmp/step3_raw_rows.json] \
    [--from-raw /tmp/step3_raw_rows.json] \
    [--output outputs/json/morning-anthem-step-3-gold-products.json]
```

- **Scrape — HasData only.** The Apify actor and the `--source` switch were
  retired in the 2026-10 rebrand (`src/extractors/google_shopping.py` is gone),
  so `src/extractors/google_shopping_hasdata.py` is the pipeline's only
  shopping source. One Google Shopping **request per keyword**, 10 credits
  each; one request returns the whole grid (~65 rows observed), so `--num` is a
  **local** cap that bounds LLM curation only — never spend. **The keyword
  count is the spend** and `--limit N` is exactly N requests. The module is
  extractor-shaped but deliberately **not** a `BaseSupplierExtractor` — these
  are marketplace retail listings with no supplier PDP, freight quote or
  gallery, so they can never become `RawSupplierProduct` without fabricating
  sourcing fields.
- **Credit guardrails (fail-closed, before the first request).** The runner
  refuses to start when the planned spend would exceed `HASDATA_GS_MAX_KEYWORDS`
  (keyword count), `HASDATA_PRODUCTION_CAP` (the run's credit ceiling), or the
  10-credit pilot cap without `--allow-over-pilot-budget`. The launch-run
  profile is **30 keywords = 300 credits**, with Step 2's ≤ 10 Trends calls
  (≤ 100) inside `HASDATA_PRODUCTION_CAP=400`. See §2.3 below and `CLAUDE.md`
  §13.8.
- **Curate** (`src/evaluators/gold_curator.py`): the LLM only **selects by
  verbatim `url`** and annotates compliance/economics. Every product fact is
  **code-assembled from the scraped rows**, so a hallucinated or edited url can
  never become a product. Rows carrying no rating/review KPI are filtered out
  before the LLM — a gold product must carry on-page demand evidence.
- **Pillar gate** (`src/research/pillar_gate.py`): Jev classifies each curated
  product into exactly one of the five Morning Anthem pillars, or discards it —
  discarded products never reach the gold list. It runs **last** on purpose, so
  the decision sees each product's numeric price, rating and review count, not
  just a title. It is the pillar's single author: the curator no longer assigns
  one. If the gate keeps nothing, the runner halts without writing an empty gold
  list.
- **Deliverable**: `MORNING_ANTHEM_GOLD_PRODUCTS_PATH` (+ a `.md` digest grouped
  by the gate's pillar) in the untracked `outputs/` tree. The runner prints the planned credit envelope
  before the first request, and exits non-zero when nothing usable comes back;
  `--from-raw` replays curation over a prior dump at **zero scrape spend** (the
  debugging path).

**Why the HasData source is an HTTP client, not MCP or a skill.** Step 2 reaches
HasData through an MCP server, so the same route was the first choice here — but
the gateway (`https://mcp.hasdata.com/mcp?apis=…`) enumerates 27 API groups and
`google_shopping` is **not** among them (a `tools/list` probe answers `"No
HasData tools match ?apis=google_shopping"`).

The HasData skills *do* cover Google Shopping — the `hasdata` skill lists
`/scrape/google/shopping`, and the `hasdata-cli` skill drives
`hasdata google-shopping` (10 credits/call) — but a skill is an agent-facing
instruction bundle, not something a Python runner can import, and shelling out
to the CLI would add a per-machine binary to a pipeline whose tests are
hermetic and network-free. So the module calls the REST endpoint directly with
the `x-api-key` header; the official CLI builds the identical URL, which
cross-checks the request shape. The skills stay installed for agent-side
research (`.agents/skills/`, §5).

### 2.5 Step 4 — gold-standard keyword bank (exact title first)

Turns the gold list into the supplier search strings Step 5 feeds to
CJdropshipping. The bank is built **exact title first**: each product's primary
keyword is its **own title**, derived in code, and the configured reasoning LLM
contributes only the optional **fallback**.

```bash
source .venv/bin/activate && python scripts/generate_gold_keywords.py \
    [--gold-products outputs/json/morning-anthem-step-3-gold-products.json] \
    [--batch-size 4] [--max-products 150] \
    [--output outputs/json/morning-anthem-step-4-keyword-bank.json]
```

- **The exact keyword is code-derived** (`exact_keyword`): lowercase, apostrophes
  removed, every other run of non-alphanumerics collapsed to a single space —
  `Bodum Chambord French Press, 1L` → `bodum chambord french press 1l`. Copying a
  string is not a job for a model, and a code-derived string cannot hallucinate.
- **The fallback is the model's only job**: one tightly-specific variant of that
  title — the same object named through its mechanics, material, form factor or
  size — for a product whose exact title finds nothing. At most one per product,
  and an empty list is a valid answer the prompt explicitly allows.
- **Validation splits by who could cause the problem.** A broken *code* invariant
  (an exact keyword that does not match its product's title, a missing exact row,
  a row naming an unknown product) fails the run and writes nothing. A *model*
  slip (a fallback too long, punctuated, banned, or sharing no concrete word with
  its own title) costs that one row and the bank is re-validated without it.
- **Deliverables**: `MORNING_ANTHEM_KEYWORD_BANK_PATH` (the bank — each row typed
  `match ∈ {exact, tight_fallback}`) and a `.md` digest listing every product with
  its exact keyword and its fallback. Both in the untracked `outputs/` tree.
- **Cost**: LLM tokens only. This step spends **no HasData credits** — the gold
  list it reads was bought in Step 3.

### 2.6 Step 5 — CJ ingestion (the gold kernel)

Ingests **every** Step-4 product through the CJdropshipping pipeline and exports
what survives into the gold-kernel tree. **The product is the unit**: its exact
title runs first and its fallback only fills whatever target slots the exact
keyword left open, so a fallback is never paid for when the exact title worked.

```
<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/{metadata.json, images/}
```

```bash
source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
    [--keywords outputs/json/morning-anthem-step-4-keyword-bank.json] \
    [--target-per-product 2] [--limit 4] [--export-root <dir>]
```

**This is not the general pipeline's fallback chain.** `--extractor auto` stops
at the first engine that answers; the bank runs *every* product because the point
of the step is to obtain each product's optimal listing and let Step 6 rank them.
Each (product, keyword, engine) *leg* is its own call into the existing
`run_pipeline` with a single-engine list — **no supplier gate is forked or
relaxed**. Every leg still runs CJ's commercial gate, the MCP liveness gate,
the mandatory freight quote (to `CJ_DEFAULT_DEST_COUNTRY`), the LLM viability
gate, the AUD margin floor and the 3-image gallery gate.

The machinery stays engine-generic (a second supplier would need no rewrite),
but `BANK_ENGINES` holds **one** engine today: CJdropshipping.

`BANK_TARGET_PER_PRODUCT` is the number of packages one product may contribute
through one engine: the exact keyword fills them first and the fallback only
what is left. The summary reports `products_attempted` / `products_empty` and a
per-engine `fallbacks_skipped` count, so a run's log shows how often the exact
title was enough.

Failure is **per-leg, not per-run**: an engine that is unconfigured, blocked or
timed out is skipped for that leg only; only when *every* registered engine
failed *every* product does ingestion raise, and the runner renders the
intervention block. The supplier folder is numbered and deduped against itself,
so the same product re-offered by a later keyword is skipped as a duplicate.

```bash
source .venv/bin/activate && python scripts/ingest_keyword_bank.py \
    [--keywords outputs/json/step-4-gold-keywords.json] \
    [--target-per-keyword 2] [--limit 4] \
    [--min-per-pillar N] [--export-root <dir>]
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
one engine is a long run — that is the pacing, not a hang. The supplier
trees are production deliverables, not repo artefacts, and stay untracked.

### 2.7 Step 6 — Jev product ranking (the gold kernel's last automated step)

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
  `product-NN` under `<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/` is
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
- **A report, not a deletion — by the ranker.** No package directory is moved or
  removed *by Step 6*; the separate Step-7 cleanup (dry run by default) is what
  acts on the report and deletes. The
  ranker writes the tiered report and Step 7 (human) decides what to validate
  and link. A failed batch call retries once; if it still fails, only that
  batch's packages become `disregard` with a note, and the run continues.
- **Deliverable**: `step-6-ranked-candidates.{json,md}` in the untracked
  `outputs/` tree. The JSON carries the per-package verdicts plus a `tiers`
  grouping; the digest groups the packages by tier with each one's similarity,
  value, rank score and pillar.

---

### 2.8 Step 7 — cleanup (deletes the rejected packages)

**The only step that deletes, and it is irreversible.** It reads the Step-6
report and keeps the tree to what is still in play: every `shortlist` package is
kept, the top `CLEANUP_REVIEW_KEEP` (**10**) `review` packages by `rank_score` are
kept and the rest go, and every `disregard` package is deleted.

```bash
# 1. see exactly what would go (nothing is touched)
source .venv/bin/activate && python scripts/cleanup_candidates.py

# 2. delete, once the plan looks right
source .venv/bin/activate && python scripts/cleanup_candidates.py --apply
```

**Dry run by default.** `--apply` is what deletes — the same posture as
`my-store-build/scripts/shopify_admin.py`, whose `delete` is `--yes`-gated. A
package holds images from the supplier's CDN and a `metadata.json` bought with
HasData credits, CJ round trips and LLM calls, and the tier floors have never been
calibrated on a live run, so the first pass should be read before it is trusted.

Every candidate path is resolved and must sit strictly inside
`<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/` and be named `product-NN` — checked
twice, in the plan and again at the moment of deletion. An unrecognised tier or a
refused path is **kept** and reported, so a report shape the cleanup does not know
can never become a mass deletion.

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

The **Step-5 gold kernel** (§2.6) lands in its own tree, in the supplier's
subfolder, numbering and deduping only against itself:

```
/Users/ahmadammari/PD/my-store-build/inspiration/morning-anthem-candidates/
└── cjdropshipping/product-NN/{metadata.json, images/}
```

The two trees never interleave: `dropship-candidates/` is the general intake,
`morning-anthem-candidates/` is the gold-kernel intake, where the CJ engine
runs every gold keyword. The supplier subfolder is created on its **first
export**, so an engine that verifies nothing leaves no folder behind.

**Step 6 ranks that gold kernel without touching it** (§2.7): Jev scores every
package on three questions — is it the same object as a gold-standard product
(`similarity`), is it worth listing at its own AUD retail and margin against the
gold reference's demand evidence (`winning_value`), and which pillar is it
(`pillar`) — then tiers the result `shortlist` / `review` / `disregard`. A
post-evaluation compliance gate forces any package whose own text carries a banned
token to `disregard` regardless of score. It writes its tiered report to the
untracked `outputs/` tree beside the other research deliverables —

```
outputs/
├── json/                                     # structured deliverables
│   ├── step-2-search-keywords.json
│   ├── morning-anthem-step-2-trend-keywords.json # Jev-gated Step-2 strings
│   ├── morning-anthem-step-3-gold-raw-rows.json  # the shopping scrape dump (--dump-raw)
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
| `shipping_notice_au` | **Not LLM-authored** — derived in code from the freight quote's service name and transit window. Where the supplier quoted no shipping it is the bare literal `Standard tracked international shipping to Australia from China.` and asserts nothing about a service or transit window, because nothing verified them. It makes no claim about what the customer pays, because the pipeline does not know the store's shipping policy |
| `supplier_name`, `supplier_retail_url` | **Not LLM-authored** — copied verbatim from the verified `RawSupplierProduct`; the URL must match the supplier's direct-product-page shape |
| `image_source` | Always `"supplier_gallery"` — imagery provenance for the files in `images/` |

The landed cost is only as honest as the freight quote behind it, so the CJ path
quotes real shipping for the target country on every product and drops a listing
whose freight cannot be quoted. Freight figures move between calls, so a re-run
can shift a cost basis slightly; pin `CJ_FREIGHT_METHOD` when you need one
reproduced.

A supplier that quoted no freight would leave a **floor, not a verified
figure**. The evaluator is told which it is holding — the prompt payload carries
a `shipping_quoted` flag — and is instructed to price conservatively and reject a
product whose case rests on that best case. CJ always quotes, so this branch is
the defensive one; the margin floor is unchanged, and nothing ever invents a
freight figure.

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

**Step 5 folds the same counters per engine** (§2.6), reading
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
- **CJ blocks:** an unconfigured or blocked CJ MCP endpoint is the only
  supplier-block case left (the AliExpress engine, its credential-free DS Center
  access and its saved-session recovery steps are all retired). Keep
  `CJ_DEFAULT_DEST_COUNTRY` on the market you actually ship into: CJ quotes
  freight per destination lane, and that pin is what keeps the landed cost
  honest.
- **LLM config:** verify `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` against your
  Ollama Cloud account.
- **CJ MCP rate limits:** tool calls return 429-style errors under load — the
  client retries once after a 2s backoff before surfacing; space out keyword
  runs. A dropped transport connection mid-call surfaces as a tool error
  (contained to the current candidate/leg, never fatal).

Step 5 raises its own blocks (§2.6), all with the same rendered shape:

- **Step 5 Keyword Bank** — the Step-4 deliverable is missing, malformed, empty,
  or carries a banned token; the block names
  `scripts/generate_gold_keywords.py` as the fix.
- **Step 5 Supplier Ingestion** — the CJ engine failed *every* keyword. With
  one registered engine this is the total-failure case.
- **[HISTORICAL] AliExpress DS Center Session / PDP shells** — the DS Center's
  re-login recovery and Ali's anti-bot shell pages (`PDP gallery harvest for
  '<kw>': …`) went with the extractor in the 2026-10 rebrand. Recorded because
  the shell phenomenon is a useful precedent: a supplier side can answer
  "successfully" while returning nothing usable, so a collapsed yield
  with a healthy report is what to look at first.
- **Step 5 Funnel Exhausted** — the bank produced **zero** exported packages
  across the registered engine(s).
- **LLM config** — the shared evaluator is built before the first leg, so a bad
  `.env` fails before supplier quota is spent.

Step 6 raises two blocks (§2.7), same rendered shape:

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
scripted LLM/System One transports). Coverage spans the schema
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
├── skills-lock.json           # Pinned HasData agent skills (`.agents/skills/`, §2.3)
├── .agents/skills/            # hasdata + hasdata-cli agent skills (.claude/skills/ symlinks in)
├── docs/                      # Operator-local reference notes — git-ignored, never committed
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
│   │   ├── google_shopping_hasdata.py # Step-3 HasData shopping (the only shopping source)
│   │   └── shopping_row.py        # the shared ShoppingRow contract
│   ├── keywords/              # Step-4 gold-standard keyword engine
│   │   ├── gold_keyword_prompt.md  # Step-4 prompt resource ({PRODUCT_TABLE} slot)
│   │   └── generator.py       # Batched generation + code-side pool validation
│   ├── ranking/               # Step-6 Jev product ranking
│   │   ├── jev_client.py      # System One transport + all question/threshold constants
│   │   └── jev_product_ranker.py  # Batching, composite score, tiers, report writer
│   └── pipeline/
│       ├── cj_mcp_client.py   # CJ MCP client (token-in-URL auth, log redaction, liveness gate)
│       ├── image_sourcing.py  # Deterministic supplier-gallery image engine
│       └── keyword_bank.py    # Step-5 bank loader + CJ ingestion
├── scripts/
│   ├── generate_ali_session.py  # Optional saved DS Center login
│   ├── verify_cj_gate.py        # CJ list-count threshold diagnostic (no LLM, no export)
│   ├── mcp_headers.py           # MCP headersHelper: emits the trend-server auth headers
│   ├── generate_gold_keywords.py      # Step-4 keyword bank runner
│   ├── ingest_keyword_bank.py         # Step-5 CJ ingestion runner
│   ├── rank_optimal_candidates.py     # Step-6 Jev ranking runner (report only, no deletion)
│   └── run_gold_standard_research.py  # Step-3 gold-product research runner (HasData + LLM)
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
