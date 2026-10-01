# Multi-Step Pipeline — Deliverable Counts (Steps 2–6)

Current state: **2026-10-01**. The widened Step-2→6 run was the branch
`fix/gold-funnel-bottlenecks` work (PR #4, merged); the counts below carry that
run forward plus two CJ-only Step-5 top-up runs and a Step-6 re-rank done on
2026-10-01. The per-step narrative lives in CLAUDE.md §13.

## Current state

| Metric | Count |
|---|---|
| Step 2 — AU search keywords | 40 |
| Step 3 — gold-standard products | 263 |
| Step 4 — gold-standard supplier keywords | 320 (40 products × 8 keywords, 5 pools × 8 via `--chunk-size 8`, merged band [270, 340]) |
| Step 5 — packages on disk (`optimal-dropship-candidates/`) | **8** |
| Step 6 — packages ranked (last run) | 32 |
| Step 6 — shortlist | **8** |
| Step 6 — review | 0 |
| Step 6 — disregard | 24 — **13 demoted by the post-evaluation compliance gate** (electric 4, soap 4, mud · serum · gel · usb · oil 1 each) |
| Shopify listings created for the shortlist | 8 (7 batch drafts + `product-45`, published and linked by hand) |

The 8 packages on disk are exactly the 8 shortlisted candidates, so the
gold-kernel tree and the Step-6 report agree: **there is nothing on disk that
Step 6 did not pass**. Everything the re-rank disregarded has been moved out to
`my-store-build/data/archived-candidates/`.

Shortlist (Step-6 rank, 2026-10-01):

| Package | Title | Rank | Pillar |
|---|---|---|---|
| `cjdropshipping/product-45` | Silicone Ice Face Roller | 4.43 | self_care_rituals |
| `cjdropshipping/product-25` | Double-Wall Glass Tea Infuser Bottle | 4.42 | curated_home |
| `cjdropshipping/product-47` | Leak-Proof Ice Roller for Face, Eyes & Neck | 4.34 | self_care_rituals |
| `cjdropshipping/product-46` | Cooling Facial Ice Roller | 4.18 | self_care_rituals |
| `cjdropshipping/product-17` | Curved Stainless Steel Garlic Masher | 4.12 | curated_home |
| `cjdropshipping/product-15` | Circular Stainless Steel Garlic Masher | 3.94 | curated_home |
| `cjdropshipping/product-99` | Bamboo Cheese & Cutting Board with Knife Drawer | 3.80 | curated_home |
| `cjdropshipping/product-95` | Black Ceramic Backflow Incense Burner | 3.73 | curated_home |

## Distribution by supplier

| Step | CJDropshipping | AliExpress |
|---|---|---|
| Step 4 keywords ingested | 319 legs (1 failed leg) | 320 legs |
| Step 5 funnel, widened run (scraped → accepted) | 1,055 → 390 | 6 → 1 |
| Step 5 packages on disk | **8** | **0** |
| Step 6 ranked (last run) | 32 | 0 |
| Step 6 shortlist | 8 | 0 |

AliExpress exported nothing on the widened run: every DS Center search leg
answered, but Ali's anti-bot served ~98% of item PDPs as empty shell pages at
the gallery harvest, so items died at the 3-image minimum (see CLAUDE.md §12 —
the block is IP/volume-driven; a VPN exit IP cleared it in testing, so a
re-ingestion through a clear IP can fill the Ali tree later). The
`aliexpress/` subfolder is created on its first export, hence absent.

## Run history

| When | Step | Scope | Result |
|---|---|---|---|
| 2026-09-29 → 30 | 5 | 320 keywords × 2 engines | 639 legs (1 failed), 82 new CJ packages → **100 on disk**; Ali 0 |
| 2026-09-30 | 6 | 100 CJ packages | shortlist 9 · review 4 · disregard 87 (43 demoted by the compliance gate) |
| 2026-10-01 | — | prune | non-shortlisted packages archived out of the tree |
| 2026-10-01 | 5 | 16 body-brush keywords, CJ only | `scraped=41 evaluated=41 accepted=15 rejected=26` → **5 exported** (`product-100`…`product-104`), 1 keyword empty |
| 2026-10-01 | 5 | 26 body-brush/bath-scrubber keywords, CJ only, `CJ_MAX_PRODUCTS=25` | `scraped=224 evaluated=180 accepted=62 rejected=118` → **24 exported** (`product-100`…`product-123`), 0 empty, 30 duplicate skips |
| 2026-10-01 | 6 | 32 CJ packages (8 shortlist + 24 from the broad run) | shortlist 8 · review 0 · disregard 24 |
| 2026-10-01 | — | prune | all 24 disregarded packages archived |

## Body-brush re-source: no replacement found

`product-02` (*Natural Bristle Body & Bath Brush*, CJ pid
`C9BE8DAC-436F-4B13-8EE0-00129E51BA64`) was delisted on CJdropshipping. Its
Shopify product `gid://shopify/Product/10299971666136` was deleted on
2026-10-01 and its package directory moved to
`my-store-build/data/archived-candidates/2026-10-01-targeted-16kw/cjdropshipping/product-02`.

Both top-up runs above were the search for a replacement. They exported **29
packages between them** and the re-rank put **every one of them in `disregard`**
(best: `product-111`, rank 2.86). CJ's catalogue returns pet-grooming brushes
and electric/motorised cleaning devices under these terms, not a dry body
brush — so `product-02`'s slot in the shortlist stays empty rather than being
filled with a weaker product.

Full details of every archived package are in the two archive folders:

```
my-store-build/data/archived-candidates/2026-10-01-targeted-16kw/cjdropshipping/
my-store-build/data/archived-candidates/2026-10-01-broad-26kw/cjdropshipping/
```

## Where the artefacts live

- Structured deliverables: `outputs/json/` (`step-6-ranked-candidates.json` is
  the current 32-package report; the 100-package 2026-09-30 report was
  superseded in place)
- Readable digests: `outputs/md/`
- Run logs: `outputs/logs/`, stamped with the run's own first timestamp
- Production packages: `my-store-build/inspiration/optimal-dropship-candidates/`

The Shopify side is tracked in
`my-store-build/inspiration/optimal-dropship-candidates/cjdropshipping/BATCH-MANIFEST.md`,
which carries the Shopify product id and CJ SKU list per listing (its
`product-02` row is now historical — that product was deleted).
