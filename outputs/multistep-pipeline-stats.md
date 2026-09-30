# Multi-Step Pipeline — Deliverable Counts (Steps 2–6)

Widened run: **2026-09-29 → 2026-09-30** (branch `fix/gold-funnel-bottlenecks`,
PR #4; the prior full run's counts are in CLAUDE.md §13.5–§13.7).

| Metric | Count |
|---|---|
| Step 2 — AU search keywords | 40 |
| Step 3 — gold-standard products | 263 |
| Step 4 — gold-standard supplier keywords | 320 (40 products × 8 keywords, 5 pools × 8 via `--chunk-size 8`, merged band [270, 340]) |
| Step 5 — supplier packages exported | 82 new (+18 carried over: **100 on disk**) |
| Step 6 — products ranked | 100 |
| Step 6 — filtered (shortlist) | 9 |
| Step 6 — review | 4 |
| Step 6 — disregard | 87 — including **43 demoted by the post-evaluation compliance gate** (banned minerals/cosmetics tokens, electric/usb/rechargeable) |

## Distribution by supplier

| Step | CJDropshipping | AliExpress |
|---|---|---|
| Step 4 keywords ingested | 319 legs (1 failed leg) | 320 legs |
| Step 5 funnel (scraped → accepted) | 1,055 → 390 | 6 → 1 |
| Step 5 packages on disk | **100** | **0** |
| Step 6 ranked | 100 | 0 |
| Step 6 shortlist | 9 | 0 |

AliExpress exported nothing this run: every DS Center search leg answered, but
Ali's anti-bot served ~98% of item PDPs as empty shell pages at the gallery
harvest, so items died at the 3-image minimum (see CLAUDE.md §12 — the block
is IP/volume-driven; a VPN exit IP cleared it in testing, so a re-ingestion
through a clear IP can fill the Ali tree later). The `aliexpress/` subfolder
is created on its first export, hence absent.