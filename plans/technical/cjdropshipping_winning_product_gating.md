# Agent Execution Directive: CJdropshipping Quantitative "Winning Product" Gating

> **Tracked** as of 2026-10-12 (`plans/technical/`). A plain-English version of
> this directive is at
> `plans/simplified/cjdropshipping_winning_product_gating_simplified.md`.
> The gate itself is live: `CLAUDE.md` §6.2 (commercial gate), §6.3 (freight
> quote) and §8 (`shipping_notice_au` derivation in code).

> **Amended 2026-09-23 after the Phase 1 payload spike.** The original
> directive specified a dual-metric gate on historical sales (`sellNum`) and
> listing count (`listedNum`), with `MIN_CJ_SELL_COUNT` (default `50`).
> **The spike disproved the sales leg: CJ's MCP surface publishes no
> historical-sales figure under any name, on any of its 62 tools.** The
> directive is therefore amended to a single-metric gate. `MIN_CJ_SELL_COUNT`
> is abandoned outright — it must not be added to config, the `.env`
> ecosystem, or `CLAUDE.md`, and the extractor must not parse it or
> fail-close on it. Enforcing a permanently-absent metric under the repo's
> fail-closed rule would have dropped every CJ product.
>
> **Amended again 2026-09-23 after the first live export review (run.log +
> product-06).** Two defects were found in shipped output: CJ shipping was
> hardcoded to zero, so `estimated_cogs_aud` omitted freight entirely and the
> margin gate judged an understated cost; and `shipping_notice_au` was
> LLM-authored free text, which had invented the claim "Free standard
> shipping on this item" against a real freight cost of USD 9.37–12.53.
> Phases 6 and 7 below close both.

## Objective
The CJdropshipping pipeline (`cj_mcp_extractor.py`) relied solely on
logistical liveness gates (inventory > 0, active status) and enforced no
commercial viability. It now mirrors the AliExpress pipeline's architecture —
a strict quantitative "winning product" gate applied before the LLM, plus a
real landed-cost basis — using the single demand metric CJ actually publishes.

## Phase 1: Payload Schema Spike (complete — 2026-09-23)
Live `search_products` and `get_product_detail` calls were executed against
the StreamableHTTP MCP endpoint. Findings:

* **`listedNum` is verified.** Present and numeric on 10/10 search hits on
  `garlic grater` (15–1071) and 10/10 on `kitchen gadgets` (440–4175), and on
  the detail record too. It is how many dropshippers have imported the
  listing.
* **No sales metric exists.** `sellNum`, `sellCount`, `sales`, `soldNum`,
  `soldCount`, `orderNum`, `importNum` are absent from both records; no tool
  among the 62 advertised carries market sales (the order tools report the
  *operator's own* orders). `get_product_reviews` exposes a real review count
  but it is sparse (3 on a 44-listed item) and ratings-based.
* **`variantVolume` is a decoy** — the variant's volumetric freight dimension
  in mm³, never a sales figure.
* **Freight is quotable.** `calculate_freight` and `calculate_freight_tip`
  return 15 live AU methods for a sampled listing; CJPacket Eub is the
  cheapest at USD 9.37 (6–10 days), rising to DHL Official at USD 33.54.

## Phase 2: Configuration & Documentation
* `src/config.py` + `.env.example` + `.env`: `MIN_CJ_LISTED_COUNT`
  (default `150`). **No `MIN_CJ_SELL_COUNT` key.**
* The `.env` ecosystem must declare identical keys in identical order —
  `test_config.py` enforces this.
* `CLAUDE.md`: §10 table row, and the gate documented as §6.2 with a §6
  bullet reference. §6.2 text must respect §5.0: `listedNum` is a
  *commercial* proxy and a liveness non-discriminator.

## Phase 3: Gate Implementation in `cj_mcp_extractor.py`
Intercept the hits returned by `search_products` *before* the
`get_product_detail` loop:
* Drop and log `Skipping <ID>: Insufficient CJ list count (<count>)` when
  `listedNum < MIN_CJ_LISTED_COUNT`.
* Drop and log `Skipping <ID>: Insufficient CJ list count (unavailable)` when
  `listedNum` is missing/`None`/`""` — inverted tolerance.
* `logger.warning`, mirroring the AliExpress gate's level. The gate runs
  before the liveness pre-gate, so a commercially weak hit costs nothing.
* Key vocabulary lives in `cj_mcp_client.py`; the policy check is
  `CjMcpExtractor._passes_winner_gate`.

## Phase 4: Performance Sorting
Sort surviving hits descending by `listedNum` before the detail loop, and log
the survivors' counts at INFO strongest-first so the ranking is observable in
a run's own log. The pool is the search page CJ already returned (capped at
`CJ_MAX_PRODUCTS`), so this re-ranks that page, not CJ's catalogue.

## Phase 5: Testing & Delivery
* Fixtures carry `listedNum`; cover pass-at-floor, drop-below, drop-unreported,
  both documented logs, no detail call for a gated-out hit, descending
  ranking, and the configured floor.
* Default + env-override tests for `MIN_CJ_LISTED_COUNT`.

## Phase 6: Real Freight Quotes (complete the landed cost)
Shipping is half of a landed cost and CJ was reporting it as zero, so the
margin gate judged a figure roughly a quarter of reality and exported a
product that fails the gate once freight is included.

* **Quote source:** `calculate_freight` for the listing's cheapest variant
  (`endCountryCode` = `TARGET_COUNTRY`, origin CN), which returns every
  method with `logisticName`, `logisticPrice` (USD) and `logisticAging`.
  Fall back to `calculate_freight_tip` (weight + logistics attributes) when no
  variant id is available; it quotes the same catalogue with `discountFee` as
  the USD price and `option.enName` as the method.
* **Method choice:** `CJ_FREIGHT_METHOD` pins a method by name; blank means
  the cheapest. A pin that is not offered logs and falls back to the cheapest.
* **Fail-closed:** a listing with no usable quote is dropped
  (`Skipping <ID>: freight quote unavailable`), because an unquoted shipping
  figure is exactly the understatement this phase exists to remove — the same
  inverted tolerance §5 and §6.2 apply.
* `shipping_cost_aud` becomes real, so `estimated_cogs_aud`, the basis note
  and both legs of the margin floor all judge the true landed cost.

## Phase 7: Close the invented-shipping-claim hole
`shipping_notice_au` was LLM-authored and shipped a false customer-facing
promise ("Free standard shipping on this item") while the same file admitted
shipping was treated as zero.

* Remove `shipping_notice_au` from the LLM's response model and from the
  prompt's marketing list, adding it to the never-authored contract alongside
  `supplier_name` / `supplier_retail_url` / `estimated_cogs_aud` /
  `cogs_estimation_basis`.
* Derive it in code in `ProductCandidateEvaluation.from_raw` from the real
  quote — method and transit window — making no cost claim the pipeline cannot
  substantiate.
* Carry the quote's method and transit window on `RawSupplierProduct` so the
  derived line, and the cost basis beside it, are auditable.
