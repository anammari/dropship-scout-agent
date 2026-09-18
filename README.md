# Dropship Scout Agent

An autonomous eCommerce intelligence pipeline that discovers, validates, and packages
winning dropshipping product candidates for an Australian Shopify store. The agent
ingests **live, verified products directly from supplier APIs/scrapers** (Supplier-First
architecture), filters every candidate through an LLM-enforced **5-point commercial
gate**, downloads validated product photography deterministically from the supplier's
own CDN gallery, and writes complete, sourcing-ready product packages directly into
the Shopify store workspace.

```
[Supplier extractors — SUPPLIER_PRIORITY_ORDER chain]
  CJdropshipping MCP → AliExpress via Apify → Etsy Open API v3
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
│ 2. EVALUATION — Ollama Cloud LLM     │
│    5-point gate via structured       │
│    (Pydantic/instructor) output;     │
│    margin math reconciled in code    │
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

**Key design contracts** (enforced by schema validators, not prompt wording):

- **Candidates are real before the LLM sees them.** Every candidate is a
  `RawSupplierProduct`: a live supplier product-detail URL, a listed price
  converted to AUD, and ≥ 3 gallery URLs — or it never enters the pipeline.
- **The LLM never touches images, supplier data, or costs.** No image-URL field
  exists on any evaluation model; `supplier_name` / `supplier_retail_url` /
  `estimated_cogs_aud` / `cogs_estimation_basis` are mapped programmatically
  from the verified raw product.
- **Every supplier URL must be a direct product page** — search/category/
  gateway URLs are rejected at the schema level.
- **LLM arithmetic is never trusted** — margins and markups are recomputed in
  code, and an ACCEPT that misses the margin floor (markup ≥ 3.0 OR margin
  > AUD 25) is downgraded to REJECT.
- **No incomplete packages** — a candidate with fewer than 3 validated images
  is dropped entirely; an `images/`-less directory is never written.

---

## 1. Prerequisites & Environment Setup

### 1.1 System requirements

- **macOS** (Apple Silicon — `darwin-arm64`)
- **Python 3.13** via Homebrew (`brew install python@3.13`; the project requires
  ≥ 3.11, and 3.13 is the version the shipped `.venv` was built with)
- A configured **Ollama Cloud** account (the LLM evaluation endpoint)
- At least one supplier credential: `CJ_MCP_TOKEN` (primary), `APIFY_API_TOKEN`
  (AliExpress), or `ETSY_API_KEY`
- Internet access to `cjdropshipping.com`, `apify.com`, `openapi.etsy.com`,
  `ollama.com`, and supplier CDN hosts (`cdn.alibabaimg.com`, etc.)

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

Chromium is used only for the AliExpress PDP gallery harvest (upgrading thin
actor results to full carousel galleries) — the CJ (MCP) and Etsy (Open API)
engines never open a browser.

### 1.5 Configure `.env`

Copy the template and fill in your credentials:

```bash
cp .env.example .env
```

```ini
# .env — runtime configuration (NEVER commit this file)

# CJdropshipping MCP (primary extractor) — paste the token from CJ's API
# Authorization page; it is appended to the MCP endpoint as a path segment
# and is never logged.
CJ_MCP_TOKEN="YOUR_COPIED_MCP_TOKEN"

# LLM evaluation endpoint — Ollama Cloud (OpenAI-compatible)
LLM_BASE_URL="https://ollama.com/v1"
LLM_API_KEY="your_ollama_cloud_api_token"
LLM_MODEL="deepseek-v4-flash:cloud"

# AliExpress extractor (optional; fallback engine in the chain).
# The actor must be pay-per-result — free-tier credit cannot pay a rental.
APIFY_API_TOKEN=your_api_token_here
APIFY_ACTOR_ID=cryptosignals/aliexpress-scraper

# Etsy extractor (optional; fallback engine in the chain)
ETSY_API_KEY=""

# Optional tunables (defaults shown)
TARGET_COUNTRY=AU
USD_TO_AUD=1.55
CJ_MAX_PRODUCTS=10
EXPORT_DIR=/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates
```

| Variable | Required | Purpose |
|---|---|---|
| `CJ_MCP_TOKEN` | for the CJ engine | CJdropshipping MCP token (primary supplier; token in URL path, never logged) |
| `CJ_MCP_BASE_URL` | — | MCP endpoint without the token (defaults to `https://developers.cjdropshipping.com/mcp`) |
| `LLM_BASE_URL` | ✅ | OpenAI-compatible chat-completions endpoint (Ollama Cloud: `https://ollama.com/v1`) |
| `LLM_API_KEY` | ✅ | Ollama Cloud API token |
| `LLM_MODEL` | — | Defaults to `deepseek-v4-flash:cloud` |
| `APIFY_API_TOKEN` | for the AliExpress engine | Managed AliExpress scraper actor |
| `APIFY_ACTOR_ID` | — | Defaults to `cryptosignals/aliexpress-scraper` (pay-per-result; never a rental actor) |
| `APIFY_MAX_ITEMS` | — | Listings fetched per AliExpress keyword run (default `20`) |
| `APIFY_MAX_ITEMS_PER_RUN` | — | Hard item cap per run — the budget guard (default `100`) |
| `APIFY_PRICE_PER_RESULT_USD` | — | Per-result price for the cost estimate (default `0.005`) |
| `APIFY_RUN_TIMEOUT_SECS` | — | Apify run wait bound (default `60`) |
| `ETSY_API_KEY` | for the Etsy engine | Etsy Open API v3 key |
| `SUPPLIER_PRIORITY_ORDER` | — | Comma-separated chain order (default `cjdropshipping,aliexpress,etsy`) |
| `USD_TO_AUD` | — | USD→AUD rate for all price math (default `1.55`) |
| `CJ_MAX_PRODUCTS` | — | CJ products expanded (detail + gallery) per keyword (default `10`) |
| `TARGET_COUNTRY` | — | Extraction/evaluation target (default `AU`); also the AliExpress actor's shipping-destination `country` input. CJ's MCP search `countryCode` is pinned to the China warehouse (`CN`) instead |
| `EXPORT_DIR` | — | Destination workspace (defaults to the Shopify path below) |

**Security:** secrets from `.env` are never printed or logged by the agent
(`Settings` holds a no-leak repr). Keep `.env` out of version control (it is
already covered by `.gitignore`).

---

## 2. CLI Usage & Commands

### 2.1 Running the full pipeline

```bash
python -m src.main --keyword "desk organizer" --target-count 3 --country AU
```

| Option | Default | Meaning |
|---|---|---|
| `--keyword` | `desk organizer` | Seed niche keyword for supplier searches |
| `--target-count` | `3` | Stop as soon as this many ACCEPTed products are exported |
| `--country` | `AU` | Extraction target market |
| `--extractor` | `auto` | Force one engine: `auto`, `cjdropshipping`, `aliexpress`, or `etsy` |

Examples:

```bash
# Standard validated intake of 3 winning products
python -m src.main --keyword "desk organizer" --target-count 3

# Force a single engine
python -m src.main --keyword "kitchen gadgets" --target-count 3 --extractor cjdropshipping

# Scale intake — see §4.1 for the batch strategy this implies
python -m src.main --keyword "foot rest" --target-count 10 --country AU
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Target count reached (`[PIPELINE COMPLETE]`), or fewer packages landed but ≥ 1 (`[PARTIAL]` note printed) |
| `1` | Human intervention required: all extractors unavailable/blocked, LLM config missing, or the funnel was exhausted with **zero** exports |
| `2` | Unexpected error (traceback logged) |

The LLM filter is constructed before extraction, so a misconfigured `.env`
fails immediately without burning supplier quota.

---

## 3. Data Contract & Staging Structure

### 3.1 Where outputs land

```
/Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/
```

Each ACCEPTed, image-validated product becomes one self-contained package:

```
dropship-candidates/
├── product-01/
│   ├── metadata.json       # complete sourcing + pricing contract (below)
│   └── images/
│       ├── image-1.jpg     # pixel-validated product photography
│       ├── image-2.jpg     # (extension follows the real encoded format)
│       └── image-3.jpg
├── product-02/
│   ├── metadata.json
│   └── images/
│       └── image-1.jpg
└── product-03/
    └── ...
```

Numbering continues past whatever already exists (`product-04`, `product-05`,
...), so successive runs across different keywords **accumulate** into the same
staging directory without overwriting earlier work. A candidate that fails the
image gates produces **no directory at all** — an incomplete package is never
written.

### 3.2 `metadata.json` schema

Exactly these 13 keys, every run:

```json
{
  "product_title": "Kitchen Sink Caddy Organiser",
  "category": "Kitchen & Household",
  "suggested_price_aud": 49.95,
  "estimated_cogs_aud": 18.6,
  "cogs_estimation_basis": "Supplier listed price AUD $18.60 taken directly from the live CJdropshipping listing; quoted shipping AUD $0.00 (unquoted by the supplier at scrape time).",
  "projected_margin_aud": 31.35,
  "marketing_ad_copy": "...",
  "features": [
    "Rust-resistant stainless steel construction",
    "Adjustable width fits standard AU sink sizes",
    "Sponge + brush storage with drainage"
  ],
  "target_tags": ["dropship", "kitchen", "organisation"],
  "shipping_notice_au": "Ships from overseas: 7-12 business days via tracked air freight to Australia.",
  "supplier_name": "CJdropshipping",
  "supplier_retail_url": "https://cjdropshipping.com/product/2097985041113341954.html",
  "image_source": "supplier_gallery"
}
```

| Field | Provenance / guarantee |
|---|---|
| `product_title`, `category`, `marketing_ad_copy`, `features`, `target_tags`, `shipping_notice_au` | LLM-authored, grounded only in the real supplier listing (no invented specs) |
| `suggested_price_aud` | LLM verdict under the 5-point gate, reconciled against the real cost |
| `estimated_cogs_aud` + `cogs_estimation_basis` | **Not LLM-authored** — copied from the supplier's real listed price (+ quoted shipping); the basis string is zero-URL (any URL substring fails schema validation) |
| `projected_margin_aud` | Recomputed deterministically in code (`retail − COGS`) — the LLM's arithmetic is never trusted |
| `supplier_name`, `supplier_retail_url` | **Not LLM-authored** — copied verbatim from the verified `RawSupplierProduct`; the URL must match the supplier's direct-product-page shape |
| `image_source` | Always `"supplier_gallery"` — imagery provenance for the files in `images/` |

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
`strip_size_suffix()` (covering both the legacy trailing `_640x640.jpg` form
and alicdn's mid-filename `_960x960q75.jpg_.avif` marker); the upgrade is
tried first with the as-served URL as fallback.

### 3.4 Image provenance

Images come from **one exclusive source**: `RawSupplierProduct.image_urls` —
the extractor-captured CDN gallery of the live, verified supplier listing.
There is no fallback tier to ad creatives, competitor stores, or search
thumbnails; if the gallery can't yield 3 validated images, the candidate is
dropped and counted, never exported.

---

## 4. Operational Best Practices & Troubleshooting

### 4.1 Batch-sourcing across niches

A single keyword yields a bounded supplier pool (CJ expands `CJ_MAX_PRODUCTS`
products per keyword; AliExpress fetches `APIFY_MAX_ITEMS` listings), and
after the funnel losses (5-point rejections, liveness drops, image
hard-fails) one keyword realistically yields a handful of packages. Rotate
**complementary niches** and let the incremental `product-NN` numbering
accumulate the intake:

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

- Prefer **several smaller runs across different keywords** over one run with
  a huge `--target-count` — each keyword surfaces a genuinely different
  supplier pool.
- If a keyword lands 0 ACCEPTs, swap it rather than re-running it.
- Verify accumulated intake with
  `ls /Users/ahmadammari/PD/my-store-build/inspiration/dropship-candidates/`.

**Wellness & bath pool** (Sabaah Goods "Self-Care & Bath Rituals", registered 2026-09-18).
Exploratory sets for the store's fourth pillar, ordered by **compliance risk** so the
lowest-risk wave can be sourced first. The ordering is deliberate, not cosmetic — see the store
repo's `_docs/aicis-compliance.md` for why:

- **Wave 1 — non-cosmetic ritual accessories (no AICIS exposure at all):** bath towels, bath
  mats, exfoliating gloves and loofahs, body brushes, bath trays and caddies, soap dishes, bath
  sponges, robes. These are not industrial chemicals, so importing them carries no AICIS
  registration or categorisation obligation.
- **Wave 2 — rinse-off cosmetics (AICIS: register, then categorise each ingredient):** body wash,
  bar soap, bath soak, shampoo, shower gel. The lower-risk end of the cosmetic range, because
  maximum concentrations in Inventory listings are typically more permissive for rinse-off use.
- **Wave 3 — leave-on cosmetics (deferred — the highest compliance load):** body lotion, body
  oil, hand cream, balm. Inventory listings often restrict or exclude leave-on use outright, so
  these wait until a compliant formula has been evidenced.

**Do not use source-qualifier keywords** — "dead sea", "himalayan salt", "maris limus", or any
named mineral, raw material or branded-formulation term. The pillar is deliberately unbound from
a named raw material until AICIS compliance and import verification settle, and an
ingredient-specific keyword surfaces precisely the products that need full categorisation first.

```bash
python -m src.main --keyword "bath accessories"  --target-count 3
python -m src.main --keyword "body wash"         --target-count 3
python -m src.main --keyword "bar soap"          --target-count 3
```

Two usual caveats apply. Wave 1 items are bulky, so they survive the quality funnel but carry the
shipping cost the candidate audit does not model. And a cosmetic candidate cannot be listed until
the supplier has produced the evidence listed in `_docs/aicis-compliance.md` §4 — INCI ingredient
list with concentrations, rinse-off/leave-on status, importer of record, EU Annex II/III
compliance.

### 4.2 Reading the funnel counters

Every run prints a summary with these counters — your first diagnostic, before
opening any log file:

| Counter | Rises when… | What to do |
|---|---|---|
| `candidates_scraped` | A fully-formed `RawSupplierProduct` was extracted | Baseline — funnel size |
| `evaluated` / `accepted` / `rejected` | The LLM judged the candidate | `rejected` rising is working as intended (commodities, heavy items, saturated goods). If it dominates with plausible winners, retune the keyword |
| `dropped_llm_validation_failed` | The LLM's output stayed schema-invalid after bounded retries, or the call errored | Occasional = fine. Persistent = check `LLM_BASE_URL`/`LLM_MODEL` health |
| `dropped_no_valid_images` | The supplier gallery couldn't yield 3 validated images (no directory was written) | Occasional = fine. Persistent = the supplier's gallery is thin or CDN-hostile for that keyword |
| `candidates_exported` | A complete, validated package landed in the workspace | The number that matters |

Zero exports prints a `Pipeline Funnel Exhausted` intervention block; fewer
than target prints a `[PARTIAL]` note but still exits 0.

### 4.3 CJ liveness: the manual verification step (important)

The retired REST search could not distinguish stale, delisted items, and CJ
fronts every product page with a Cloudflare Turnstile wall for automated
clients — so the frontend check was permanently inconclusive. CJ ingestion
now reads the official **MCP server** instead, and every hit must pass the
**MCP Payload Liveness Gate** (§5 of `CLAUDE.md`): the payload must
explicitly confirm an active listing *and* positive stock. "We couldn't
tell" is a drop, not a maybe.

**Therefore: after every run that exports a CJ candidate, open the exported
`supplier_retail_url` in a real browser and confirm the product page renders
normally and is in stock.** The gate is strict, but the MCP stream has not
yet demonstrated stale-free exports across several runs, so the browser
check remains the conclusive arbiter. The cross-repo QA audit
(`my-store-build/scripts/audit_dropship_candidates.py`) also re-verifies
exported URLs against the geo-block signals.

### 4.4 Blocks and the human-intervention protocol

When every extractor in the chain is unconfigured, blocked, or timing out —
or the LLM configuration is missing — the pipeline stops and prints an
instruction block:

```
[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]
------------------------------------------------------------
Step: Supplier Extraction Stage
Reason: <concrete cause, e.g. all engines blocked / credential missing>

Instructions for You:
1. <exact remediation action>
2. <command/config to run>
3. <confirmation to paste back>
------------------------------------------------------------
```

Playbook:

- `CJ_MCP_TOKEN` issues: re-copy the token from CJdropshipping's API
  Authorization page. It is a static MCP token embedded in the endpoint URL
  as a path segment — there is no access-token cache or refresh.
- AliExpress blocks: check the Apify token is valid and the account's
  monthly spend limit (`$5` on the free tier) is not exhausted, and that
  `APIFY_ACTOR_ID` still points at a pay-per-result actor
  (`cryptosignals/aliexpress-scraper`); the chain otherwise falls through to
  the next engine. Note the actor documents that residential proxies give
  consistent results, which free-tier API runs cannot use.
- LLM config: verify `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` against your
  Ollama Cloud account.
- CJ MCP rate limits: tool calls return 429-style errors under load — the
  client retries once after a 2s backoff before surfacing; space out
  keyword runs.

### 4.5 Running the test suite

```bash
python -m pytest tests/ -v
```

- **292 hermetic tests** across 9 test modules (schema validators incl. the
  zero-URL `cogs_estimation_basis` rule and PDP-shape rejection, the MCP
  Payload Liveness Gate and its wiring in the CJ extractor, the AliExpress
  actor payload/budget guard/mapping and failure taxonomy against a scripted
  `apify_client` fake, LLM prompt/reconcile contract, image gates against
  real PIL-encoded fixtures with mocked httpx, exporter hard-fail rules,
  orchestrator counters and CLI exit codes) — no network.

---

## 5. Repository Layout

```
dropship-scout-agent/
├── README.md
├── CLAUDE.md                  # Operating mandate (current architecture)
├── pyproject.toml             # Pinned dependencies + pytest configuration
├── .env.example               # Template for runtime configuration
├── .env                       # Actual credentials — never committed
├── _docs/plan.md              # Full engineering spec (schemas, gates, config)
├── src/
│   ├── config.py              # Env-driven settings singleton
│   ├── models.py              # Pydantic schemas + sourcing/anti-hallucination validators
│   ├── main.py                # CLI orchestrator, extractor chain, funnel counters
│   ├── exporter.py            # Workspace writer (delegates all imagery)
│   ├── evaluators/
│   │   └── llm_filter.py      # 5-point gate via instructor (no image/supplier fields)
│   ├── extractors/
│   │   ├── base.py            # Extractor ABC + block/timeout/not-configured exceptions
│   │   ├── cj_mcp_extractor.py    # CJdropshipping MCP + liveness gate
│   │   ├── aliexpress_apify.py    # Apify pay-per-result AliExpress scraper + PDP harvest
│   │   └── etsy_api.py            # Etsy Open API v3
│   └── pipeline/
│       ├── cj_mcp_client.py   # CJ MCP client (token-in-URL auth, log redaction, liveness gate)
│       └── image_sourcing.py  # Deterministic supplier-gallery image engine
└── tests/                     # 292 hermetic tests, zero network
```

---

## 6. Design Guarantees (the anti-hallucination contract in brief)

1. **Candidates are pre-verified supplier listings** — real PDP URL, real
   listed price, ≥ 3 gallery URLs before the LLM runs; CJ hits additionally
   pass the MCP Payload Liveness Gate.
2. **The LLM sees and produces no image URLs** and never authors supplier,
   COGS, or basis fields — imagery and sourcing data are attached
   deterministically, after evaluation.
3. **Every supplier URL is a direct product page** — per-supplier URL-shape
   validation; search gateways are structurally impossible in the export.
4. **ACCEPT = complete, reconciled sourcing data** — enforced by Pydantic
   `model_validator`s, with bounded instructor retries before a drop; margins
   are recomputed in code and the margin floor is enforced twice.
5. **No incomplete packages** — a candidate failing the 3-image minimum is
   dropped and counted, never exported with empty `images/`.
6. **Funnel drops are always counted** — the run summary surfaces every gate's
   drop count so data-quality regressions are visible immediately.