# The Morning Anthem — Pipeline Stats

Numbers only. The design rationale lives in `CLAUDE.md` §13.8.

**Where we stand (2026-10-16):** the pipeline ran end to end, a catalogue review
capped the dog-gear flood, and a second research round tried to fill the two
brand-core pillars. **17 packages on disk** — 15 shortlist, 2 review.

## Round 1 — the launch run

| Step | What it does | Result |
|---|---|---|
| 1 | Trends research (HasData MCP, AU) | 7 calls → 63 candidates |
| 2 | Jev product-intent gate + selection | 63 → 61 gated → 30 keywords |
| 3 | Google Shopping scrape, curate, Jev pillar gate | 30 keywords → 600 rows → 117 gold |
| 4 | Keyword bank, exact title first | 117 gold → 80 products → 156 keywords |
| 5 | CJ-only ingestion | 156 keywords → 78 packages |
| 6 | Jev product ranking | 78 ranked → 23 shortlist · 10 review · 45 disregard |
| 7 | Cleanup + family cap | 78 → **33 kept** |

## Round 2 — the minor-pillar re-source (2026-10-16)

Seeded only the pillars Round 1 left empty. No seed, string or paraphrase from
Round 1 was reused. Saturated pillars were deliberately not touched.

| Step | Result |
|---|---|
| 1–2 | 3 Trends calls → 15 keywords (all fresh) |
| 3 | 15 keywords → 300 rows → **56 gold** (brew_fuel 42, mindful_awakening 10, morning_table 4) |
| 4 | 38 products → **143 keywords** (38 exact + 105 fallback) |
| 5 | 143 keywords → **21 packages** (accepted 124, rejected 574) |
| 6 | 21 ranked → **0 shortlist · 1 review · 20 disregard** |
| 7 | 1 kept, merged into the main tree as `product-48` |

**The result was poor and it is worth stating plainly: 180 credits bought one
review-grade package** — a hand-made ceramic coffee pot at 3.10. The next best
scored 2.22 (a stainless steel *vehicle heating cup*), then everything below 2.0.

### What it proved

The bottleneck is the **CJ catalogue match**, not the keywords.

`brew_fuel` was re-seeded with manual-brew terms (`pour over coffee`,
`gooseneck kettle`, `v60 pour over`, `french press`) and the gold set came back
clean — 42 products, drippers, kettles, presses, no appliances. CJ then returned
ceramic cups, plain mugs, a vehicle heating cup and a garden watering dripper.

The enrichment worked exactly as designed — 143 keywords against ~76 before,
105 fallbacks at ~2.8 per product — and moved the outcome by one package.

## The 17 on disk

| Pillar | Shortlist | Review | Total |
|---|---|---|---|
| `sunrise_move` | 6 | 0 | **6** |
| `restful_reset` | 5 | 1 | **6** |
| `mindful_awakening` | 2 | 1 | **3** |
| `brew_fuel` | 2 | 0 | **2** |
| `morning_table` | 0 | 0 | **0** |
| **Total** | **15** | **2** | **17** |

`sunrise_move`'s 6 = 5 dog gear (the cap) + 1 beach towel.
`brew_fuel`'s 2 = the milk frother + the new ceramic coffee pot.
Rank scores 3.10–4.12 · prices AUD 24.99–49.95.

## Cleanup history

| Action | Packages |
|---|---|
| Ranked at Round 1 Step 6 | 78 |
| Deleted as `disregard` | −45 |
| Deleted as dog gear over the cap of 5 | −16 |
| Deleted as `disregard` (re-rank) | −1 |
| Added from Round 2 | +1 |
| **On disk now** | **17** |

## Cost

| Round | Trends | Step 3 | Total |
|---|---|---|---|
| 1 | 70 | 300 | **370** |
| 2 | 30 | 150 | **180** |
| **Both** | **100** | **450** | **550** of the 1,000 monthly tier |

## Two fixes from the catalogue review

**Compliance gate is field-scoped.** A banned token in `product_title` is
supplier fact and still deletes. A token in `marketing_ad_copy` or `features` is
our own wording — note only. Live case: the highest-scoring package of Round 1,
a cutting board at 4.34, was deleted because its ad copy said "oil" while
describing board conditioning.

**Family cap.** `CLEANUP_MAX_PER_FAMILY` = 5, global, and it overrides the
shortlist keep. 22 of the 23 kept `sunrise_move` packages were dog gear.

## Constraints each step imposes

| Step | Blocks |
|---|---|
| 2 | Non-product searches; retailer-qualified strings |
| 3 | Cosmetics, liquids, consumables, IP; rows with no rating or price |
| 4 | Gold products whose own name carries an AICIS token |
| 5 | Unproven stock; unquotable freight; galleries under 3 valid images; **under 1.2 kg, air-freight, no fragile untreated ceramics**; the AUD 10 margin floor |
| 6 | Banned tokens in `product_title` — including `electric`, `usb`, `rechargeable` |
| 7 | Everything `disregard`; one family above 5 |

## Where things live

- `outputs/json/` structured deliverables · `outputs/md/` digests ·
  `outputs/logs/` run logs
- `my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/` the 17
