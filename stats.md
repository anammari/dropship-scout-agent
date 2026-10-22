# The Morning Anthem — linked CJ ingestion stats

Counts use only manifest rows with a confirmed Shopify Product ID in
`my-store-build/ingestion/cjdropshipping/`. Unlinked candidates, demo products,
and products outside these ingestions are excluded. Pillar labels match the
Shopify theme.

## Where the linked products are

| Ingestion folder | Linked Shopify products |
|---|---:|
| `ingestion/cjdropshipping/morning-anthem-001/` | 7 |
| `ingestion/cjdropshipping/morning-anthem-002/` | 21 |
| `ingestion/cjdropshipping/morning-anthem-20260930/` | 8 |
| **Total** | **36** |

The folders contain tracked `BATCH-MANIFEST.md` records. Product packages and
media are local, untracked ingestion data.

## Pillars by ingestion

| Shopify theme pillar | `morning-anthem-001` | `morning-anthem-002` | `morning-anthem-20260930` | **Total** |
|---|---:|---:|---:|---:|
| The Brew & Fuel | 1 | 6 | 1 | **8** |
| The Sunrise Move | 3 | 4 | 0 | **7** |
| The Mindful Awakening | 0 | 3 | 1 | **4** |
| The Restful Reset | 3 | 2 | 3 | **8** |
| The Morning Table | 0 | 6 | 3 | **9** |
| **Total** | **7** | **21** | **8** | **36** |

## HasData and Apify costs

The available records report research-service spend, but do not tie it to a
specific ingestion folder. The CJ supplier-ingestion step itself uses neither
service.

| Service | Recorded usage | Known spend | Scope |
|---|---|---:|---|
| HasData | 10 Trends requests + 45 Google Shopping requests | **550 credits** (100 + 450) | Two historical runs: 370 + 180 credits; not allocated by ingestion folder |
| Apify | `damilo/google-shopping-apify`, run `rP09Pk3x0nkqlSuLB`, 1,567 rows | **USD 4.06** for this run; **USD 6.76** historical cumulative spend | 2026-09-29 production research; not allocated by ingestion folder |

HasData usage is reported in credits; Apify usage is reported in USD. These
figures remain separate and are not assigned to individual batches.
