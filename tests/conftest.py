"""Shared test isolation for the offline suite.

The suite must make ZERO live network calls (hermetic unit tests only).
Three hazards are neutralised here for every test:

1. A real `CJ_MCP_TOKEN` may exist in `.env` (config.py loads it at import).
   The CJ MCP extractor would otherwise open a live session against the
   remote endpoint. `settings.CJ_MCP_TOKEN` is patched to None here so the
   module-level client singleton stays unconfigured; tests needing a client
   build their own against a mocked MCP session and inject it directly.
2. The cached CJ MCP client singleton in `src.extractors.cj_mcp_extractor`
   is process-global — it is reset around every test so a client configured
   (or stubbed) by one test never leaks into the next.
3. `load_dotenv()` also puts every live `.env` credential into `os.environ`
   for the whole process, so a test that reads one — or a failing assertion
   that mentions one — would print a real secret. Those values are replaced
   with stand-ins (see `sanitize_test_environment`).
"""

import os

import pytest

from src.config import settings
from src.extractors import cj_mcp_extractor

# Live credentials the developer keeps in `.env`. `LLM_API_KEY` (Ollama
# Cloud) is the same class of secret as the rest, so it is scrubbed with them.
_SENSITIVE_ENV_VARS = (
    "APIFY_TOKEN",
    "HASDATA_API_KEY",
    "LLM_API_KEY",
    "OPENROUTER_API_KEY",
    "CJ_MCP_TOKEN",
)


@pytest.fixture(autouse=True)
def sanitize_test_environment(request, monkeypatch):
    """Replace any live credential in the environment with a stand-in value.

    Present-but-fake rather than deleted: code that branches on "is this
    configured?" keeps the same shape, while a value that reaches an
    assertion can only ever be `mock-test-...`. A test that genuinely needs
    the real credential opts out with `@pytest.mark.live`.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in _SENSITIVE_ENV_VARS:
        if os.environ.get(name):
            monkeypatch.setenv(name, f"mock-test-{name.lower()}")


@pytest.fixture(autouse=True)
def _hermetic_supplier_pipeline(monkeypatch):
    cj_mcp_extractor.reset_cj_mcp_client()
    monkeypatch.setattr(settings, "CJ_MCP_TOKEN", None)
    yield
    cj_mcp_extractor.reset_cj_mcp_client()
