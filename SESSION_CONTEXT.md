# SESSION_CONTEXT.md — AliExpress Dropshipping Center ingestion

Companion to `CLAUDE.md`. Covers the native AliExpress ingestion path, the
quantitative winning-product gate, and the optional saved session.

## 1. Why the Apify path was retired

The previous AliExpress extractor ran a managed Apify actor against
unauthenticated consumer search pages. Three problems made its data
unusable for costing:

* **Fake "welcome deal" prices.** An unauthenticated, new-user context is
  fed subsidised SuperDeals prices. One exported package was costed at
  AUD 1.53 while the DS Center quotes **US $7.22 (AUD 11.19)** for the same
  item — a cost basis off by roughly 7x, which the LLM then marked up into a
  hallucinated retail price.
* **Wrong market.** The actor priced against a US ship-to context and
  ignored `--country AU`, so logistics were validated for the wrong market.
* **Per-result billing**, with a cold-container start that could exceed the
  configured run timeout.

## 2. The native extractor (`src/extractors/aliexpress_ds.py`)

`AliExpressDsCenterExtractor` reads the DS Center's own MTOP APIs through a
Playwright context's request jar — the same internal calls its React UI
makes — so no HTML scraping and no SPA driving:

| Call | Purpose |
|---|---|
| `mtop.aidc.ds.center.selection.search` | catalogue search, `sort=ORDERS_DESC` |
| `mtop.aidc.ds.center.selection.queryByItemUrl` | one item's record: price, orders, rating |

Both use MTOP's token-then-sign handshake (`md5(token&t&appKey&data)`,
appKey `12574478`); the first call primes the `_m_h5_tk` cookie and is sent
unsigned, the second is signed. Responses are JSONP-tolerant.

**The market pin is load-bearing.** The context carries AliExpress's
ship-to cookie (`aep_usuc_f`, `region=<run country>`). The same item is
quoted `US $5.23` for the DS Center's default market and `US $7.22` for AU,
and the catalogue itself differs — without the cookie the cost basis is
wrong again.

**The saved session is optional.** Anonymous access returns byte-identical
data, so `ALI_DS_STATE_PATH` is injected only when the file exists and its
absence is never an error. A session/auth refusal (`FAIL_SYS_SESSION_EXPIRED`,
`FAIL_SYS_USER_VALIDATE`, `FAIL_SYS_ILLEGAL_ACCESS`) or a login-page redirect
raises `DsCenterSessionExpiredError`, which the CLI renders as a human-
intervention block with the recovery steps.

**Currency guard.** The record carries the exact figure in minor units plus
its currency. USD is converted with `USD_TO_AUD`; AUD is taken as-is; any
other currency is skipped rather than mispriced.

## 3. The winning-product gate

Enforced in the extractor, before the LLM or any image work, on every
search hit's item record:

| Config | Default | Drop behaviour |
|---|---|---|
| `MIN_DS_ORDER_COUNT` | `500` | logs `Skipping <ID>: Insufficient order volume (<count>)` |
| `MIN_DS_RATING` | `4.5` | logs `Skipping <ID>: Rating too low (<rating>)` |

Orders arrive as display text (`"148 sold"`, `"10000+ sold"`) and are read
at their floor; rating is the record's `score`. **A metric the DS Center
does not report is treated as unproven and the item is dropped** — the same
inverted tolerance the CJ liveness gate applies to unverifiable stock.

Survivors are sorted by order volume descending, so the target count fills
with the strongest sellers first.

## 4. Imagery

The DS Center record carries a single `itemMainPic`, so each survivor's own
PDP is harvested once for its full carousel gallery and meta description
(`_GALLERY_JS` / `_META_DESCRIPTION_JS`, same probes the retired path used).
Fewer than 3 distinct gallery URLs after that pass means the candidate is
dropped — never fabricated, no fallback tier.

## 5. The optional session script (`scripts/generate_ali_session.py`)

```bash
source .venv/bin/activate && python scripts/generate_ali_session.py
```

Opens a non-headless stealth Chromium (Playwright's bundled build — the
operator's own Chrome profile and tabs are untouched), lets the operator log
in and open the Dropshipping Center by hand, then writes the context's
`storage_state` to `ALI_DS_STATE_PATH`. Needed only if AliExpress starts
requiring a login for the calls above.

## 6. Margin floor

The deterministic half of evaluation gate 2 lives in `models._enforce_accept_gates`
and `llm_filter._reconcile`, both reading the same config:

* `MIN_MARKUP_MULTIPLIER` (default `2.5`) **or** `MIN_MARGIN_AUD`
  (default `20.0`) must be cleared against the real landed cost, or the
  ACCEPT is downgraded to REJECT.

Relaxed from the original 3.0x / AUD 25 alongside the pricing prompt rework:
a true DS Center cost is far higher than the retired path's welcome-deal
prices, so a blind 3x on real cost over-prices the store. The prompt now
asks for realistic Australian retail pricing in the "Modern Arab-Aussie
Lifestyle & Cultural Nostalgia" niche and rejects a product whose realistic
price cannot clear the floor.

## 7. Tests

`tests/test_aliexpress_ds.py` (hermetic, zero network) covers the payload
decoding (plain and JSONP), order/rating parsing, the currency guard and
AUD conversion, the winning-product gate and each documented drop log, the
MTOP priming-then-signed handshake, ordering by order volume, dedupe across
keywords, session-expiry and blocked-exchange paths, the optional state
file, and the PDP gallery/description harvest. `tests/test_config.py`
covers every new key.

Live check (2026-09-21, read-only): `garlic grater` → 20 search hits →
18 gated out (18 below the order floor or rating floor) → 2 kept, both
harvested with 13 gallery URLs; a full pipeline run exported one package at
a real DS cost of AUD 3.86 (the other candidate was REJECTed by the LLM at
2.01x markup — the realistic-pricing instruction working as intended).
