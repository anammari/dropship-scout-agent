"""Shared test isolation for the offline suite.

The suite must make ZERO live network calls (hermetic unit tests only).
Two hazards are neutralised here for every test:

1. A real `CJ_MCP_TOKEN` may exist in `.env` (config.py loads it at import).
   The CJ MCP extractor would otherwise open a live session against the
   remote endpoint. `settings.CJ_MCP_TOKEN` is patched to None here so the
   module-level client singleton stays unconfigured; tests needing a client
   build their own against a mocked MCP session and inject it directly.
2. The cached CJ MCP client singleton in `src.extractors.cj_mcp_extractor`
   is process-global — it is reset around every test so a client configured
   (or stubbed) by one test never leaks into the next.
"""

import pytest

from src.config import settings
from src.extractors import cj_mcp_extractor


@pytest.fixture(autouse=True)
def _hermetic_supplier_pipeline(monkeypatch):
    cj_mcp_extractor.reset_cj_mcp_client()
    monkeypatch.setattr(settings, "CJ_MCP_TOKEN", None)
    yield
    cj_mcp_extractor.reset_cj_mcp_client()
