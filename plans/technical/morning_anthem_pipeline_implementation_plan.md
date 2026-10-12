# Implementation Plan — The Morning Anthem Multi-Step Pipeline (Global, CJ-Only, HasData-Only)

**Status:** implementation spec for the coding agent. **Tracked** — `plans/` is
tracked as of 2026-10-12 (operator decision) and split into `technical/` (this
file and the CJ gate directive) and `simplified/` (plain-English walkthroughs of
the same). Written 2026-10-09 against branch
`feature/morning-anthem-pipeline-update` at `a346565` (main @ HasData
Google Shopping source); **§1 and Step 2 are now implemented and live-piloted**
— see §13.8 of `CLAUDE.md` for the current state, and
`plans/simplified/morning_anthem_pipeline_simplified.md` for the plain-English
version.

**Supersedes:** `plans/updated_multistep_pipeline_implementation_plan.md`
(the Sabaah Goods / AU-only / dual-supplier / Apify-optional plan) **in full**
for the multi-step gold-kernel pipeline. The supplier ingestion **core**
(CJ MCP extractor, liveness gate, commercial gate, freight quote, LLM
viability gate, image gate, exporter) is inherited, not rebuilt — this plan
changes *what feeds it* and *what it is pointed at*, not how it works.

**Brand context:** the store is rebranding from **Sabaah Goods** (Australia-only
Arab-Aussie niche) to **The Morning Anthem** — a **global, premium morning
lifestyle** brand. Every AU-specific constraint in the codebase is retired;
every step is re-scoped to global demand evidence and the five Morning
Anthem merchandising pillars.

---

## LAUNCH OVERRIDES (2026-10-12, operator) — "Australia First, Expand Globally Later"

**These override the body of this plan wherever they conflict.** The store
rebrands to The Morning Anthem across the same five pillars, but its public
launch **sells and ships to Australia only**, to validate orders, transit
times and unit economics before opening Shopify Markets.

1. **AUD is RETAINED — §1.3's USD rebase is overridden.** The Shopify store
   prices, carts and customer payments are strictly in AUD, so
   `USD_TO_AUD` stays and CJ's USD list price and quoted freight are converted
   to AUD to verify landed margins. The data models and the export contract
   keep their AUD fields (`suggested_price_aud`, `estimated_cogs_aud`,
   `projected_margin_aud`) and the margin floor stays `MIN_MARGIN_AUD`
   (default `$10.00`, the operator's ≥20%-margin target). Every `_usd` rename
   in §10.2 is therefore **cancelled**.
2. **Freight destination is `AU`.** `CJ_DEFAULT_DEST_COUNTRY="AU"`: all launch
   products ship from CJ's China warehouse into Australia, and the freight
   quote must name an Australian lane (e.g. CJPacket EUB). `shipping_notice_au`
   is retained, worded "Standard tracked international shipping to Australia
   from China: {transit}."
3. **Demand geography is AU.** `TRENDS_GEO="AU"`, `SHOPPING_GL="au"`,
   `SHOPPING_DOMAIN="google.com.au"`, so demand indicators and price
   benchmarks reflect Australian shopper behaviour and AUD retail pricing.
   §1.3's `gl=us` default is cancelled for the launch.
4. **The AliExpress and Apify removals stand** (§1.1, §1.2): CJ is the sole
   supplier and HasData the exclusive data provider.
5. **The five Morning Anthem pillars stand** (§0), and all Sabaah Goods /
   Arab-Aussie legacy copy is stripped from the prompts, the Jev choices and
   the models.
6. **Step-2 Trends research is batched**: up to 5 comma-separated terms per
   call, ≤ 10 calls per pass.

### THE LAUNCH RUN (one-off, operator decision 2026-10-12)

The **first full run is a deliberate one-off** that front-loads the whole
month's research into one gold list, on the reasoning that the CJ side of the
pipeline is credit-free (CJ's MCP is the supplier's own API) so the gold list
is the only thing that costs, and it is worth buying once, properly:

| Stage | Shape | Credits |
|---|---|---|
| Step 2 — Trends research | ≤ 10 calls (up to 5 terms each) | **≤ 100** |
| Step 3 — Google Shopping | **30 keywords** × 1 request × 10 | **300** |
| **Total** | | **≤ 400** |

`HASDATA_PRODUCTION_CAP` is therefore **400** and `HASDATA_GS_MAX_KEYWORDS`
**30** for the launch run (both are config keys, so this is a numbers change,
not a code change). Against the 1,000-credit monthly free tier that leaves
~600 credits of headroom for the pilot/re-run contingency this plan's
per-step protocol depends on.

**Where the 30 come from — Jev selects them, not a hand-pick.** Step 2's
research yields ~60 usable candidate strings and the scrape can afford 30, so
the narrowing is a **decision, not a judgement call**: it is made by Jev
(TypeSafe System One) in a second Step-2 pass, exactly the way Step 6 ranks
products. A decision model is the right instrument for "pick the best N of a
scored set" — it is consistent run to run, it can be given a compact factual
context per candidate, and it returns a score rather than prose, so the cut
itself stays deterministic code.

**Jev's two Step-2 objectives, stated:**

| # | Objective | Where it is enforced |
|---|---|---|
| **A** | **Filter out non-products and irrelevant Trends strings.** A trend that is not a shopper looking for a physical product must never cost a Step-3 credit. | The **gate** pass (`trend_gate.py`): the four-way intent verdict (`product_search` / `informational` / `news_or_brand` / `service_or_digital`) plus the deterministic banned-token screen. |
| **B** | **Pick the best 30 of the ~60 usable candidates for the Morning Anthem niche, ACROSS all five pillars, representing the researched pool — so the outcome is as close to pillar-unbiased as the data allows.** | The **selection** pass (below): a per-candidate quality score from Jev, then a **proportional pillar quota** in code. |

Objective B is deliberately split between model and code, and that split is the
design:

* **Jev judges one candidate at a time** — is this a good sourcing keyword for
  *its* pillar? A per-candidate score cannot see the pool, so balance is not a
  judgement it can make; asking it to balance would also make the outcome depend
  on how candidates happen to fall into batches.
* **Code owns representation** — each pillar gets a quota proportional to how
  many candidates it supplied, with a floor, so the selection mirrors the
  researched demand instead of the loudest pillar's score distribution.

**The pass (Step 2.2 — selection):**

* Every gated survivor gets **one score question**, `<i>__keyword_quality`,
  along five ordered levels that measure one thing: how good the string is as a
  **supplier-sourcing keyword** for its pillar. The levels span *"unlikely to
  find a supplier listing — too vague, a brand or retailer term, or no clear
  physical product"* to *"specific and physical, demand evidence strong and
  rising, squarely inside its pillar"*.
* **The state carries everything the decision can use**, because a decision
  model is only as good as its context — this is the lesson the Step-6 ranker
  already encodes. Per candidate: the query, its pillar, its Trends demand
  evidence verbatim, and whether that evidence is a 5-year mean or a
  rising/related-query figure. Once per call: the brand line (The Morning
  Anthem, Australia-only launch, CJdropshipping as the target catalogue), the
  task statement, how many are being selected, and the five pillar
  descriptions.
* **The cut is code, not the model — and it is representative by
  construction.** Each pillar's **quota** is its proportional share of the
  target, given a floor of `TREND_SELECT_MIN_PER_PILLAR` (2) and never more
  than the pool actually holds, reconciled to `TREND_SELECT_TARGET` (30) by
  largest remainder; within each pillar the best-scoring candidates take the
  quota, and any shortfall fills by score across the whole pool. So a pillar
  that supplied 40 of 60 candidates takes ~20 of the 30 slots rather than all
  30, and no pillar that supplied candidates is dropped. Ties break on
  has-evidence then keyword string, making a selection reproducible. Scoring is
  capped at `TREND_SELECT_BATCH_SIZE` (12) candidates per call, inside the
  ~13-question envelope the vendor documents. `--no-pillar-quota` restores a
  pure best-of; a floor that cannot fit the target **raises** rather than
  silently overspending the Step-3 budget.
* **Failure containment mirrors the rest of the pipeline:** a failed call
  retries once, then that batch's candidates score as unclassified, rank last
  within their pillar, and are only taken if slots remain. If **nothing**
  scored, **nothing** is selected and the runner halts — scraping an unranked
  pool would spend the whole Step-3 budget on no basis.
* **Jev authors nothing.** It returns a number per candidate; the keyword,
  pillar and evidence are carried through verbatim.

The selection lands the best 30 in the Step-2 deliverable (Step 3's intake) and
preserves the full gated pool beside it, so the remaining ~30 stay available as
the re-run reserve without paying for trends again.

**Pilot evidence (2026-10-12, live, 3 Jev calls over 13 candidates).** The gate
kept 12; the selection scored them:

| Score | Verdict | Keyword |
|---|---|---|
| 4.94 | selected | `hand held milk frother` |
| 4.70 | selected | `matcha whisk` |
| 4.51 / 4.00 / 3.67 / 3.67 | reserve | `milk steamer and frother`, `coffee milk frother`, `coffee frother`, `milk frother machine` |
| 3.24 / 3.23 / 3.11 | selected (quota) | `incense burner`, `silk eye mask`, `garlic press` |
| 2.80 | reserve | `nespresso frother` |
| 2.24 | reserve | `best milk frother` |
| 1.56 | reserve | `breville barista express` |

Two findings this settled:

1. **The brand-query problem is solved here, not by a blacklist.** The brand and
   comparison strings did not merely miss the top N — they scored at the
   *bottom* of the pool (`breville barista express` lowest of all twelve),
   because a brand-plus-model query is a poor sourcing keyword.
2. **The quotas behave as intended.** Three lower-scoring rows from three
   different pillars entered on their share while four higher-scoring
   `brew_fuel` rows stayed in reserve.

**Second Trends call (2026-10-12, 10 credits) — The Sunrise Move is NOT
trend-dead; the earlier single seed was simply too narrow.** One AU call with
five terms, chosen as head-term/specific pairs:

| Term | 5-y mean | weeks with data | peak |
|---|---|---|---|
| `beach towel` | 25 | 262/262 | 100 |
| `dog leash` | 16 | 262/262 | 30 |
| `sand free towel` | 2 | 130/262 | 13 |
| `commuter backpack` | — | 16/262 | 2 |
| `hands free dog leash` | — | 8/262 | 3 |

The pillar carries real, measurable AU demand at its **head terms** and almost
none at its **phrase-level product names**: Google Trends reports head terms,
not long-tail phrasings, and AU is a small market, so a specific phrase falls
under the reporting floor — which is why the pilot's lone `commuter backpack`
seed read as "no demand". It cuts both ways: the brew-heavy pilot pool looked
strong partly because those seeds happened to be head terms.

**Seeding rule for the production Step-2 pass: seed Trends with head terms
(`beach towel`, `dog leash`), not phrase-level product names.** The long-tail
variants belong downstream as Step-4 supplier keywords, once demand is proven.

**Expected yield: ~55 gold products** (~2 per keyword, the ratio the pipeline's
own live runs have produced). Those become the reference set Steps 4–6 chase:
the Step-4 bank targets each gold product by its **exact title first**, and
Step 5's CJ ingestion — free in credits, bounded only by pacing — loops over
those keywords until it finds the identical product or a close equivalent.

**Honest limits on "loop until we find all 55":** CJ is credit-free, not
cost-free or instant. Each keyword costs up to `CJ_MAX_PRODUCTS` product-detail
round trips plus a freight quote each (minutes per keyword; the 320-keyword
run took ~11 h), the LLM viability gate bills tokens per candidate, and some
gold products genuinely have no CJ equivalent — the funnel records those as
`keywords_empty` rather than inventing a match. The target is therefore
"find the equivalent wherever CJ has one", not a guaranteed 55.

The body below records the original global-scope design. Where it says USD,
read AUD; where it says `gl=us`, read `au`/`google.com.au`; where it renames a
field to `_usd`, the field keeps its `_aud` name. The removal inventories,
the pillar set, the Jev gates, the budget arithmetic and the execution
protocol all stand unchanged.

---

## 0. The five Morning Anthem pillars (single source of truth)

Every gated decision downstream (Step 2 tagging, Step 3 Jev pillar gate,
Step 4 keyword typing, Step 6 Jev ranking) reads **these exact five pillar
keys**. They live as one importable constant tuple in
`src/ranking/jev_client.py` (the "judgement in one place" rule) and are
referenced everywhere else — never re-spelled.

| Pillar key | Pillar name | Expected product lines |
|---|---|---|
| `brew_fuel` | **The Brew & Fuel** (core) | Manual coffee tools, espresso distribution gear, portable milk frothers, minimalist tumblers, matcha accessories |
| `sunrise_move` | **The Sunrise Move** | Aesthetic active gear, commuter backpacks, waterproof pouches, hands-free dog leashes, sand-free towels |
| `mindful_awakening` | **The Mindful Awakening** | Backflow incense burners, digital gratitude journals, shower steamers, meditation accessories |
| `restful_reset` | **The Restful Reset** | Silk eye masks, ergonomic recovery pillows, sunrise alarm clocks, facial ice rollers |
| `morning_table` | **The Morning Table** | Cutting boards, garlic presses/mashers, breakfast preparation and serving kitchen aids |

There is **no `other` and no `discard` escape in Step 3's pillar gate**: a
gold product must land in exactly one of these five. (`discard` remains a
valid Jev *choice* answer in Step 6's per-package classification — Step 6
ranks what Step 5 exported and may still flag a misfit — but the Step-3
gate that *admits* a product into the gold list is five-way, closed.)

The banned-token boundary is unchanged in spirit (AICIS inert-goods +
no-cosmetics + no-consumables + no-IP), but the **retail-saturation tokens
are re-scoped from Australian big-box names to global commodity signals**:
`kmart`, `bunnings`, `coles`, `woolworths`, `big w` are replaced by
`walmart`, `target`, `amazon basics`, `ikea`, `kmart`, `target australia`,
`bunnings`, `coles`, `woolworths` — i.e. the *global* mass-retail set. The
full tuple lives in `src/keywords/generator.py::BANNED_TOKENS` and is
updated there (one source of truth).

---

## 1. Retire / rewrite / keep inventory

### 1.1 Remove outright (AliExpress — CJ-only mandate)

| File | Why |
|---|---|
| `src/extractors/aliexpress_ds.py` | AliExpress Dropshipping Center extractor — retired. CJ is the exclusive supplier. |
| `src/extractors/base.py` | Keep the file (CJ still subclasses it) but the docstring/examples referencing AliExpress are rewritten. |
| `scripts/generate_ali_session.py` | AliExpress session generator — no longer needed. |
| `tests/test_aliexpress_ds.py` | The AliExpress test module — deleted with the extractor. |
| `plans/AliExpress Dropshipping Center Gating.md` | Historical gate record — archive-worthy but no longer load-bearing; keep on main, not touched. |
| `plans/future_addressing_aliexpress_ip_blockage.md` | Same. |

**Config keys removed from `src/config.py`, `.env`, `.env.example`:**
`ALI_DS_STATE_PATH`, `ALI_DS_MAX_PRODUCTS`, `MIN_DS_ORDER_COUNT`,
`MIN_DS_RATING`.

**Registry changes:**
- `src/main.py::_EXTRACTOR_REGISTRY` → `{"cjdropshipping": CjMcpExtractor}` only.
- `src/config.py::Settings._parse_supplier_order` default → `["cjdropshipping"]`.
- `src/config.py::KNOWN_SUPPLIERS` / `_SUPPLIER_PDP_PATTERNS` in `models.py` → CJ only (the `AliExpress` `/item/<id>.html` pattern is removed).
- CLI `--extractor` choices → `{auto, cjdropshipping}` (auto now has exactly one engine; the chain machinery stays for future suppliers).
- `src/pipeline/keyword_bank.py::BANK_ENGINES` → `("cjdropshipping",)` and `OPTIMAL_SUPPLIER_DIRS` → `{"cjdropshipping": "cjdropshipping"}`.
- `src/ranking/jev_product_ranker.py::SUPPLIER_DIRS` → `("cjdropshipping",)`.

### 1.2 Remove outright (Apify — HasData-only mandate)

| File / key | Why |
|---|---|
| `src/extractors/google_shopping.py` (the Apify actor wrapper) | Apify is no longer used anywhere in the pipeline. |
| `APIFY_TOKEN`, `APIFY_GS_ACTOR`, `APIFY_GS_MAX_RESULTS_PER_KEYWORD`, `APIFY_GS_MAX_CHARGE_USD` config keys | Removed from `config.py`, `.env`, `.env.example`. |
| `pyproject.toml` `apify-client` dependency | Removed. |
| `.mcp.json` `apify-trends` server | Removed (Steps 2 & 3 use HasData exclusively). |
| `tests/test_google_shopping.py` | The Apify scraper test module — deleted with the wrapper. |

**Note:** `HASDATA_API_KEY` stays — it is now the *sole* scraping credential
and is shared (as today) between the Step-2 Trends MCP (`.mcp.json` via
`scripts/mcp_headers.py`) and the Step-3 Shopping REST client. The
`google-trends` HasData MCP server in `.mcp.json` stays (Step 2 still
uses it).

### 1.3 Remove outright (AU localization — global mandate)

| Item | Replacement |
|---|---|
| `USD_TO_AUD` config key + all `price_aud` / `*_aud` field names | **USD becomes the pipeline's working currency.** Fields rename to `price_usd`, `estimated_cogs_usd`, `projected_margin_usd`, `suggested_price_usd`, `MIN_MARGIN_USD`. The static `USD_TO_AUD` conversion is deleted. (Shopify can present local currency at storefront; the pipeline costs and prices in USD.) |
| `MIN_MARGIN_AUD` | → `MIN_MARGIN_USD` (default **10.0** — the operator's $10 target, now in USD). |
| `TARGET_COUNTRY="AU"` | Removed. CJ's freight quote destination becomes a **configurable** `CJ_DEFAULT_DEST_COUNTRY` defaulting to `US` (the largest single market for a global morning brand; the operator can override per run). The `aep_usuc_f` AU market pin concept dies with AliExpress. |
| `shipping_notice_au` field + `_UNQUOTED_SHIPPING_NOTICE` "Ships to Australia" | → `shipping_notice` — "Standard tracked international shipping to {dest} via {service}: {transit}." and "Ships to {dest} from the supplier." Destination is interpolated from the quote, never hardcoded AU. |
| `cogs_estimation_basis` "AUD $X… shipping to AU" | → "USD $X… tracked shipping to {dest}". Zero-URL rule unchanged. |
| `llm_filter.py::_SYSTEM_PROMPT_TEMPLATE` AU/Arab-Aussie niche text | Rewritten for The Morning Anthem (see §5). |
| `gold_curator.py` "Australian e-commerce market" / "purchased by Australians" / "AU demand" / "AUD $10" | Rewritten for global demand (see §4). |
| `google_shopping_hasdata.py` `GOOGLE_DOMAINS`/`gl=au`/`domain=google.com.au` AU pin | → global: `gl=us`, `domain=google.com` by default (configurable `SHOPPING_GL` / `SHOPPING_DOMAIN`), since the brand is global and US Google Shopping carries the deepest demand signal. |
| `models.py` field docstrings referencing AUD/AU | Swept. |
| `keyword_bank.py` "AU market pin" docstring | Swept. |
| `jev_client.py` PILLAR_OPTIONS `curated_home`/`self_care_rituals`/`other`/`discard` | → the five Morning Anthem pillars + `discard`. |
| `keywords/generator.py::PILLARS` | → the five Morning Anthem pillar keys. |
| `gold_curator.py::PILLARS` | → the five Morning Anthem pillar keys. |
| Exporter `metadata.json` keys `suggested_price_aud` / `estimated_cogs_aud` / `projected_margin_aud` / `shipping_notice_au` | → `suggested_price_usd` / `estimated_cogs_usd` / `projected_margin_usd` / `shipping_notice`. The 13-key contract is preserved in *count*, renamed in currency. |
| `CJ_FREIGHT_METHOD` "CJPacket Eub" AU-centric example | Kept (method pinning is still valid); docstring updated to a global-default example. |
| CLAUDE.md §3 gate-2 "Australian retail" / §8 export contract AUD keys / §10 `USD_TO_AUD` / §13 pillar names | Swept in the doc pass (§10). |

### 1.4 Keep untouched (the supplier core)

`src/pipeline/cj_mcp_client.py` (MCP transport, liveness gate, freight
tools), `src/pipeline/image_sourcing.py`, the CJ commercial gate
(`MIN_CJ_LISTED_COUNT`), the MCP Payload Liveness Gate, the CJ freight
quote (§6.3), the 3-image gallery gate, the exporter's duplicate-scan +
numbering, the `run_pipeline` orchestration shape, the funnel counters,
the intervention-block format, the hermetic test scaffolding (conftest
scrub, MockTransport idioms).

### 1.5 New code to build

| File | Step | Purpose |
|---|---|---|
| `src/research/__init__.py`, `src/research/trend_gate.py` | 2 | Jev **product-intent gate** over the Step-2 trend strings (drop informational/news/brand queries). |
| `src/research/__init__.py`, `src/research/pillar_gate.py` | 3 | Jev **pillar gate** over the Step-3 candidate products (assign one of the five pillars or discard). |
| `scripts/gate_trend_keywords.py` | 2 | Step-2 Jev gate + selection runner. |
| `src/research/trend_select.py` | 2 | Jev **selection** pass: score every gated survivor, cut the best N deterministically. |
| `tests/test_trend_select.py` | 2 | Hermetic selection tests (batched scoring, the cut, the pillar floor, failure ranking, verbatim carry-through). |
| `scripts/curate_gold_products.py` | 3 | Step-3 Jev pillar-gate runner (replaces the LLM-curator-only path). |
| `tests/test_trend_gate.py` | 2 | Hermetic Jev gate tests. |
| `tests/test_pillar_gate.py` | 3 | Hermetic Jev pillar-gate tests. |

Plus the rewrites of §1.3 (renames) and the new Step-4 prompt resource
(`src/keywords/morning_anthem_keyword_prompt.md`).

---

## 2. Target architecture (Morning Anthem)

```
[Step 1] HasData Google Trends MCP (HasData only — Apify retired)
              │  global (gl=us default) trend strings per pillar
              ▼
[Step 2] LLM drafts niche trend strings  ──►  Jev PRODUCT-INTENT GATE
              │                                  (drop non-product queries)
              ▼
outputs/json/morning-anthem-step-2-trend-keywords.json   (filtered)
              │
              ▼  Step 3: HasData Google Shopping (REST, gl=us) per keyword
        scrape rows ──► LLM select-by-url curation
              │        ──►  Jev PILLAR GATE (5 Morning Anthem pillars)
              ▼
outputs/json/morning-anthem-step-3-gold-products.json   (gold standard)
              │
              ▼  Step 4: reasoning LLM — EXACT-TITLE-first keyword bank
        (verbatim product titles; LLM-tweaked similar-product fallback
         ONLY when the exact title yields nothing; physical-mechanics
         specificity; never generic)
              │
outputs/json/morning-anthem-step-4-keyword-bank.json
              │
              ▼  Step 5: CJdropshipping MCP ONLY — single-engine ingestion
        (commercial gate → liveness gate → freight quote →
         global viability LLM gate → margin floor → 3-image gate)
              │
              ▼
  my-store-build/inspiration/morning-anthem-candidates/cjdropshipping/product-NN/
              │
              ▼  Step 6: Jev ranks packages vs the gold list (global appeal)
outputs/json/morning-anthem-step-6-ranked-candidates.{json,md}
```

**Export root rename:** the gold-kernel tree moves from
`optimal-dropship-candidates/` to **`morning-anthem-candidates/`** (config key
`MORNING_ANTHEM_EXPORT_DIR`, default
`/Users/ahmadammari/PD/my-store-build/inspiration/morning-anthem-candidates`).
The single supplier subfolder is `cjdropshipping/`. (The old
`OPTIMAL_EXPORT_DIR` key is removed; the general `EXPORT_DIR` /
`dropship-candidates/` intake is untouched.)

**Deliverable naming:** all Step 2–6 deliverables are renamed with a
`morning-anthem-` prefix under `outputs/json/` + `outputs/md/`:
`morning-anthem-step-2-trend-keywords.{json,md}`,
`morning-anthem-step-3-gold-products.{json,md}`,
`morning-anthem-step-4-keyword-bank.{json,md}`,
`morning-anthem-step-6-ranked-candidates.{json,md}`.

---

## 3. Step 1 — Google Trends (HasData ONLY)

No Apify fallback. The `apify-trends` MCP server is removed from
`.mcp.json`; only the HasData `google-trends` server remains. Step 2's
research reads trends through it exactly as before, but with **global
geography**: `geo=US` (default; the configurable `TRENDS_GEO` key) over
weekly `today 5-y` series, related-queries pass for intent, coverage as
the demand screen, in-flight week excluded — the proven methodology.

`scripts/mcp_headers.py` keeps serving the HasData key to the trends MCP.
`HASDATA_API_KEY` is unchanged.

**Budget:** Trends calls are **5 HasData credits each** (published rate). The
Step-2 research is agent-driven (operator/agent session, not a repo runner),
so it is budgeted in §9's envelope as a line item, not gated in code.

---

## 4. Step 2 — Global trend strings + Jev product-intent gate

### 4.1 Research (no repo code, agent-driven)

The operator/agent runs the Step-2 prompt through the HasData Trends MCP
(global geography, five Morning Anthem pillars). Output is a list of
**candidate product-search strings** (not products), each tagged with a
pillar key from §0 and carrying its Trends demand evidence (coverage /
related-query intent). 15–40 raw candidates.

**Deliverable (untracked):**
`outputs/json/morning-anthem-step-2-trend-keywords.json`:

```json
{
  "keywords": [
    {"keyword": "manual milk frother rechargeable", "pillar": "brew_fuel",
     "demand_evidence": "coverage 78, rising 5-y (Google Trends, geo=US)"}
  ],
  "generated_with": "HasData Google Trends MCP, geo=US",
  "note": "Step 2 raw candidates; not tracked"
}
```

### 4.2 NEW GATE — Jev product-intent gate (`src/research/trend_gate.py`)

Every candidate string is routed through **Jev** to validate that the trend
actually represents a **product search** — not an informational query, a
news topic, a how-to, a brand controversy, or a service. Non-product strings
are **dropped** (never passed to Step 3).

**Design (reuses the §8.0-verified System One envelope):**

- One `decide()` call per batch of `TREND_GATE_BATCH_SIZE` (default **8**)
  candidate strings. State = `{"candidates": [<string>, …]}`.
- One **choice** question per candidate:
  `<i>__product_intent` with criteria:
  - `product_search` — "the string is what a shopper types to FIND and BUY a physical product"
  - `informational` — "a how-to, explainer, or research query, not a purchase intent"
  - `news_or_brand` — "a news topic, brand name, controversy, or person"
  - `service_or_digital` — "a service, software, course, or digital good"
- Answers keyed `<i>__product_intent`; only `product_search` survives. Every
  other verdict drops the string (logged with its verdict as the reason).
- A failed batch call retries once; a second failure drops that batch's
  strings with a `Jev batch call failed` note (the run never aborts on one
  bad call — same posture as the Step-6 ranker).
- Missing `OPENROUTER_API_KEY` → `JevConfigError` → intervention block.

**Anti-hallucination posture:** Jev returns only a classification per
string; the string itself, its pillar tag and its demand evidence are
carried through verbatim from the Step-2 file. Jev authors no keyword text.

**Runner:** `scripts/gate_trend_keywords.py`
`[--input …] [--batch-size 8] [--output …]`. Prints the planned call count
and the per-verdict tally; exits non-zero on a missing input or a
config error.

**Filtered deliverable:**
`outputs/json/morning-anthem-step-2-trend-keywords.json` (overwritten with
the *filtered* list — the raw list is preserved as
`…-raw.json` for audit) + the `.md` digest.

**Acceptance:** every surviving string is a Jev-confirmed `product_search`,
carries a valid §0 pillar and demand evidence, and contains no banned
token (§0).

---

## 5. Step 3 — HasData Google Shopping + Jev pillar gate

### 5.1 Scrape — HasData ONLY (`src/extractors/google_shopping_hasdata.py`)

The Apify actor wrapper is deleted; the HasData REST source becomes the
**only** Step-3 scraper (no `--source` flag anymore — there is one source).

- `gl=us`, `domain=google.com` by default (configurable via
  `SHOPPING_GL` / `SHOPPING_DOMAIN` — the global default is US, the deepest
  Google Shopping demand signal; the operator may point at `gl=au`,
  `gl=gb`, etc. without a code change).
- **One request per keyword = 10 credits.** One request returns the whole
  shopping grid (~65 rows observed); `HASDATA_GS_MAX_RESULTS_PER_KEYWORD`
  (default **20** for Morning Anthem — see §9 budget math) is a **local**
  slice bounding LLM curation cost only. The request count IS the keyword
  count, and the keyword count IS the spend.
- The runner **hard-caps the keyword count** at
  `HASDATA_GS_MAX_KEYWORDS` (default **20**) so a full production run cannot
  exceed the §9 budget. A run that would exceed the cap refuses to start
  (fail-closed before any credit moves).
- Fail-closed per keyword (non-200 / non-JSON / error status / empty grid
  fails the run), unchanged from the proven HasData module.
- The `--dump-raw` / `--from-raw` replay is preserved (a dump replays at
  zero scrape spend).

### 5.2 LLM select-by-url curation (`src/evaluators/gold_curator.py` — rewritten)

Same anti-hallucination contract (the LLM selects by verbatim `url` only;
every product fact is code-assembled from the rows; rows with neither
rating nor review count are filtered out before the LLM). Changes:

- The prompt is rewritten for **global** demand (no "Australians", no
  "AUD $10", no AU packet shipping).
- The curator no longer assigns the pillar itself — it selects rows and
  writes `compliance_note` / `unit_economics_note` only. The **pillar is
  assigned by the Jev pillar gate** (§5.3), not the LLM.
- `unit_economics_note` is now denominated in **USD** and the prompt's
  margin reference is the `MIN_MARKUP_MULTIPLIER` (2.5x) **or**
  `MIN_MARGIN_USD` ($10) floor, interpolated from config so prompt and code
  cannot drift.

### 5.3 NEW GATE — Jev pillar gate (`src/research/pillar_gate.py`)

Every curated candidate product is routed through **Jev** and evaluated
against the **five Morning Anthem pillars** (§0). Jev assigns exactly one
pillar, or `discard` when the product does not logically fit any pillar.
**Discarded products never reach the gold list.**

**Placement — settled by the operator 2026-10-12.** The gate runs **after** the
scrape and the LLM curation, never before it. A decision model is only as good as
its context, and a pillar needs *commercial* context to be classified honestly:
"is this a Restful Reset product?" is a very different question when the row
carries a price and 4.7/5 from 1,200 reviews than when it carries a bare title.
So the gate sees **enriched rows**, and the cheap scraper/curator stages run
first.

**Design:**

- One `decide()` call per batch of `PILLAR_GATE_BATCH_SIZE` (default **4**)
  candidate products. State =
  `{"pillars": {<key>: <one-line description>, …}, "products": {<i>: {name, description, retail_price_text, price_value, rating, review_count, demand_evidence, source_keyword}}}`
  — note the *numeric* `price_value`, `rating` and `review_count` alongside the
  raw `retail_price_text` and the formatted evidence line, so the model can
  reason over the numbers rather than parse a string. The curator's own pillar
  guess is deliberately **not** passed: it would bias the classification the
  gate exists to make.
- One **choice** question per product: `<i>__pillar` with criteria =
  the five pillar keys (each with its §0 product-line description) plus
  `discard` — "does not logically fit any Morning Anthem pillar".
- One **choice** question per product: `<i>__pillar` with criteria =
  the five pillar keys (each with its §0 product-line description) plus
  `discard` — "does not logically fit any Morning Anthem pillar".
- Only products whose answer is one of the five pillar keys survive;
  `discard` (or a missing/unparsable answer) drops the product with the
  verdict as the note.
- One retry on a failed call; a second failure drops that batch with a
  note (the run never aborts on one bad call).
- Jev authors **no product fact** — `name`, `url`, `price`, `merchant`,
  `demand_evidence` are all code-assembled from the scraped rows; Jev
  contributes only the pillar key, which is written into the deliverable by
  code.
- **The gate becomes the pillar's single author.** The curator stops assigning
  one (its prompt and schema drop the field): two independent pillar opinions
  in one pipeline is one too many, and only the gate's is informed by the
  numeric context above.
- **Fail-closed, in the pipeline's idiom:** if every product in a run is
  discarded, the runner halts with an intervention rather than writing an empty
  gold list that Step 4 would silently work from.

**Gold deliverable:** `outputs/json/morning-anthem-step-3-gold-products.json`
— every product carries a valid five-way `pillar`, on-page demand
evidence, a retail price, and its source keyword. The `.md` digest groups
by pillar.

**Runner:** `scripts/curate_gold_products.py` (scrape → LLM select →
code-assemble → Jev pillar gate → write). Prints the planned HasData
credit envelope and the Jev call count before the first billable call.

---

## 6. Step 4 — Gold-standard keyword bank (exact-title-first, CJ-only)

### 6.1 Strategy — exact title first, tight fallback only

The Step-4 prompt is rewritten (`src/keywords/morning_anthem_keyword_prompt.md`,
a tracked resource with the same `{PRODUCT_TABLE}` slot mechanism). The
strategy pivots from "broad + modifier catalogue strings" to
**exact-match-first**:

1. **Primary keyword = the verbatim gold product title.** The model copies
   the Step-3 `name` field **verbatim** as the product's primary supplier
   search keyword. CJ's catalogue search matches titles closely, so the
   exact title is the highest-precision query that exists.
2. **Fallback ONLY when the exact title yields no results.** For each
   product, the model may additionally author **one** fallback keyword —
   and only if it is a *tight* physical-mechanics/attribute variant of the
   exact title (material, mechanism, form factor, pack size). The fallback
   is the LLM's *only* authored keyword, and it must name the physical
   mechanics of the Step-3 winner — it must **never** be a generic head term
   or a loosely-coupled category.
3. **The keyword bank records which kind each keyword is.**
   `CandidateKeyword` gains a `match` field:
   `Literal["exact", "tight_fallback"]`.
   - `exact` — the keyword IS the verbatim product title (code-verified:
     the validator checks the string equals the source product's `name`).
   - `tight_fallback` — LLM-authored, must be specific to the physical
     mechanics/attributes of the product, must not be a generic category
     term, and must pass the banned-token boundary.

### 6.2 Validation (code-side, fail-closed — `validate_pool` extended)

- `exact` rows: the `keyword` string must equal the source product's
  `name` **verbatim** (case-insensitive, whitespace-collapsed). A mismatch
  is fatal — the exact-match contract is the point of the step.
- `tight_fallback` rows: 2–7 words, lowercase, no punctuation, no brand
  names, banned-token-free (§0), and **must share at least one
  content word** with the source product's title (a cheap code-side
  proxy for "specific to this product, not a generic category"). A
  fallback that shares no content word with the title is rejected as
  too loosely coupled.
- The broad/modifier + `tightens` pairing from the old bank is **retired** —
  the exact/fallback model replaces it. `role` and `tightens` are removed
  from `CandidateKeyword`.
- The pool band, per-product floor, duplicate-string fatality,
  cross-chunk fatality, carried do-not-repeat list, chunked generation,
  batched calls, JSON-tail salvage, and the `dedupe_keywords`
  neediest-first collapse all **stay** (they are the proven machinery).
- `pillar` is now one of the five §0 keys (Literal updated).
- `FIELDS` becomes `("keyword", "product", "pillar", "match", "rationale")`.

### 6.3 Pillar-balanced, type-diverse selection (unchanged machinery, new pillars)

`select_gold_products` (demand-ranked, one row per `source_keyword`,
pillar round-robin, `carries_banned_token` exclusion) is unchanged except
it round-robins across the **five** Morning Anthem pillars.

### 6.4 Runner

`scripts/generate_gold_keywords.py` is rewritten in place:
`--gold-products outputs/json/morning-anthem-step-3-gold-products.json`
(default `MORNING_ANTHEM_GOLD_PRODUCTS_PATH`),
`--output outputs/json/morning-anthem-step-4-keyword-bank.json`.
Preserves: the bank plan print (chunk sizes, per-chunk target, summed
target, band, LLM-call estimate), fail-closed on `KeywordGenerationError`
(writes nothing), the `.md` digest.

**Deliverable:** `outputs/json/morning-anthem-step-4-keyword-bank.json`
+ `.md` — the Step-5 keyword bank.

---

## 7. Step 5 — CJ-only ingestion

### 7.1 Single-engine bank ingestion (`src/pipeline/keyword_bank.py` — slimmed)

`BANK_ENGINES = ("cjdropshipping",)` — the dual-engine fan-out collapses to
a single engine, but the per-(keyword, engine) leg structure is **kept**
(it is what makes the funnel counters fold correctly and what leaves room
for a future second supplier). `OPTIMAL_SUPPLIER_DIRS` becomes
`{"cjdropshipping": "cjdropshipping"}`.

Every leg still runs the **full CJ gate stack, unchanged**:
commercial gate (`MIN_CJ_LISTED_COUNT`) → MCP Payload Liveness Gate →
mandatory freight quote (`CJ_DEFAULT_DEST_COUNTRY`, default `US`) →
global viability LLM gate → margin floor (`MIN_MARKUP_MULTIPLIER` 2.5x OR
`MIN_MARGIN_USD` $10) → 3-image gallery gate.

The **exact/fallback ordering** matters in Step 5: for each bank keyword,
the `exact`-match keywords are ingested **first** (highest precision), and
the `tight_fallback` keywords only after — so the exact title gets the
first chance at the `BANK_TARGET_PER_KEYWORD` export slots, and the
fallback only fills what the exact title could not. The bank loader sorts
each product's keywords `exact` → `tight_fallback` before ingestion.

### 7.2 LLM viability gate rewrite (`src/evaluators/llm_filter.py`)

The 5-point gate is rewritten for **The Morning Anthem** (global, premium
morning lifestyle):

1. **Problem Solver or Emotional Trigger** — solves an active morning-
   routine friction or serves a high-passion morning-ritual niche.
2. **Margin Viability (USD)** — priced for **realistic global premium
   retail** within one of the five Morning Anthem pillars, against the
   strict landed cost (listed price + quoted freight to the default
   destination). Must clear `MIN_MARKUP_MULTIPLIER` (2.5x) OR
   `MIN_MARGIN_USD` ($10) gross profit per unit. The prompt interpolates
   the live config values (unchanged mechanism).
3. **Global Logistical Feasibility** — < 1.2 kg, durable, non-perishable,
   air-freight compliant to the default destination (inferred from
   title/description only). (The AU-specific wording is gone; the
   physical bar is unchanged.)
4. **Global Saturation Resistance** — not an everyday mass-retail
   commodity (the §0 global retailer token set), and not a
   marketplace-duplicate with no differentiation.
5. **Demonstration Appeal** — a 3-second visual problem→solution dynamic.

The `shipping_quoted` flag semantics (verified quote vs unquoted floor) are
unchanged. The `shipping_notice` derivation moves to the configurable
destination (see §1.3).

### 7.3 Export destination

`<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/{metadata.json, images/}`
— one supplier subfolder, numbered independently, duplicate-URL skip
spans the folder across runs, 13-key `metadata.json` contract preserved
(currency-renamed per §1.3):

```json
{
  "product_title": "...",
  "category": "...",
  "suggested_price_usd": 49.95,
  "estimated_cogs_usd": 24.61,
  "cogs_estimation_basis": "Supplier listed price USD $5.19 plus USD $19.42 tracked shipping to US via CJPacket EUB, taken directly from the live CJdropshipping listing and its own freight quote.",
  "projected_margin_usd": 25.34,
  "marketing_ad_copy": "...",
  "features": ["..."],
  "target_tags": ["dropship", "..."],
  "shipping_notice": "Standard tracked international shipping to US via CJPacket EUB: 6-10 business days.",
  "supplier_name": "CJdropshipping",
  "supplier_retail_url": "https://cjdropshipping.com/product/<pid>.html",
  "image_source": "supplier_gallery"
}
```

### 7.4 Runner

`scripts/ingest_keyword_bank.py` is rewritten in place:
`--keywords outputs/json/morning-anthem-step-4-keyword-bank.json`
(default `MORNING_ANTHEM_KEYWORD_BANK_PATH`), `--target-per-keyword`
(default `BANK_TARGET_PER_KEYWORD`), `--limit`, `--export-root`
(default `MORNING_ANTHEM_EXPORT_DIR`). The `--only` flag is removed (one
engine). Pilot banner, per-leg funnel summary, and the intervention blocks
(Step 5 Keyword Bank / Step 5 Supplier Ingestion / LLM Evaluation Filter
Configuration / Step 5 Funnel Exhausted) are preserved; the AliExpress
DS Center Session block is removed (no AliExpress).

---

## 8. Step 6 — Jev product ranking (global appeal)

`src/ranking/jev_product_ranker.py` + `src/ranking/jev_client.py` are
rewritten for Morning Anthem:

- **Intake:** `<MORNING_ANTHEM_EXPORT_DIR>/cjdropshipping/product-NN/` +
  the Step-3 gold list. `SUPPLIER_DIRS = ("cjdropshipping",)`. The
  engine-qualified slug becomes `cjdropshipping_product_NN` (kept — the
  qualification is harmless with one engine and future-proofs a second).
- **Questions (three per package, unchanged shape):**
  - `<slug>__similarity` (score, 5 levels) — match to the closest
    gold-standard product's kind/material/mechanism/use. The top level is
    reworded from "AU demand" to **global appeal**: "identical or
    near-identical match to a gold-standard product's kind, material,
    mechanism and use, with global (not single-market) appeal".
  - `<slug>__winning_value` (score, 5 levels) — commercial value from the
    package's own `suggested_price_usd` / `estimated_cogs_usd` /
    `projected_margin_usd`, weighed against the demand evidence in the
    gold reference. Reworded from "AU demand evidence" to **global
    demand evidence**.
  - `<slug>__pillar` (choice) — criteria = the **five Morning Anthem
    pillars** + `discard` (fails the store's boundaries). `PILLAR_OPTIONS`
    in `jev_client.py` is rewritten to the §0 set.
- **Tiering** unchanged: `rank_score = 0.6·similarity + 0.4·value` on the
  1–5 scale (the `JEV_SCORE_INDEX_SHIFT = 1.0` normalisation stays);
  `≥ JEV_SHORTLIST_MIN_SCORE` (3.5) → shortlist; `≥ JEV_REVIEW_MIN_SCORE`
  (2.5) → review; else disregard.
- **Compliance gate** unchanged in mechanism: the banned-token tuple
  (now the §0 global set) plus `electric`, `usb`, `rechargeable`, scanned
  over `product_title` / `marketing_ad_copy` / `features`, forces
  `disregard` regardless of score.
- **metadata.json field projections** rename to the USD keys
  (`suggested_price_usd`, `estimated_cogs_usd`, `projected_margin_usd`,
  `shipping_notice`).
- **Ranking is a report, not a deletion** — unchanged.

**Deliverables:** `outputs/json/morning-anthem-step-6-ranked-candidates.json`
+ `.md`.

**Runner:** `scripts/rank_optimal_candidates.py` rewritten in place
(`--gold-products`, `--export-root`, `--batch-size`, `--output` defaults
from the renamed settings).

---

## 9. Global API budget — HasData free-tier math (STRICT)

**Constraint:** 1,000 HasData credits/month free tier. **A full multi-step
production run must consume ≤ `HASDATA_PRODUCTION_CAP` credits (**400** for
the launch run — see the LAUNCH RUN profile above). A test/pilot run must be
hard-capped at ≤ 10 credits.**

### 9.1 Credit rates

| Call | Credits | Source |
|---|---|---|
| Google Trends (per request, up to 5 terms) | **10** | **Confirmed 2026-10-12** by the operator on the HasData dashboard — the endpoint's *Run Request* button states it, matching `docs.hasdata.com/apis/google-trends/search` and the pilot's own arithmetic (two calls moved the balance by exactly 20). The repo's older "5 credits" note was stale. |
| Google Shopping (per request = per keyword) | **10** | Published, and the module's own `CREDITS_PER_REQUEST`. |

Both rates are config keys (`HASDATA_TRENDS_CREDITS_PER_CALL`,
`HASDATA_SHOPPING_CREDITS_PER_REQUEST`) rather than hardcoded numbers, so the
guardrail arithmetic below is computed from one place and a confirmed rate is
a one-line change.

(Jev/OpenRouter is billed in USD by OpenRouter, **not** HasData credits — it
does not count against the 1,000-credit HasData envelope. It is tracked
separately: a Step-2 gate pass or a full Step-6 run costs fractions of a cent
at the published input rate.)

### 9.2 Production-run envelope (launch run: ≤ 400 credits)

| Stage | Shape | Credits |
|---|---|---|
| Step 2 Trends research | ≤ 10 calls × 10 | **≤ 100** |
| Step 3 Google Shopping | **30** keywords × 1 request × 10 | **300** |
| **Step 3 hard cap** | `HASDATA_GS_MAX_KEYWORDS=30` | fail-closed above 30 |
| **Production total (Steps 2+3)** | | **≤ 400** |

At the conservative trends rate the two halves sum to the cap exactly
(10 × 10 + 30 × 10 = 400): `HASDATA_PRODUCTION_CAP` is the arithmetic, not an
arbitrary ceiling. Against the 1,000-credit monthly tier this leaves ~600
credits for the per-step pilots and any re-run.

Step 2 yields ~60 usable strings; 30 go to the scrape (the operator's
hand-pick — see the LAUNCH RUN profile), so a re-run can be fed from the
reserve without paying for trends again.

**Enforcement (fail-closed, before any credit moves):**

- `HASDATA_GS_MAX_KEYWORDS` (default **30**) hard-caps the Step-3 keyword
  count; the runner refuses to start above it.
- `HASDATA_GS_MAX_RESULTS_PER_KEYWORD` (default **20**) is a **local**
  slice only — it bounds LLM curation cost, never HasData spend (one
  request = 10 credits whatever the slice).
- The Step-3 runner prints the exact credit envelope
  (`keywords × HASDATA_SHOPPING_CREDITS_PER_REQUEST = N credits`) before the
  first request and refuses to start when it would exceed
  `HASDATA_PRODUCTION_CAP`.
- `HASDATA_MONTHLY_BUDGET` (default **1000**) is the tier ceiling the
  production cap is checked against.

### 9.3 Pilot / test envelope (≤ 10 credits, hard-capped)

| Mode | Cap | Enforcement |
|---|---|---|
| Step 3 pilot (`--limit 1`) | 1 keyword × 10 = **10 credits** | `--limit 1` is the only pilot shape the runner accepts by default; `--limit N` for N>1 requires `--allow-over-pilot-budget` (an explicit operator override that prints a WARNING). |
| Step 3 replay (`--from-raw`) | **0 credits** | Replays curation over a prior `--dump-raw` dump; no scrape call is made. |
| Hermetic tests | **0 credits** | The whole suite stays zero-network (MockTransport); no test may construct a real HasData client. |
| Step 2 Jev gate / Step 3 Jev pillar gate / Step 6 Jev ranking | 0 HasData credits | Jev is OpenRouter-billed (USD), not HasData — it does not touch the 1,000-credit envelope. |

The pilot banner on every runner prints theplanned credit spend and refuses
to proceed past the pilot cap without the explicit override.

### 9.4 What is NOT budgeted here

- LLM tokens (the reasoning model for Steps 3–4 curation/keyword-gen, and
  the OpenRouter Jev calls) are billed by the LLM provider, not HasData.
  They are tracked in each runner's summary line but do not count against
  the 1,000-credit HasData free tier.
- CJ MCP calls are free in credits (CJ's own supplier API), so Step 5 can loop
  over keywords until it finds an equivalent listing. They are **paced**, not
  budgeted: up to `CJ_MAX_PRODUCTS` product-detail round trips plus a freight
  quote each per keyword, and the LLM viability gate bills tokens per
  candidate. "Free" is not "instant", and a gold product CJ simply does not
  stock stays unfound (recorded as `keywords_empty`).

---

## 10. Configuration changes (`src/config.py`, `.env`, `.env.example`)

### 10.1 Removed keys

`ALI_DS_STATE_PATH`, `ALI_DS_MAX_PRODUCTS`, `MIN_DS_ORDER_COUNT`,
`MIN_DS_RATING`, `APIFY_TOKEN`, `APIFY_GS_ACTOR`,
`APIFY_GS_MAX_RESULTS_PER_KEYWORD`, `APIFY_GS_MAX_CHARGE_USD`,
`USD_TO_AUD`, `MIN_MARGIN_AUD`, `TARGET_COUNTRY`, `OPTIMAL_EXPORT_DIR`,
`GOLD_PRODUCTS_PATH`, `KEYWORD_BANK_PATH`.

### 10.2 Renamed keys

| Old | New | Default |
|---|---|---|
| `MIN_MARGIN_AUD` | `MIN_MARGIN_USD` | `10.0` |
| `GOLD_PRODUCTS_PATH` | `MORNING_ANTHEM_GOLD_PRODUCTS_PATH` | `outputs/json/morning-anthem-step-3-gold-products.json` |
| `KEYWORD_BANK_PATH` | `MORNING_ANTHEM_KEYWORD_BANK_PATH` | `outputs/json/morning-anthem-step-4-keyword-bank.json` |
| `OPTIMAL_EXPORT_DIR` | `MORNING_ANTHEM_EXPORT_DIR` | `…/my-store-build/inspiration/morning-anthem-candidates` |

### 10.3 New keys

| Key | Default | Used by |
|---|---|---|
| `CJ_DEFAULT_DEST_COUNTRY` | `US` | CJ freight quote destination (global default) |
| `SHOPPING_GL` | `us` | Step 3 HasData Google Shopping `gl` param |
| `SHOPPING_DOMAIN` | `google.com` | Step 3 HasData Google Shopping `domain` param |
| `TRENDS_GEO` | `US` | Step 2 Trends MCP geography (agent-side) |
| `HASDATA_GS_MAX_KEYWORDS` | `30` | Step 3 hard keyword-count cap (fail-closed) |
| `HASDATA_MONTHLY_BUDGET` | `1000` | The free-tier ceiling (informational + cap check) |
| `HASDATA_PRODUCTION_CAP` | `400` | Max credits one production run may plan (launch-run profile) |
| `HASDATA_TRENDS_CREDITS_PER_CALL` | `10` | Trends rate used by the guardrail arithmetic (observed 2026-10-12; published tables say 5) |
| `HASDATA_SHOPPING_CREDITS_PER_REQUEST` | `10` | Shopping rate used by the guardrail arithmetic |
| `TREND_GATE_BATCH_SIZE` | `8` | Step 2 Jev product-intent gate batch |
| `TREND_SELECT_BATCH_SIZE` | `12` | Step 2 Jev **selection** pass batch (score questions) |
| `TREND_SELECT_TARGET` | `30` | how many keywords Step 2 selects for the Step-3 scrape |
| `TREND_SELECT_MIN_PER_PILLAR` | `2` | per-pillar floor in the selection quota, so no pillar is crowded out |
| `PILLAR_GATE_BATCH_SIZE` | `4` | Step 3 Jev pillar gate batch |

`HASDATA_GS_MAX_RESULTS_PER_KEYWORD` stays (default lowered 40 → **20** for
the tighter curation budget).

`.env.example` mirrors `.env` in the same order with the same descriptions
(CLAUDE.md §10 rule preserved); blank still means default. `Settings` keeps
the no-leak repr.

---

## 11. Tests (hermetic, zero network — 579-test baseline preserved in spirit)

- **Delete:** `tests/test_aliexpress_ds.py`, `tests/test_google_shopping.py`
  (Apify wrapper).
- **Rewrite:** `tests/test_keyword_generator.py` (new `match` field,
  exact-verbatim validator, tight-fallback content-word rule, five-pillar
  Literal, global banned tokens), `tests/test_keyword_bank.py` (single-
  engine, exact-before-fallback ordering), `tests/test_jev_ranker.py`
  (five-pillar PILLAR_OPTIONS, USD metadata fields, `cjdropshipping`-only
  intake), `tests/test_llm_filter.py` (Morning Anthem prompt + USD
  margin floor), `tests/test_models.py` (USD field names, CJ-only
  supplier patterns, destination-interpolated shipping notice),
  `tests/test_config.py` (new/renamed/removed keys), `tests/test_main.py`
  (CJ-only registry), `tests/test_exporter.py` (USD metadata keys),
  `tests/test_google_shopping_hasdata.py` (gl=us default, keyword cap,
  budget-cap refusal).
- **New:** `tests/test_trend_gate.py` (Jev product-intent gate: batch
  envelope, per-string choice fan-in, non-`product_search` drops, retry-
  then-drop, config error, verbatim carry-through), `tests/test_pillar_gate.py`
  (Jev pillar gate: five-pillar + discard criteria, discard drops, batch
  envelope, retry-then-drop, no-fact-authoring — every product fact is
  code-assembled).
- **Unchanged:** `tests/test_cj_mcp_client.py`, `tests/test_cj_mcp_extractor.py`
  (the CJ core is inherited; only the destination-default and docstring
  sweeps touch them), `tests/test_image_sourcing.py`,
  `tests/test_mcp_headers.py`.
- `tests/conftest.py`: the `_SENSITIVE_ENV_VARS` scrub gains nothing new
  (HasData/OpenRouter keys already covered; Apify key removed from the
  scrub list is fine since the key itself is gone).

---

## 12. Documentation updates

- **CLAUDE.md:** §1 mission (Morning Anthem, global, CJ-only, HasData-only);
  §3 gate-2 (global pricing, USD); §4 workspace tree (new modules, renamed
  scripts, deleted extractor); §5–§6 supplier core (CJ only; AliExpress
  section removed); §8 export contract (USD keys, `shipping_notice`,
  `morning-anthem-candidates/`); §9 CLI ( `--extractor {auto,cjdropshipping}`);
  §10 config table (new/renamed/removed keys); §12 operational notes
  (HasData-only budget, global geography, the Jev gates); §13 replaced by
  a **Morning Anthem pipeline** section (the five pillars, the new step
  flow, the budget math). Every "Sabaah Goods", "Arab-Aussie", "AU",
  "AUD", "AliExpress", "Apify" reference is purged.
- **README.md:** the gold-standard intake subsection rewritten for the
  Morning Anthem flow; repo-layout tree extended; test-count line corrected.
- **`docs/trends-mcp-schema.md`:** geography note updated to global.

---

## 13. Execution protocol & acceptance

Same standing agreement: **step by step, each step proven before the next
starts; the operator supplies credentials one step at a time; work commits
+ pushes to `feature/morning-anthem-pipeline-update`; the agent never opens
a PR.** The operator requests the Copilot review himself when he wants it.

Per step:

| Step | Proof gate before advancing |
|---|---|
| Renames/removals (§1) | suite green after the **AUD-retention**, AliExpress-removal and Apify-removal sweeps (the AUD rebase the plan body describes was cancelled by the launch overrides); no existing test weakened |
| 2 | Step-2 deliverable validated (Jev `product_search`-confirmed, five-pillar tags, demand evidence, no banned tokens) + the Jev **selection** recorded (scores per keyword, the cut, the reserve) + `tests/test_trend_gate.py` and `tests/test_trend_select.py` green + one live pilot (≤ 10 HasData credits) |
| 3 | HasData-only scrape + LLM select + Jev pillar gate; `tests/test_pillar_gate.py` green + one live pilot (`--limit 1`, exactly 10 credits, 0 over) + gold deliverable reviewed (every product five-way pillar-tagged, on-page demand evidence). The launch run then scrapes the **Jev-selected 30 keywords** (300 credits, within `HASDATA_PRODUCTION_CAP=400`) |
| 4 | suite green + one live run reproducing an exact-title-first bank from the real Step-3 file; every `exact` row verbatim-verified against its product name; bank reviewed |
| 5 | suite green + pilot (`--limit 4 --target-per-keyword 1`) live, `cjdropshipping/` root populated, per-leg funnel summary reviewed; exact-before-fallback ordering observed in the logs |
| 6 | suite green + one live ranked report over the pilot packages; tiers reviewed; five-pillar classification observed |

Commits are per-step, imperative, repo-style, each carrying the step's
live-evidence notes. No existing test may be weakened — the rewritten
modules must keep every still-applicable machinery test.

---

## 14. Open decisions for the operator

1. ~~`CJ_DEFAULT_DEST_COUNTRY=US`~~ — **SETTLED by the launch overrides:
   `AU`.** The Australia-first launch ships from CJ's China warehouse into
   Australia, so the freight quote names an Australian lane. The global
   question (a second reference destination, or quote-to-each-market) returns
   when the store opens Shopify Markets.
2. ~~`SHOPPING_GL=us` / `TRENDS_GEO=US`~~ — **SETTLED by the launch overrides:
   `au` / `AU`.** Demand evidence and price benchmarks are the AU market at
   launch, so the launch run can be read against Australian retail reality.
3. ~~Trends credit rate~~ — **RESOLVED 2026-10-12: 10 credits per request**,
   confirmed on the HasData dashboard. The launch-run arithmetic above already
   budgets it, so the 400-credit envelope stands as written.
4. **The selection shape** — Jev scores, code quotes. `TREND_SELECT_MIN_PER_PILLAR`
   (**2**) is the floor inside each pillar's proportional quota: it guarantees a
   weak pillar still reaches the launch (the pilot found `sunrise_move` has
   almost no AU demand evidence, so a pure score race would drop it entirely),
   and it costs the dominant pillar one slot. `TREND_SELECT_TARGET` **is** the
   Step-3 budget (10 credits per keyword), so 30 means 300 credits.
   `--no-pillar-quota` reverts to a pure best-of.
5. **Keyword-bank size** — the exact-title-first strategy yields ~1–2
   keywords per product (exact + maybe one tight fallback). Confirm the
   bank band target (the old 50–70 band is replaced by a per-product
   1–2 target; the merged bank lands at roughly the gold-product count).
