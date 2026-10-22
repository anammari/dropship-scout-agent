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

The surviving Step-5 logs record CJ supplier requests and package exports, but
do not record HasData or Apify charges against an ingestion folder. The tracked
manifests have no research-run ID, so per-folder attribution is **unknown**.
Step 5's CJ ingestion itself uses neither HasData nor Apify credits.

| Ingestion folder | HasData credits | Apify spend (USD) |
|---|---:|---:|
| `ingestion/cjdropshipping/morning-anthem-001/` | Unknown | Unknown |
| `ingestion/cjdropshipping/morning-anthem-002/` | Unknown | Unknown |
| `ingestion/cjdropshipping/morning-anthem-20260930/` | Unknown | Unknown |
| **Folder-attributed total** | **Unknown** | **Unknown** |

Historical research records show **550 HasData credits** across two runs
(370 + 180), and **USD 6.76 total Apify spend**; the recorded 2026-09-29 Apify
run was USD 4.06. These are unallocated historical totals, not costs assigned
to the three ingestion folders. HasData credits and Apify dollars are different
units and are not combined.
