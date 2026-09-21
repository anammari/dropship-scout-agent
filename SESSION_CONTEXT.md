# SESSION_CONTEXT.md — AliExpress Dropshipping Center gate

Companion to `CLAUDE.md`. Covers the AliExpress Dropshipping Center (DS
Center) ingestion gate and the authenticated session it depends on.

## 1. What the gate does

`ENABLE_DS_CENTER_GATE` (default **off**) adds a DS Center verification step
to AliExpress ingestion. When on, every candidate returned by the Apify actor
— not just the thin ones — is checked against the DS Center, and a candidate
the DS Center does not list is dropped *before* its PDP image extraction, so
no cost or time is spent on products that cannot be dropshipped. Items that
pass are harvested exactly as before.

The Apify actor still runs first and supplies the bulk candidate list; the DS
Center check is purely a filter in the Playwright verification phase.

## 2. How the check works

`src/extractors/aliexpress_apify.py`, gate section:

1. **Authenticated context, target market pinned.** When the gate is on, the
   Playwright context is created with `storage_state=ALI_DS_STATE_PATH`. A
   missing state file raises `DsCenterSessionExpiredError` (a `RuntimeError`)
   rather than running unverified. The context also carries the AliExpress
   ship-to cookie (`aep_usuc_f`, `region=<run country>`, defaulted from
   `TARGET_COUNTRY`) because the DS Center's verdict is market-specific: the
   same item can be listed for AU and `none_of_item` elsewhere, and without
   the cookie the DS Center answers for its own default market.
2. **Navigate.** The item's DS Center Product Analysis entry is opened —
   `https://ds.aliexpress.com/product-analysis?itemId=<id>` — which is the
   same flow as pasting the product link into the DS Center and pressing
   *Analyze* (the page auto-runs the analysis for an `itemId` in the query).
   A login redirect, or a password field where the analysis should be,
   raises `DsCenterSessionExpiredError` so the operator knows to refresh the
   session.
3. **Item verdict.** The item's DS Center record is read through the
   authenticated context via MTOP's token-then-sign handshake
   (`mtop.aidc.ds.center.selection.queryByItemUrl`, the XHR the DS Center UI
   itself uses): `code "-1"` / `message "none_of_item"` means the DS Center
   holds no record of the item → **not supported**; a record carrying the
   item id means **supported**. MTOP session flags
   (`FAIL_SYS_SESSION_EXPIRED`, `FAIL_SYS_USER_VALIDATE`,
   `FAIL_SYS_ILLEGAL_ACCESS`) also raise `DsCenterSessionExpiredError`.
4. **DOM fallback.** If the lookup yields no verdict, the rendered page is
   read directly: an explicit *no data / not supported* statement is a
   rejection, and a rendered analysis table is a pass. No signal at all is
   inconclusive and the candidate is dropped with a warning.

Drops are logged per candidate — `Skipping <id>: Not supported in DS Center`
— and summarised once per run
(`DS Center gate dropped N of M verified candidate(s)`). A candidate that
passes but already has a full gallery is not re-harvested.

The gate never fabricates or edits product data: it only removes candidates,
so the `CLAUDE.md` §2 anti-hallucination contract is untouched.

## 3. The session script (`scripts/generate_ali_session.py`)

The DS Center needs an account login, so the pipeline cannot reach it
anonymously; a saved browser session supplies the credentials.

```bash
source .venv/bin/activate && python scripts/generate_ali_session.py
```

* Launches a **non-headless** stealth Chromium (Playwright's bundled build —
  the operator's own Chrome profile and tabs are untouched).
* Opens `https://login.aliexpress.com` and prints:

  > Please log in to your AliExpress account and navigate to the
  > Dropshipping Center manually. Press Enter here when done.

* On Enter, writes the browser context's `storage_state` to
  `ALI_DS_STATE_PATH` (default `ali_ds_state.json`, repo root, git-ignored).

Re-run it whenever a run reports `DsCenterSessionExpiredError`. The blocking
`input()` runs on a worker thread so the event loop keeps servicing the
browser while the operator logs in.

## 4. Configuration

| Key | Default | Notes |
|---|---|---|
| `ENABLE_DS_CENTER_GATE` | `False` | Off: ingestion is unchanged. On: the gate applies and the session file is required. |
| `ALI_DS_STATE_PATH` | `ali_ds_state.json` | Playwright `storage_state` written by the session script; git-ignored. |

Both keys are declared in `.env.example` and `.env` in the same order. The
market the gate asks about is the run's country (`--country`, default
`TARGET_COUNTRY`).

## 5. Tests

`tests/test_ds_center_gate.py` (hermetic, zero network) covers the item-id
extraction, the MTOP signature, the payload classification, the login-wall
and session-expiry paths, the DOM fallback, the state-file requirements, and
the drop/keep wiring through `fetch_products`. Its fixtures are the two live
products from the gate directive: `1005012359331033` (in the DS Center) and
`1005007987710373` (not in it). `tests/test_config.py` covers the two new
keys.

Live check (2026-09-19, read-only, no Apify spend): with the AU market
pinned, both directive products resolve to a DS Center record (`KEEP`); an
item id the DS Center has no record of resolves to `none_of_item` and is
dropped. Verdicts are market-specific — the directive's product labels hold
for a different region's context, not AU — so the gate is only meaningful
with the ship-to cookie above.
