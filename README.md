# Dropship Scout Agent

Finds dropshipping product candidates, verifies them against real supplier
data, and exports them as Shopify-ready packages.

Two pipelines share one supplier core:

| Pipeline | What it does |
|---|---|
| **CJ gated ingestion** | one keyword in → validated CJ products out |
| **Morning Anthem multi-step** | AU demand research → keywords → gold products → ranked CJ packages |

Everything starts from a **real supplier listing** — real product URL, real
listed price, real gallery. The LLM never invents a product fact, a price, an
image or a shipping claim; it only ever *chooses* and *judges*.

---

## Pipeline 1 — CJ gated ingestion

Give it a keyword. It searches CJdropshipping, filters hard, and writes what
survives into the workspace.

```
  keyword
     │
     ▼
┌──────────────────┐   commercial gate    ┌──────────────┐
│ CJ MCP search    │── listedNum ≥ 150 ──▶│  detail +    │
│ (China warehouse)│   else DROP          │  liveness    │
└──────────────────┘                      └──────┬───────┘
                                                 │ active + real stock
                                                 ▼
                                          ┌──────────────┐
                                          │ freight quote│  no quote = DROP
                                          └──────┬───────┘
                                                 ▼
                                          ┌──────────────┐
                                          │ LLM viability│  5-point gate
                                          │ + margin floor│  2.5x OR AUD 10
                                          └──────┬───────┘
                                                 ▼
                                          ┌──────────────┐
                                          │ image gate   │  ≥3 valid images
                                          └──────┬───────┘
                                                 ▼
                                    product-NN/{metadata.json, images/}
```

| Stage | Blocks | Why |
|---|---|---|
| CJ commercial gate | `listedNum` < 150, or unreported | CJ publishes no sales figure; listing count is the only demand proof |
| Liveness gate | inactive status, no recognised stock | a delisted item's payload still looks alive |
| Freight quote | no quotable method | costing freight at zero understates landed cost |
| LLM viability | fails 2+ of the 5-point gate | niche fit, margin, logistics, saturation, demo appeal |
| Margin floor | markup < 2.5x **and** profit ≤ AUD 10 | recomputed in code, never trusted from the LLM |
| Image gate | fewer than 3 valid images | no partial packages, no fallback tier |

```bash
source .venv/bin/activate
python -m src.main --keyword "kitchen gadgets" --target-count 3
python -m src.main --keyword "garlic press" --target-count 3 --extractor cjdropshipping
```

---

## Pipeline 2 — Morning Anthem multi-step

Seven steps. Each hands a file to the next, and every step fails closed: a bad
input stops the run rather than producing a short list that looks successful.

```
 Step 1  Google Trends (HasData, AU)         →  candidate strings
 Step 2  gate + select                       →  ~30 product-search keywords
 Step 3  Google Shopping + curate + pillar   →  gold-standard products
 Step 4  keyword bank (title → fallbacks → sub-terms → head term)
 Step 5  CJ ingestion (all Pipeline-1 gates) →  product-NN packages
 Step 6  decision model ranks every package  →  shortlist | review | disregard
 Step 7  cleanup                             →  what stays on disk
```

| Step | What it produces | Blocks |
|---|---|---|
| 1 | trend candidates | — (research only) |
| 2 | selected keywords | non-product searches; retailer-qualified strings |
| 3 | gold products | cosmetics, liquids, consumables, IP; rows with no rating or price |
| 4 | keyword bank | gold products whose own name carries an AICIS token |
| 5 | CJ packages | everything in Pipeline 1, plus unproven stock |
| 6 | tiered ranking | banned tokens in `product_title`; **batteries**; electrical goods kept but flagged `[ESCALATE]` for an EESS/ACMA check |
| 7 | final tree | all `disregard`; one product family above 5 |

**Step 2 — the descent ladder.** A keyword is only worth a Step-3 request if it
is a real product search, so a decision model classifies each candidate first.
Code then cuts a pillar-balanced selection.

**Step 4 — the ladder.** Each gold product is searched by its own title first,
then model-written fallbacks, then code-derived sub-terms, then its pillar's
head term. Step 5 walks the rungs and stops as soon as the product's export
slots fill.

```
Epicurean Kitchen Series Cutting Board
  → cutting board  → series cutting board  → kitchen series cutting board
  → breakfast                        (head term, last resort)
```

**Step 6 — tiers.** `rank_score = 0.6·similarity + 0.4·value` on a 1–5 scale.
≥ 3.5 shortlist, ≥ 2.5 review, else disregard.

```bash
source .venv/bin/activate

python scripts/gate_trend_keywords.py                       # Step 2
python scripts/run_gold_standard_research.py                # Step 3
python scripts/generate_gold_keywords.py                    # Step 4
python scripts/ingest_keyword_bank.py                       # Step 5
python scripts/rank_optimal_candidates.py                   # Step 6
python scripts/cleanup_candidates.py                        # Step 7, dry run
python scripts/cleanup_candidates.py --apply                # Step 7, deletes
```

Useful flags:

```bash
# Step 3: curate from a saved row dump instead of paying for a re-scrape
python scripts/run_gold_standard_research.py --from-raw /tmp/step3_raw.json

# Step 5: run into a scratch root so the real trees are untouched
python scripts/ingest_keyword_bank.py --export-root /tmp/scratch

# Step 6: swap the decision model for one run (Jev is the default)
python scripts/rank_optimal_candidates.py \
    --decision-model perplexity/pplx-decider-flash-v1.2
```

Step 5 is slow by design: up to 10 CJ detail calls plus a freight quote per
keyword. Minutes to hours is the pacing, not a hang.

---

## Running it manually

```bash
# 1. one-time setup
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. configure: copy the template and fill in the keys you have
cp .env.example .env
#    needs at least: CJ_MCP_TOKEN, LLM_API_KEY, HASDATA_API_KEY, OPENROUTER_API_KEY

# 3. check everything works, spending nothing
python scripts/rehearse_pipeline.py     # whole chain on synthetic data, sockets blocked
pytest tests/ -q                        # hermetic suite

# 4. then run whichever pipeline you need (commands above)
```

A key left blank or commented resolves to its code default — blank never means
`0` or `false`.

---

## Output contract

Each candidate becomes `product-NN/` holding `metadata.json` plus
`images/image-N.<ext>`.

| Field | Source |
|---|---|
| `product_title`, `supplier_retail_url`, `supplier_name` | supplier listing, verbatim |
| `estimated_cogs_aud`, `cogs_estimation_basis` | code, from listed price + quoted freight |
| `suggested_price_aud`, `projected_margin_aud` | recomputed in code |
| `marketing_ad_copy`, `features`, `target_tags`, `category` | LLM |
| `shipping_notice_au` | code, derived from the supplier's own quote |
| `image_source` | always `supplier_gallery` |

Images must be JPEG/PNG/WebP, 15 KB–15 MB, at least 800 px on the short side,
and three distinct ones or the candidate is dropped.

---

## Operations

**Funnel counters** print every run: `candidates_scraped`, `evaluated`,
`accepted`, `rejected`, `dropped_llm_validation_failed`,
`dropped_no_valid_images`, `skipped_duplicate`, `candidates_exported`.

**Exit codes:** `0` complete, `1` human intervention required, `2` unexpected
error. An intervention renders a block with the exact steps to take.

**Step 7 deletes.** It is the only step that does, it is a dry run unless you
pass `--apply`, and it refuses any path outside the supplier folder.

**Deliverables** live in the gitignored `outputs/` tree — `json/` for
structured, `md/` for digests, `logs/` for captured run logs. Production
packages go to the Shopify workspace, not the repo.

**CJ liveness** is settled by the MCP stream alone. Spot-check exported
`supplier_retail_url`s in a browser as a final safeguard.

---

## Repository layout

```
src/
  config.py            env-driven Settings
  models.py            supplier + evaluation schemas, gate enforcement
  extractors/          cj_mcp_extractor (sole supplier), google_shopping_hasdata, shopping_row
  research/            Step-2 intent gate, selection, Step-3 pillar gate
  evaluators/          llm_filter (viability), gold_curator (Step 3)
  keywords/            Step-4 bank: exact titles, ladder, prompt
  ranking/             jev_client (System One transport), jev_product_ranker (Step 6)
  pipeline/            cj_mcp_client, image_sourcing, keyword_bank, cleanup
  exporter.py          workspace writer
  main.py              CLI orchestrator
scripts/               one runner per step, plus rehearse_pipeline.py
tests/                 hermetic suite, zero network
```

`CLAUDE.md` is the operating mandate: schemas, gate rules and the config
reference. This README is the quick start; that file is the detail.
