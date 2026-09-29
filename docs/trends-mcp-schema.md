# Google Trends MCP — schema reference (Step 1)

Verified live on 2026-09-25 against both servers, with real AU calls. This is
the reference for the keyword-research stage of the Multi-Step Implementation
Pipeline (`plans/spike_niche_keywords_implementation_plan.md` §4); it records
the observed data structures, not the vendors' documentation.

**Default source: HasData. Fallback: the Apify `data_xplorer/google-trends-fast-scraper` actor.**
Both are wired in the project-scope `.mcp.json` and authenticate through
`scripts/mcp_headers.py`, which reads `.env` from disk (Claude Code strips
credential-named variables from the environment before running a project
`headersHelper`, so the usual `${VAR}` expansion cannot carry them).

## 1. Connection contract

| | `google-trends` (default) | `apify-trends` (fallback) |
|---|---|---|
| URL | `https://mcp.hasdata.com/mcp?apis=google_trends` | `https://mcp.apify.com/?tools=data_xplorer/google-trends-fast-scraper` |
| Auth header | `x-api-key: <HASDATA_API_KEY>` | `Authorization: Bearer <APIFY_TOKEN>` |
| Tools exposed | 1 (of HasData's 63 — scoped by `apis=`) | 5 |
| Session | **stateless** — no session id, no `notifications/initialized` needed | **stateful** — must echo `Mcp-Session-Id` from `initialize` and send `notifications/initialized` |

The Apify asymmetry is a real integration trap: a client that skips the
handshake gets `Bad Request: No valid session ID provided` on every
`tools/list` or `tools/call`, while HasData works without it.

## 2. HasData — default source

### Tool: `hasdata_google_trends_search_getTrendsData`

| Param | Required | Type / values |
|---|---|---|
| `q` | **yes** | Search term. Multiple queries allowed for `timeseries` / `geoMap`. |
| `geo` | no | Exact documented value (3,517 allowed), e.g. `AU`, `AU-NSW`. Default worldwide. |
| `dataType` | no | `timeseries` (default) · `geoMap` · `relatedTopics` · `relatedQueries`. The last two accept **only a single query**. |
| `date` | no | `now 1-H` · `now 4-H` · `now 1-d` · `now 7-d` · `today 1-m` · `today 3-m` · `today 12-m` · `today 5-y` · `all`, or custom `yyyy-mm-dd yyyy-mm-dd`. |
| `tz` | no | Minutes offset; default `420` (PDT). **AU: `600` AEST, `660` AEDT.** |
| `region` | no | `country` · `region` (subregion) · `dma` (metro) · `city` — `geoMap` only. |
| `cat` | no | Category id; default `0` (all). |
| `gprop` | no | `images` · `news` · `froogle` · `youtube`; unset = Web Search. |

### Response shape

MCP wraps the payload as `content[0].text`, a **JSON string** — so reading it
takes a double decode (`json.loads(content[0]["text"])`, then the `.json` key):

```
{ "url": "...", "status": 200, "text": "...",
  "json": {
    "requestMetadata": { "id", "status": "ok", "html", "json", "preview" },
    "interestOverTime": {
      "timelineData": [
        { "date": "Sep 19 – 25, 2021",     # display string, NOT sortable
          "timestamp": "1632009600",        # string — int() it to sort
          "isPartial": false,
          "values": [ { "query": "incense burner",
                        "value": "53",          # string
                        "extractedValue": 53,   # int — use this
                        "hasData": true } ] } ] } } }
```

Observations from a live `date="today 5-y"`, `geo="AU"` call (262 weekly points):

- **`hasData` is the gap marker.** Only **218 of 262** points carry it as true;
  the other 44 are weeks Google has no data for, and their `extractedValue` is
  meaningless. Filter on `hasData` before any averaging.
- **`isPartial`** is true on exactly the final (in-flight) week — exclude it
  from seasonality math.
- Range 0–100, relative index (not absolute volume).
- `url` exposes the underlying REST endpoint
  (`https://api.hasdata.com/scrape/google-trends/search/`), so the same data is
  reachable without MCP from Python later if that is preferred.

### Calling it from code — two traps (observed 2026-09-25)

1. **Replies are framed as SSE.** `content-type: text/event-stream`, one JSON
   message per `data:` line. A client that calls `response.json()` on the raw
   body fails with `JSONDecodeError: Expecting value: line 1 column 1` — parse
   the `data:` lines instead.
2. **Tool-level failures are not HTTP errors.** A rejected call returns
   **HTTP 200** with `result.isError: true` and the diagnostic as plain text in
   `content[0].text` (e.g. `MCP error -32602: Input validation error: … Expected
   number, received string at tz`); the query never runs. Read `isError` before
   decoding `content[0].text` as JSON, or the real error is masked by a decode
   failure.
3. **`tz` is a number, not a string.** `"tz": "600"` is rejected at input
   validation; `"tz": 600` works.

Measured spend for the Step 2 research pass: 36 successful calls ≈ 180 credits.

## 3. Apify fallback — `data_xplorer/google-trends-fast-scraper`

The actor is exposed as one dedicated tool, `data_xplorer--google-trends-fast-scraper`
(plus auto-injected `get-actor-run`, `get-dataset-items`, `get-key-value-store-record`,
`abort-actor-run`). `?tools=data_xplorer/google-trends-fast-scraper` scopes the
server to exactly those 5, instead of the 10 you get from `?tools=actors,docs,…`.

Params (all optional per the tool schema): `mode` (`keyword` | `trending`),
`keyword`, `geo`, `predefinedTimeframe`, `fetchRegionalData`,
`trendingSearches{Country,Timeframe,Categories,MaxItems}`, `proxyConfiguration`,
`waitSecs` — **`waitSecs` must be ≤ 45**, and a larger value fails input
validation before the run starts.

**It does not return rows.** The tool call returns a run envelope
(`runId`, `status`, `stats`, `storages.datasets.default.id`); the data comes
from a **second** call to `get-dataset-items`, which returns
`{datasetId, items[], itemCount, totalItemCount, offset, limit}` plus a trailing
non-JSON "Fetched all N items" content part (parse part 0 only).

Item shape — note it is **flatter** than HasData's:

```
{ "keyword": "incense burner", "timeframe": "today 5-y", "geo": "AU",
  "language": "en-AU", "data_granularity": "week",
  "trends_url": "https://trends.google.com/trends/explore?...",
  "timeline_data": {
      "incense burner": { "2021-09-19": 53, "2021-09-26": 63, ... },   # ISO date -> int
      "isPartial":      { "2021-09-19": false, ..., "2026-09-20": true } },
  "region_data": [] }
```

Dates are ISO strings, so they sort chronologically as keys — simpler than
HasData's display strings. `region_data` populates when `fetchRegionalData` is on.

## 4. Cross-validation: the two sources agree

Both were run on the same term (`incense burner`, `geo=AU`, 5 years) and the
series aligned point-for-point:

| Check | Result |
|---|---|
| Point count | 262 vs 262, same week-start convention |
| Values where HasData has data | **214 / 218 identical (98%)**; the 4 mismatches differ by ±1 (quantisation) |
| `isPartial` | **Agrees on all 262 points** (exactly 1 partial week each) |
| Missing weeks | HasData's **44** `hasData=false` weeks map **exactly** onto Apify's **44** zeros — zero disagreement either direction |

### The trap this exposes

**Apify encodes "no data" as `0`, indistinguishable from genuine zero interest.**
HasData flags the same weeks explicitly via `hasData: false`. Because the series
are otherwise identical, Apify's zeros are missing-data markers, not real
readings — 44 of 262 points (17%) are phantom. Averaging Apify's series naively
drags the mean down and can invert a seasonality read. HasData's `hasData` flag
makes correct handling possible; on the fallback, treat `0` as missing.

## 5. Cost & rate limits

| | Free allowance | Per call | Notes |
|---|---|---|---|
| HasData | 1,000 credits/month ≈ **200 calls** | 5 credits, any size | Failed (non-200) calls not billed; a successful call that finds nothing still is. |
| Apify | $5/month credit | **$0.02** run start + $0.002/result (FREE tier) | Measured: one keyword run cost **$0.02**, 5s wall clock. |

Seasonality work is what makes the default worth it: a 5-year series is one
HasData call, whereas TrendsMCP's free tier caps history at 90 days and returns
top-10 boards only — too shallow to answer "when does this peak in AU?".

## 6. Fallback policy

Prefer HasData. Fall back to Apify when HasData is unavailable, rate-limited
(monthly credits exhausted), or returns a schema too thin for the need in hand —
`relatedQueries` / `relatedTopics` are single-query-only on HasData, and the
fallback carries its own regional and trending-search modes. Every fallback read
must apply the §4 zero-is-missing rule.

## 7. Operational notes

- **Restart required.** MCP servers added to `.mcp.json` are loaded at session
  start; their tools do not appear in an already-running session.
- **Project `.mcp.json` triggers a trust prompt** on first use, and
  `headersHelper` runs only after that is accepted.
- If the helper is ever invoked from outside the project root, the relative
  `scripts/mcp_headers.py` path in `.mcp.json` will not resolve — the script
  locates `.env` from its own `__file__`, but its own path is the caller's
  responsibility.
- A missing credential makes the helper exit non-zero with a clear message
  rather than emitting an empty header, so a misconfigured run fails loudly.
