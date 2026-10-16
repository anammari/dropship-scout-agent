# The Morning Anthem — Pipeline Stats

Numbers only. The design rationale lives in `CLAUDE.md` §13.8.

**Where we stand (2026-10-16):** the pipeline ran end to end once, then a
catalogue review fixed a compliance false positive and capped the dog-gear
flood. **16 packages survive on disk** — 14 shortlist, 2 review.

## The pipeline, end to end

| Step | What it does | Result |
|---|---|---|
| 1 | Trends research (HasData MCP, AU) | 7 calls → **63** candidate strings |
| 2 | Jev product-intent gate + selection | 63 → 61 gated → **30** keywords |
| 3 | Google Shopping scrape, curate, Jev pillar gate | 30 keywords → 600 rows → **117** gold products |
| 4 | Keyword bank, exact title first | 117 gold → 80 products → **156** keywords |
| 5 | CJ-only ingestion | 156 keywords → **78** packages on disk |
| 6 | Jev product ranking | 78 packages ranked |
| 7 | Cleanup + family cap | 78 → **16** kept |

Steps 1 and 3 are the only ones that spend HasData credits: **370 of the
400-credit** production cap, leaving 580 of the 1,000 monthly tier.

## The final 16

| Pillar | Shortlist | Review | Total |
|---|---|---|---|
| `sunrise_move` | 6 | 0 | **6** |
| `restful_reset` | 5 | 1 | **6** |
| `mindful_awakening` | 2 | 1 | **3** |
| `brew_fuel` | 1 | 0 | **1** |
| `morning_table` | 0 | 0 | **0** |
| **Total** | **14** | **2** | **16** |

`sunrise_move`'s 6 = 5 dog gear (the cap) + 1 beach towel.
Rank scores 3.14–4.12 · prices AUD 24.99–49.95 · margins AUD 14.89–28.32.

## Ingestion funnel (Step 5)

| Bank | Keywords | Products | Exports | scraped | accepted | rejected |
|---|---|---|---|---|---|---|
| Run 1 (30 products) | 58 | 30 | 37 | 318 | 94 | 138 |
| Run 2 (80 products) | 156 | 80 | 41 | 1,035 | 402 | 605 |

Run 2 drops: `duplicate` **309**, `no_images` **52**, `llm_failed` **0**,
failed legs **0**. The 309 duplicates are the skip logic working — run 2
re-offers run 1's 30 products, which were already exported.

## How 78 became 16

| Action | Packages |
|---|---|
| Ranked at Step 6 | 78 |
| Deleted as `disregard` | −45 |
| Survived run 1's cleanup | 33 |
| Deleted as dog gear over the cap of 5 | −16 |
| Deleted as `disregard` (re-rank) | −1 |
| **On disk now** | **16** |

## Two fixes behind that

**Compliance gate is field-scoped.** A banned token in `product_title` is
supplier fact and still deletes. A token in `marketing_ad_copy` or `features`
is our own wording — note only. Live case: the highest-scoring package of the
run, a cutting board at 4.34, was deleted because its ad copy said "oil" while
describing board conditioning.

| Field of match | Count | Kind |
|---|---|---|
| `product_title` | 19 | supplier fact — deletes |
| `marketing_ad_copy` / `features` | 12 | our own copy — note only |

**Family cap.** `CLEANUP_MAX_PER_FAMILY` = 5, global, and it overrides the
shortlist keep. 22 of the 23 kept `sunrise_move` packages were dog gear.

## Exports

| Fixed | How |
|---|---|
| `dropship` tag on every package | stripped at export — internal marker, never customer-facing |
| `"6-10 business days"` | now `"6-10 days"` — CJ quotes calendar days |
| Category was LLM prose | **still open** — needs the pillar plumbed to the exporter |

## Where things live

- `outputs/json/` structured deliverables · `outputs/md/` digests ·
  `outputs/logs/` run logs
- `my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/`
  the 16 packages
