# Replace AliExpress ingestion with the pay-per-use `cryptosignals/aliexpress-scraper` actor

## Context

The AliExpress arm of the Supplier-First pipeline is currently wired to
`logical_scrapers/aliexpress-scraper`, an actor that charges a **flat
$20/month rental**. Apify's free-tier $5/month platform credit cannot pay a
rental fee — the platform blocks the run or demands a card — so the current
configuration cannot run "for free" at all. `_docs/aliexpress_ingestion_updated_specs.txt` (since deleted — it was a
planning prompt, not a spec)
directs the pipeline at a **pay-per-use** actor instead.

The operator's instruction: target one of the pay-per-use actors, keep the
existing hardening, stay within the $5 budget for now, prove it with
hermetic tests, and replace the old logic completely.

**Decisions taken (operator-confirmed):**
- Target actor: **`cryptosignals/aliexpress-scraper`**.
- Budget guard: **per-run item cap + startup cost estimate + the Apify
  Console monthly spend limit set to $5 as the authoritative backstop.**

### What was verified against Apify (the spec was wrong on several points)

`cryptosignals/aliexpress-scraper` — no rental, **$0.005 per product
scraped** ($5 ≈ 1,000 products). Real input (from its own Python example):

```python
{"action": "search", "query": "bluetooth headphones", "maxItems": 5,
 "country": "US", "currency": "USD", "sort": "default",
 "proxyConfiguration": {"useApifyProxy": True}}
```

Real dataset item: `id`, `title` (string), `price` (number), `originalPrice`,
`discount`, `currency` ("USD"), `soldCount`, `starRating`, `reviewCount`,
`sellerRating`, `shipping` (string), `store`, `storeId`,
`imageUrl` (absolute `https://ae01.alicdn.com/kf/...`),
`productUrl` (`https://www.aliexpress.com/item/<id>.html`).

Therefore, **do not** implement the spec's literal payload. Its
`{"queries": [...], "maxItemsPerQuery": 20}` matches *neither* pay-per-use
actor (cryptosignals wants a singular `query` **plus a required `action`**;
kawsar wants `keywords`). Its "$10.00 per 1,000 items" figure is also wrong
($0.005 × 1000 = $5.00), and its `item.get("title")` parser would return
`None` against kawsar. The spec's *architecture* guidance is sound and is
what we implement: keep the event-loop offload, keep the timeout bound, keep
the standalone Playwright gallery upgrade.

## Goal

Swap the actor while preserving every existing guarantee, then close the
test gap (there is currently **zero** test coverage for this extractor — no
test imports `apify_client`).

**Non-goals:** no change to `RawSupplierProduct`, the export contract, the
image gates, the LLM stage, or `engine_name` (`tests/test_main.py:125-135`
asserts `"aliexpress_apify"`).

---

## Changes

### 1. `src/config.py`

- Add a module constant beside the other `DEFAULT_*` block (lines 23–34):
  ```python
  DEFAULT_APIFY_ALIEXPRESS_ACTOR = "cryptosignals/aliexpress-scraper"
  ```
  and use it at line 82–84, replacing the inline `"logical_scrapers/..."`.
  Keep the env name `APIFY_ACTOR_ID` unchanged.
- Add the cost/budget fields next to the existing Apify block (lines 82–90):
  ```python
  APIFY_PRICE_PER_RESULT_USD: float = _parse_float(
      os.getenv("APIFY_PRICE_PER_RESULT_USD"), default=0.005)
  APIFY_MAX_ITEMS_PER_RUN: int = _parse_int(
      os.getenv("APIFY_MAX_ITEMS_PER_RUN"), default=100)
  ```
  `_parse_float` already exists (line 49) — reuse it.
- Change `APIFY_RUN_TIMEOUT_SECS` default `300` → `60`, per spec §3 ("a tight
  `wait_duration` wrapper… a hard stop before draining your finite monthly
  Compute Units").
- `APIFY_MAX_ITEMS_PER_KEYWORD` (env `APIFY_MAX_ITEMS`) keeps its name and
  default 20; its meaning narrows to *per single actor run*.

### 2. `src/extractors/aliexpress_apify.py` — the rewrite

Keep unchanged: `engine_name`, `supplier_name`, `_PDP_PATTERN`,
`_PRICE_NUMBER_RE`, `_coerce_price_usd`, `_coerce_images`, `_GALLERY_JS`,
`_META_DESCRIPTION_JS`, `_NAV_TIMEOUT_MS`, `_MAX_GALLERY_CANDIDATES`,
`_MIN_GALLERY`, `_harvest_galleries`, `_build_product`, and the exception
taxonomy in `_run_actor`.

**a. Replace `build_search_url()` with an input builder.** The actor takes a
single `query` string, so one actor run happens **per keyword** (the
orchestrator passes one keyword today). Delete `build_search_url` after
confirming it has no other callers (`grep -rn build_search_url src tests`).

```python
def build_run_input(keyword: str, max_items: int, country: str) -> dict:
    """`cryptosignals/aliexpress-scraper` search payload."""
    return {
        "action": "search",
        "query": keyword,
        "maxItems": max_items,
        "country": country,          # shipping destination, not currency
        "currency": "USD",           # pinned: USD_TO_AUD math depends on it
        "sort": "default",
        "proxyConfiguration": {"useApifyProxy": True},
    }
```

**b. Use the `country` argument.** `fetch_products(keywords, country="AU")`
currently ignores `country`; it now flows into `run_input["country"]`, so the
extractor honours `--country` / `TARGET_COUNTRY` instead of hard-coding US.

**c. Per-run budget guard.** Before the loop compute the planned run count and
log an INFO estimate; cap each run and stop early once the ceiling is hit:

```python
budget = self._max_items_per_run
for keyword in keywords:
    if budget <= 0:
        logger.warning(...)   # per-run cap reached; not an error
        break
    max_items = max(1, min(self._max_items, budget, 500))   # actor max is 500
    budget -= max_items
    items.extend(await asyncio.to_thread(
        self._run_actor, token, actor_id,
        build_run_input(keyword, max_items, country)))
```

Log once at INFO before the first run, e.g.
`aliexpress run plan: 1 keyword(s), up to 20 item(s), estimated cost $0.10
(at $0.005/result)`. The cap bounds worst-case spend per run (default 100
items = $0.50); the Apify Console monthly spend limit remains the
authoritative ceiling. No local ledger, no new persisted state.

**d. Rewrite the item mapping** in `fetch_products` to the real schema,
adding a **currency guard** (mirrors the Etsy extractor's USD-only rule):

```python
url = str(item.get("productUrl") or item.get("url") or "").strip()
title = str(item.get("title") or "").strip()
currency = str(item.get("currency") or "").strip().upper()
if currency and currency != "USD":
    continue                      # reject: USD_TO_AUD would be wrong
price_usd = _coerce_price_usd(item.get("price"))
images = _coerce_images(item.get("imageUrl") or item.get("images"))
```

Everything downstream (thin-gallery harvest, `_MIN_GALLERY` drop, `price_aud
= price_usd × USD_TO_AUD`, `shipping_cost_aud = 0.0`) stays as it is.
`imageUrl` is already absolute and `strip_size_suffix` still upgrades any
`_960x960q75.jpg_.avif` alicdn markers before download.

**e. Update the module docstring** (lines 1–22) to name the new actor, its
per-result pricing, the `action: "search"` payload, and the budget guard.
`_run_actor`'s body needs no change beyond the timeout default it already
reads from settings.

### 3. Tests — new `tests/test_aliexpress_apify.py`

Hermetic, zero network, zero spend. Fake the SDK by monkeypatching the real
module attribute — `_run_actor` imports lazily, so
`monkeypatch.setattr("apify_client.ApifyClient", FakeApifyClient)` takes
effect at call time. Shape:

- `FakeApifyClient(token)` → `.actor(actor_id).call(run_input=..., wait_duration=...)`
  records the args and returns `{"defaultDatasetId": "ds-1"}`;
  `.dataset(id).iterate_items()` yields the scripted items.
  The fake also records `threading.current_thread()` so the offload can be
  asserted.
- Payload fixtures mirroring the verified sample item (absolute `imageUrl`,
  `productUrl` of the `/item/<id>.html` shape, numeric `price`,
  `currency: "USD"`).
- Patch the Playwright path by injecting a fake harvest (monkeypatch
  `AliExpressApifyExtractor._harvest_galleries`) rather than driving a browser.

Cases to cover:

1. **Payload shape** — `run_input` is exactly the dict in §2a (all seven keys,
   `action == "search"`, `currency == "USD"`), and `wait_duration ==
   timedelta(seconds=60)`.
2. **Event-loop offload** — the actor call runs off `MainThread`.
3. **Happy mapping** — one item → one `RawSupplierProduct` with the right
   `product_title`, `supplier_retail_url`, `price_aud == price × USD_TO_AUD`,
   and `image_urls`.
4. **Currency guard** — `currency: "EUR"` is skipped.
5. **PDP validation** — a search/category `productUrl` is skipped.
6. **Price coercion** — numeric and `"US $8.99"` both parse; `0`/absent/None
   are skipped.
7. **Thin gallery upgraded** — a single-image item whose harvest yields ≥3 is
   emitted.
8. **Never fabricated** — a harvest yielding <3 drops the candidate (no
   product returned).
9. **Harvest crash isolation** — a raising/timeout harvest skips only that
   candidate and the run continues.
10. **Per-candidate `_build_product` failure isolation** — one bad candidate
    does not abort the batch.
11. **Budget guard** — `maxItems` is clamped to the per-run cap and to the
    actor's 500 hard max; a multi-keyword call stops requesting once the cap
    is consumed; the estimate is logged.
12. **Error taxonomy** — `401`/`unauthorized` → `ExtractorBlockedException`;
    `timeout` → `ExtractorTimeoutException`; other call errors → Blocked;
    missing `defaultDatasetId` → Blocked; dataset read failure → Blocked.
13. **Not configured** — empty token → `ExtractorNotConfiguredError("APIFY_API_TOKEN")`.
14. **Identity unchanged** — `engine_name == "aliexpress_apify"`,
    `supplier_name == "AliExpress"`.

Add an autouse fixture (in the new file or `tests/conftest.py`) that
neutralises `settings.APIFY_API_TOKEN`, mirroring the existing CJ fixture in
`tests/conftest.py:22-27` — otherwise a real token in `.env` could reach a
test.

**Update `tests/test_config.py`:** the two hard-coded actor assertions at
lines 93–105 must expect `cryptosignals/aliexpress-scraper`; add tests for
`APIFY_PRICE_PER_RESULT_USD` (default 0.005, override), `APIFY_MAX_ITEMS_PER_RUN`
(default 100, malformed → `ValueError`, matching the existing
`APIFY_MAX_ITEMS` test), and the new `APIFY_RUN_TIMEOUT_SECS` default of 60.

### 4. Docs and configuration

- **`.env.example`** (Apify block, lines 17–23): point `#APIFY_ACTOR_ID` at
  the new actor, change `#APIFY_RUN_TIMEOUT_SECS` to `60`, and add
  `#APIFY_PRICE_PER_RESULT_USD="0.005"` and `#APIFY_MAX_ITEMS_PER_RUN="100"`.
  Note that the Apify Console monthly spend limit should be set to $5.
- **`CLAUDE.md`**: §6 actor bullet, §10 config table (four `APIFY_*` rows →
  six, new defaults), §11 test count, §12 replace the "rental" note with the
  pay-per-result model + per-result price + console spend limit. Also fix the
  stale "228 tests" in §4 (line 101) — the suite is 236 today.
- **`_docs/plan.md`**: §4.3 (AliExpress section) — new actor, payload, budget
  guard; §10 config rows; §11 test table (add `test_aliexpress_apify.py`);
  §13 operational note. Fix the stale "228 tests" in the header and §2.
- **`README.md`**: lines 130–131, the §1.5 config table (149–152), and the
  rental line 386. (README is already stale elsewhere — fix only the Apify
  lines, don't rewrite the file.)
- **`../my-store-build/_docs/backlog.md`**: mark **D3b** resolved (actor
  pinned: `cryptosignals/aliexpress-scraper`) and **D3c** resolved (direct
  Apify REST API via `apify-client`, not MCP). Leave D3d's tagging work
  alone.

---

## Verification

1. **Hermetic suite** (the agreed bar — no live spend):
   ```bash
   source .venv/bin/activate && pytest tests/ -q
   ```
   Expect 236 existing tests plus the new AliExpress tests, all green.
2. **Grep sanity**: no remaining `logical_scrapers` reference in `src/`;
   no remaining caller of `build_search_url`; `tests/test_main.py` chain
   assertion still passes unchanged.
3. **Import check**: `python -c "import src.main"` after the rewrite.
4. **Optional, operator-run live smoke** (not part of this build; spends
   money): with the Apify Console monthly spend limit set to $5,
   ```bash
   python -m src.main --keyword "kitchen gadgets" --target-count 1 --extractor aliexpress
   ```
   Expected cost ≈ 20 items × $0.005 = $0.10 per keyword run.

**What hermetic tests do and do not prove.** They prove the wiring, the
payload shape, the field mapping, the guards, and the failure isolation.
They **cannot** prove live AliExpress yield, because no network call is made.

## Risks and notes

- **Residential proxies.** The actor's own documentation states "residential
  proxies are required for consistent results", while free-tier credit only
  covers datacenter proxies reachable from the API. Live runs may therefore
  hit a captcha and return nothing. That surfaces as `ExtractorBlockedException`
  and degrades gracefully to the next extractor in the chain — it does not
  crash the pipeline. If live yield turns out to be poor, the fallback is the
  `kawsar/aliexpress-search-scraper` variant ($6/1,000 results, whose
  `additionalImages` array may supply the ≥3-image gallery natively); the
  per-actor `build_run_input` / item-mapping seam introduced here makes that
  a contained change.
- **Behaviour change:** the default run timeout drops 300s → 60s. A slow
  actor run now times out sooner and falls through the chain. Intentional
  (spec §3), but worth knowing.
- **Single-image source.** The actor returns one `imageUrl`, so the
  Playwright carousel upgrade remains load-bearing for the hard 3-image gate —
  unchanged from today's behaviour.
- **The spec file itself** remains inaccurate about payload shape and pricing;
  this plan implements against Apify's verified schemas instead. Consider
  correcting the spec file so it does not mislead a future run.
