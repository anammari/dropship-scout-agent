#!/usr/bin/env python
"""Generate the AliExpress Dropshipping Center session state.

Optional. `AliExpressDsCenterExtractor` reaches the DS Center's search and
item-record APIs anonymously, so a run works without this file; the saved
session is injected into its Playwright context only when it exists, as an
upgrade for the case where AliExpress starts gating those calls behind a
login.

This script opens a real (non-headless) stealth Chromium, lets the operator
log in and visit the Dropshipping Center by hand, and then saves the browser
context's `storage_state` to `ALI_DS_STATE_PATH` (default
`ali_ds_state.json` at the repo root — git-ignored). Re-run it if a run
reports that the DS Center refused the request.

Usage (from the repo root):

    source .venv/bin/activate && python scripts/generate_ali_session.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow `python scripts/generate_ali_session.py` (script dir is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402

logger = logging.getLogger("generate_ali_session")

LOGIN_URL = "https://login.aliexpress.com"
PROMPT = (
    "Please log in to your AliExpress account and navigate to the "
    "Dropshipping Center manually. Press Enter here when done."
)


async def main() -> int:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    state_path = Path(settings.ALI_DS_STATE_PATH)
    print(f"Session state will be written to: {state_path.resolve()}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=settings.USER_AGENT,
            locale="en-AU",
            viewport={"width": 1366, "height": 900},
        )
        await Stealth(
            navigator_user_agent_override=settings.USER_AGENT
        ).apply_stealth_async(context)
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            # Blocking `input()` must not run on the event loop: the loop has
            # to keep servicing the browser while the operator logs in.
            await asyncio.to_thread(input, PROMPT)
            await context.storage_state(path=str(state_path))
        finally:
            await browser.close()

    print(f"Saved AliExpress Dropshipping Center session state to {state_path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))
