# The Morning Anthem — Multi-Step Pipeline Stats

**Stats only.** The per-step narrative (schemas, gate rules, why each design
decision was made) lives in `CLAUDE.md` §13.8; this page is the numbers.

Current state: **2026-10-13**, branch `feature/morning-anthem-pipeline-update`.
The full launch run completed end-to-end, Steps 1 → 7, with Step 7 applied.

## Pipeline — Steps 1 → 7

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

## Step 5 — ingestion funnel

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

## Step 6/7 — tiers

| Tier | Ranked | Deleted by Step 7 | Retained |
|---|---|---|---|
| `shortlist` | 23 | 0 | **23** |
| `review` | 10 | 0 | **10** |
| `disregard` | 45 | 45 | 0 |
| **Total** | **78** | **45** | **33** |

Review landed on exactly the `CLEANUP_REVIEW_KEEP` cap of 10, so the
"delete the 11th and below" rule had nothing to act on this run.

Retained-`rank_score` range **2.65 – 4.30**; retained projected margin
**AUD 13.58 / 20.39 / 29.34** (min / median / max).

## Pillar coverage — retained packages

| Pillar | Step-2 keywords | Step-3 gold | Step-4 keywords | Step-5 packages | Retained |
|---|---|---|---|---|---|
| `sunrise_move` | 11 | 43 | 58 | 37 | **23** |
| `restful_reset` | 4 | 16 | 20 | 13 | **6** |
| `mindful_awakening` | 4 | 12 | 20 | 6 | **3** |
| `brew_fuel` | 6 | 23 | 32 | 10 | **1** |
| `morning_table` | 5 | 23 | 26 | 11 | **0** |
| *(no pillar)* | — | — | — | 1 | 0 |
| **Total** | **30** | **117** | **156** | **78** | **33** |

The Step-4 column is the 156-keyword bank (from 80 eligible products); the
Step-5/retained columns are tagged by Jev at Step 6, so the one package Jev
classified `discard` sits outside the five pillars.

`morning_table` retained **nothing** — all 11 of its packages ranked
`disregard`. Step 2 spent 5 of its 30 paid slots on that pillar, so this is
where the funnel's biggest drop-off sits.

## Compliance gate

**24 of 78** packages carried a banned-token match; 7 of those were demoted
out of `shortlist`/`review`, the rest were already `disregard` on score.

By field — 19 matches sit in `product_title` (supplier fact: `electric` ×10,
`cream` ×5, `gel` ×3, `salt` ×1); **12 sit in the LLM-authored fields**
(`marketing_ad_copy` and `features`: `oil` ×4, `usb` ×3, `target` ×2, `mud`,
`rechargeable`, `cream`). The highest-scoring package in the whole run
(`304 stainless steel cutting board`, 4.34) was deleted on an `oil` match in
its own generated ad copy.

## Where the artefacts live

- Structured deliverables — `outputs/json/`
- Readable digests — `outputs/md/` (the per-package Step-6 report is
  `morning-anthem-step-6-ranked-candidates.md`)
- Run logs — `outputs/logs/`
- Production packages — `my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/`
