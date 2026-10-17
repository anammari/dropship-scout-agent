# The Morning Anthem — Pipeline Stats

Numbers only. Design rationale lives in `CLAUDE.md` §13.8.

**Where we stand (2026-10-17).** Two research rounds ran end to end. Round 1
built the launch catalogue; a review then removed a compliance false positive
and capped the dog-gear flood; Round 2 tried and failed to fill the two
brand-core pillars. **16 packages on disk, 20 products live in Shopify.**

## Every filtration step, in order

| # | Stage | In | Out | What it cut |
|---|---|---|---|---|
| 1 | Trends research (HasData, AU) | 7 calls | 63 candidates | — |
| 2 | Jev intent gate + selection | 63 candidates | 30 keywords | 2 non-product, 31 held in reserve |
| 3 | Shopping scrape + curate + pillar gate | 30 keywords | 117 gold products | 483 of 600 rows (no rating/price) |
| 4 | Keyword bank (exact titles + fallbacks) | 117 gold | 80 products → 156 keywords | 37 same-type products collapsed |
| 5 | CJ ingestion | 156 keywords | 78 packages | rejects, duplicates, thin galleries |
| 6 | Ranking | 78 packages | 23 shortlist · 10 review · 45 disregard | — |
| 7 | Cleanup: drop `disregard` | 78 | 33 kept | **−45** |
| 8 | Re-rank after the gate fixes | 33 packages | 23 shortlist · 9 review · 1 disregard | — |
| 9 | Cleanup: dog-gear family cap of 5 | 33 | 16 kept | **−17** (16 dog gear + 1 disregard) |
| 10 | Round 2 promoted one keeper | +1 | 17 | — |
| 11 | Cross-tree duplicate removed | 17 | **16** | **−1** (`product-35`, same CJ pid as `optimal-dropship-candidates/product-95`) |

## Step 5 — the two ingestion funnels

| Round | Bank | Products | Exports | scraped | accepted | rejected |
|---|---|---|---|---|---|---|
| 1 | 156 keywords | 80 | 78 packages on disk | 1,035 | 402 | 605 |
| 2 (rebuild) | 251 keywords | 38 | 26 packages | 1,090 | 162 | 885 |

Round 2's bank was the wider one: exact titles, up to 5 model fallbacks ranked
by evidence, code-derived sub-terms, and each pillar's head term. It produced
**fewer** packages than Round 1 — the extra rungs mostly reached category terms
CJ indexes with unrelated goods.

## Round 2 — why the minor pillars are still empty

Round 2 seeded only `brew_fuel`, `morning_table` and `mindful_awakening` with
fresh terms, then ranked against a gold set built from those same pillars.

| Tier | Count |
|---|---|
| shortlist | **0** |
| review | **0** |
| disregard | **26** |

Best six, for scale — the review floor is 2.5:

```
2.28  sim 2.14  Kitchen Natural Wooden Bowl
2.25  sim 1.95  304 stainless steel mug coffee cup
2.23  sim 2.09  Stainless Steel Vehicle Heating Cup (electric)
2.10  sim 1.72  Copper Lid Ceramic Incense Burner
2.10  sim 1.78  Portable Car Cup Stainless Steel
2.04  sim 1.85  3 Sizes Sliding Pizza Peel
```

**The conclusion, now tested twice:** the bottleneck is the CJ catalogue match,
not the keywords. `brew_fuel`'s gold set came back clean this time — drippers,
kettles, presses — and CJ still answered with mugs and car cups. No keyword
strategy fixes what the supplier does not stock.

## Where the products are

| Location | Packages | State |
|---|---|---|
| `morning-anthem-candidates/` | **16** | the Round-1 kernel, reviewed and clean |
| `optimal-dropship-candidates/` | 8 | the earlier shortlist |
| `morning-anthem-candidates-tmp/` | 26 | Round 2, all `disregard`, held pending a decision |
| **Shopify** (`2giusj-zm.myshopify.com`) | **20** | all `ACTIVE` |

## The 16 on disk

| Pillar | Shortlist | Review | Total |
|---|---|---|---|
| `sunrise_move` | 6 | 0 | **6** |
| `restful_reset` | 5 | 1 | **6** |
| `mindful_awakening` | 1 | 1 | **2** |
| `brew_fuel` | 1 | 1 | **2** |
| `morning_table` | 0 | 0 | **0** |
| **Total** | **13** | **3** | **16** |

`sunrise_move`'s 6 = 5 dog gear (the cap) + 1 beach towel.
`brew_fuel`'s review is `product-48`, the one keeper Round 2 produced.

## Cost

| Round | Trends | Shopping | Total |
|---|---|---|---|
| 1 | 70 | 300 | **370** |
| 2 | 30 | 150 | **180** |
| **Both** | **100** | **450** | **550** of the 1,000 monthly tier |

## Two fixes from the catalogue review

**Compliance gate is field-scoped.** A banned token in `product_title` is
supplier fact and still deletes. A token in our own generated copy only adds a
note. Live case: the run's highest-scoring package, a cutting board at 4.34,
was deleted because its ad copy said "oil" while describing board care.

**Family cap.** `CLEANUP_MAX_PER_FAMILY` = 5, global, and it overrides the
shortlist keep — 22 of the 23 kept `sunrise_move` packages were dog gear.

**Electrical goods are sellable again.** `electric`, `usb`, `rechargeable` no
longer delete; a shortlist or review match gains an `[ESCALATE]` note for an
EESS/ACMA check with CJ before linking. **Batteries remain banned.**

## Constraints each step imposes

| Step | Blocks |
|---|---|
| 2 | non-product searches; retailer-qualified strings |
| 3 | cosmetics, liquids, consumables, IP; rows with no rating or price |
| 4 | gold products whose own name carries an AICIS token |
| 5 | unproven stock; unquotable freight; galleries under 3 valid images; under 1.2 kg, air-freight, no fragile untreated ceramics; the AUD 10 margin floor |
| 6 | AICIS tokens and **batteries** in `product_title`; electrical goods kept but escalated |
| 7 | everything `disregard`; one product family above 5 |

## Where things live

`outputs/json/` deliverables · `outputs/md/` digests · `outputs/logs/` run logs
Production packages: `my-store-build/inspiration/`
