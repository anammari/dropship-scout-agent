# The Morning Anthem — Multi-Step Pipeline Stats

**Stats only.** The per-step narrative (schemas, gate rules, why each design
decision was made) lives in `CLAUDE.md` §13.8; this page is the numbers.

Current state: **2026-10-16**, branch `feature/morning-anthem-pipeline-update`.

Two states are reported below, because the second is not finished:

| | What | Status |
|---|---|---|
| **Run A** (2026-10-13) | the launch run, Steps 1 → 7 | complete, applied |
| **Run B** (2026-10-16) | the catalogue review — compliance scope + family cap | re-ranked, **cleanup dry-run only** |

## Pipeline — Steps 1 → 7 (Run A)

| Step | What | In | Out | HasData credits |
|---|---|---|---|---|
| 1 | Trends research (HasData MCP, `geo=AU`) | 7 calls | **63** candidate strings | 70 |
| 2 | Jev product-intent gate + selection | 63 candidates | 61 gated → **30** selected keywords | 0 |
| 3 | Google Shopping scrape + curator + Jev pillar gate | 30 keywords | 600 rows → **117** gold products | 300 |
| 4 | Keyword bank (exact title first) | 117 gold products | 80 eligible → **156** keywords (80 exact + 76 fallback) | 0 |
| 5 | CJ-only ingestion | 156 keywords | 80 products attempted → **41** new exports (**78** on disk) | 0 |
| 6 | Jev product ranking | 78 packages | shortlist 23 · review 10 · disregard 45 | 0 |
| 7 | Automated cleanup (`--apply`) | 78 packages | **45 deleted**, **33 retained** | 0 |
| | **Total** | | | **370** of the 400-credit production cap |

Only Steps 1 and 3 spend HasData credits; the CJ side is free in credits and
bounded by pacing. 580 of the 1,000-credit monthly tier remains.

## Step 5 — ingestion funnel (Run A)

Two runs, both CJ-only, accumulating into one `product-NN` sequence.

| Run | Bank | Keywords | Products | Exports | scraped | evaluated | accepted | rejected |
|---|---|---|---|---|---|---|---|---|
| 1 | 30 products | 58 | 30 (1 empty) | 37 | 318 | 232 | 94 | 138 |
| 2 | 80 products | 156 | 80 (1 empty) | 41 | 1,035 | 1,007 | 402 | 605 |

Run 2's drops: `duplicate` **309** (re-offered products from run 1, skipped by
`supplier_retail_url` — no re-download), `dropped_no_valid_images` **52**,
`fallbacks_skipped` 8, `dropped_llm_validation_failed` **0**, failed legs **0**.

Run 2's `duplicate` count is the duplicate-skip logic working, not waste: the
80-product bank re-offers the 30 run-1 products, whose packages already exist.

## Run B — re-rank and planned cleanup (2026-10-16)

Two code changes drove this run; both are applied to the 33 survivors.

**Compliance gate is now field-scoped.** A banned token in `product_title` is
supplier fact and still forces `disregard`. A token in `marketing_ad_copy` or
`features` is the pipeline's own wording: the note is recorded, the tier is
left to the score.

**Family cap is global.** `CLEANUP_MAX_PER_FAMILY` (5) caps one product family
anywhere in the tree, derived from the package title. It runs last, so it
**overrides the shortlist keep**.

| Stage | Result |
|---|---|
| Re-rank (Step 6) | 33 packages → shortlist 23 · review 9 · disregard 1 |
| Advisory demotions after the scope fix | **0** |
| Family cap in the plan (Step 7 dry run) | dog-gear 21 survivors → **5** kept |
| **Step 7 dry-run plan** | **16 kept · 17 to delete** (16 dog-gear beyond cap + 1 disregard) |

Step 7 has **not** been applied. The 17 deletions are pending confirmation,
because 16 of them are `shortlist`/`review` packages and the act is
irreversible.

### Run B — what survives the plan

| Pillar | Retained (Run A) | Kept under the plan |
|---|---|---|
| `sunrise_move` | 23 | **6** (5 dog gear + 1 beach towel) |
| `restful_reset` | 6 | **6** |
| `mindful_awakening` | 3 | **3** |
| `brew_fuel` | 1 | **1** |
| `morning_table` | 0 | **0** |
| **Total** | **33** | **16** |

## Pillar coverage — Run A

| Pillar | Step-2 keywords | Step-3 gold | Step-4 keywords | Step-5 packages | Retained |
|---|---|---|---|---|---|
| `sunrise_move` | 11 | 43 | 58 | 37 | 23 |
| `restful_reset` | 4 | 16 | 20 | 13 | 6 |
| `mindful_awakening` | 4 | 12 | 20 | 6 | 3 |
| `brew_fuel` | 6 | 23 | 32 | 10 | 1 |
| `morning_table` | 5 | 23 | 26 | 11 | 0 |
| *(no pillar)* | — | — | — | 1 | 0 |
| **Total** | **30** | **117** | **156** | **78** | **33** |

The Step-4 column is the 156-keyword bank (from 80 eligible products); the
Step-5/retained columns are tagged by Jev at Step 6, so the one package Jev
classified `discard` sits outside the five pillars.

`morning_table` retained **nothing**. Diagnosed, not guessed: of its 11
packages, 10 scored similarity 1.06–1.84 — CJ returned meat grinders and grape
peelers against designer cutting boards. One scored 4.58 and was deleted by
the old compliance gate.

## Compliance gate — Run A, by field

**24 of 78** packages carried a banned-token match; 7 of those were demoted out
of `shortlist`/`review`.

| Field | Matches | Kind | Effect now |
|---|---|---|---|
| `product_title` | 19 (`electric` ×10, `cream` ×5, `gel` ×3, `salt` ×1) | supplier fact | still deletes |
| `marketing_ad_copy`, `features` | 12 (`oil` ×4, `usb` ×3, `target` ×2, `mud`, `rechargeable`, `cream`) | our own copy | note only |

The highest-scoring package in the whole run — `304 stainless steel cutting
board`, 4.34 — was deleted on an `oil` match in its own generated ad copy.
Under the new scope it would have been **shortlist**. It is already gone from
disk, so the fix is forward-looking only.

## Export hygiene changes (2026-10-16)

| Issue | Fix |
|---|---|
| `dropship` tag on all 33 packages | stripped at export — it is an internal ACCEPT marker, not a customer-facing tag |
| `"6-10 business days"` on all 33 | now `"6-10 days"` — CJ quotes calendar days, so the old label promised delivery about a third sooner than quoted |
| Category is LLM prose ("Pet Gear — Dog Walking") | **not fixed** — needs the pillar plumbed to the exporter |

## Where the artefacts live

- Structured deliverables — `outputs/json/`
- Readable digests — `outputs/md/` (the per-package Step-6 report is
  `morning-anthem-step-6-ranked-candidates.md`)
- Run logs — `outputs/logs/`
- Production packages — `my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/`
