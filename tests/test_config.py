"""Unit tests for `src.config` settings resolution.

Uses the `load_settings()` factory (not the cached `settings` singleton)
so each test is isolated from import order / env pollution.
"""

import re
from pathlib import Path

import pytest

from src.config import load_settings


# ----------------------------------------------------------------------
# LLM config
# ----------------------------------------------------------------------


def test_llm_model_defaults_to_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert load_settings().LLM_MODEL == "deepseek-v4-flash:cloud"


def test_llm_model_env_override_still_respected(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-other-model:cloud")
    assert load_settings().LLM_MODEL == "some-other-model:cloud"


def test_llm_model_empty_string_normalises_to_default(monkeypatch):
    # An empty pin is treated as unset, not as a literal empty model name.
    monkeypatch.setenv("LLM_MODEL", "")
    assert load_settings().LLM_MODEL == "deepseek-v4-flash:cloud"


def test_missing_credentials_resolve_to_empty_strings(monkeypatch):
    for name in (
        "APIFY_API_TOKEN",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "ETSY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    fresh = load_settings()
    # Plain str (not Optional/None) — extractor-level NotConfigured checks
    # rely on a plain falsy value.
    for name in (
        "APIFY_API_TOKEN",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "ETSY_API_KEY",
    ):
        assert getattr(fresh, name) == ""


# ----------------------------------------------------------------------
# CJdropshipping MCP config
# ----------------------------------------------------------------------


def test_cj_mcp_token_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("CJ_MCP_TOKEN", raising=False)
    assert load_settings().CJ_MCP_TOKEN is None


def test_cj_mcp_token_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CJ_MCP_TOKEN", "mcp-token-abc")
    assert load_settings().CJ_MCP_TOKEN == "mcp-token-abc"


def test_cj_mcp_token_empty_string_normalises_to_none(monkeypatch):
    # An empty slot in .env (the shipped placeholder) must read as
    # "unconfigured", not as an empty credential to send to CJ.
    monkeypatch.setenv("CJ_MCP_TOKEN", "")
    assert load_settings().CJ_MCP_TOKEN is None


def test_cj_mcp_base_url_defaults_to_the_documented_endpoint(monkeypatch):
    monkeypatch.delenv("CJ_MCP_BASE_URL", raising=False)
    assert (
        load_settings().CJ_MCP_BASE_URL
        == "https://developers.cjdropshipping.com/mcp"
    )


def test_cj_mcp_base_url_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CJ_MCP_BASE_URL", "https://proxy.internal/mcp/")
    assert load_settings().CJ_MCP_BASE_URL == "https://proxy.internal/mcp/"


def test_cj_mcp_base_url_blank_falls_back_to_the_default(monkeypatch):
    # A key present but blank in .env means "use the code default" — not an
    # empty base that would produce a bare "/{token}" endpoint URL.
    monkeypatch.setenv("CJ_MCP_BASE_URL", "")
    assert (
        load_settings().CJ_MCP_BASE_URL
        == "https://developers.cjdropshipping.com/mcp"
    )


# ----------------------------------------------------------------------
# Supplier-first extractor config (plan F)
# ----------------------------------------------------------------------


def test_apify_actor_defaults_to_the_pay_per_result_actor(monkeypatch):
    monkeypatch.delenv("APIFY_ACTOR_ID", raising=False)
    assert (
        load_settings().APIFY_ALIEXPRESS_ACTOR
        == "cryptosignals/aliexpress-scraper"
    )


def test_apify_actor_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("APIFY_ACTOR_ID", "some/other-actor")
    assert load_settings().APIFY_ALIEXPRESS_ACTOR == "some/other-actor"


def test_apify_max_items_defaults_to_twenty(monkeypatch):
    monkeypatch.delenv("APIFY_MAX_ITEMS", raising=False)
    assert load_settings().APIFY_MAX_ITEMS_PER_KEYWORD == 20


def test_apify_max_items_malformed_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("APIFY_MAX_ITEMS", "not-an-int")
    with pytest.raises(ValueError):
        load_settings()


def test_apify_price_per_result_defaults_to_the_actor_price(monkeypatch):
    monkeypatch.delenv("APIFY_PRICE_PER_RESULT_USD", raising=False)
    assert load_settings().APIFY_PRICE_PER_RESULT_USD == 0.005


def test_apify_price_per_result_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("APIFY_PRICE_PER_RESULT_USD", "0.006")
    assert load_settings().APIFY_PRICE_PER_RESULT_USD == 0.006


def test_apify_max_items_per_run_defaults_to_one_hundred(monkeypatch):
    monkeypatch.delenv("APIFY_MAX_ITEMS_PER_RUN", raising=False)
    assert load_settings().APIFY_MAX_ITEMS_PER_RUN == 100


def test_apify_max_items_per_run_malformed_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("APIFY_MAX_ITEMS_PER_RUN", "not-an-int")
    with pytest.raises(ValueError):
        load_settings()


def test_apify_price_per_result_blank_falls_back_to_the_actor_price(monkeypatch):
    monkeypatch.setenv("APIFY_PRICE_PER_RESULT_USD", "")
    assert load_settings().APIFY_PRICE_PER_RESULT_USD == 0.005


def test_apify_max_items_per_run_blank_falls_back_to_the_cap(monkeypatch):
    # Blank must not collapse to 0 — that would disable AliExpress ingestion.
    monkeypatch.setenv("APIFY_MAX_ITEMS_PER_RUN", "")
    assert load_settings().APIFY_MAX_ITEMS_PER_RUN == 100


def test_apify_run_timeout_defaults_to_a_tight_sixty_seconds(monkeypatch):
    # The pay-per-result budget guard relies on a short bound: a stuck
    # proxy must not keep a run alive (and billing) for minutes.
    monkeypatch.delenv("APIFY_RUN_TIMEOUT_SECS", raising=False)
    assert load_settings().APIFY_RUN_TIMEOUT_SECS == 60


def test_apify_run_timeout_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("APIFY_RUN_TIMEOUT_SECS", "120")
    assert load_settings().APIFY_RUN_TIMEOUT_SECS == 120


def test_supplier_priority_order_defaults_to_cjdropshipping_first(monkeypatch):
    monkeypatch.delenv("SUPPLIER_PRIORITY_ORDER", raising=False)
    assert load_settings().SUPPLIER_PRIORITY_ORDER == [
        "cjdropshipping",
        "aliexpress",
        "etsy",
    ]


def test_supplier_priority_order_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "etsy,aliexpress")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["etsy", "aliexpress"]


def test_supplier_priority_order_is_normalised(monkeypatch):
    # Keys are trimmed and lowercased so " AliExpress , CJdropshipping "
    # and shell quoting quirks resolve to canonical extractor keys.
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", " AliExpress , CJdropshipping ")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["aliexpress", "cjdropshipping"]


def test_supplier_priority_order_empty_string_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == [
        "cjdropshipping",
        "aliexpress",
        "etsy",
    ]


def test_supplier_priority_order_drops_blank_segments(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "aliexpress,,,etsy")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["aliexpress", "etsy"]


def test_usd_to_aud_defaults_to_one_point_five_five(monkeypatch):
    monkeypatch.delenv("USD_TO_AUD", raising=False)
    assert load_settings().USD_TO_AUD == 1.55


def test_usd_to_aud_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("USD_TO_AUD", "1.60")
    assert load_settings().USD_TO_AUD == 1.60


def test_usd_to_aud_malformed_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("USD_TO_AUD", "not-a-float")
    with pytest.raises(ValueError):
        load_settings()


def test_cj_max_products_defaults_to_ten(monkeypatch):
    monkeypatch.delenv("CJ_MAX_PRODUCTS", raising=False)
    assert load_settings().CJ_MAX_PRODUCTS_PER_KEYWORD == 10


def test_cj_max_products_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CJ_MAX_PRODUCTS", "5")
    assert load_settings().CJ_MAX_PRODUCTS_PER_KEYWORD == 5


# ----------------------------------------------------------------------
# AliExpress Dropshipping Center gate
# ----------------------------------------------------------------------


def test_ds_center_gate_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_DS_CENTER_GATE", raising=False)
    assert load_settings().ENABLE_DS_CENTER_GATE is False


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes"])
def test_ds_center_gate_accepts_truthy_values(monkeypatch, raw):
    monkeypatch.setenv("ENABLE_DS_CENTER_GATE", raw)
    assert load_settings().ENABLE_DS_CENTER_GATE is True


@pytest.mark.parametrize("raw", ["", "false", "0", "no"])
def test_ds_center_gate_blank_or_false_stays_off(monkeypatch, raw):
    monkeypatch.setenv("ENABLE_DS_CENTER_GATE", raw)
    assert load_settings().ENABLE_DS_CENTER_GATE is False


def test_ali_ds_state_path_defaults_to_the_repo_root_file(monkeypatch):
    monkeypatch.delenv("ALI_DS_STATE_PATH", raising=False)
    assert load_settings().ALI_DS_STATE_PATH == "ali_ds_state.json"


def test_ali_ds_state_path_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("ALI_DS_STATE_PATH", "/tmp/ali-state.json")
    assert load_settings().ALI_DS_STATE_PATH == "/tmp/ali-state.json"


def test_ali_ds_state_path_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ALI_DS_STATE_PATH", "")
    assert load_settings().ALI_DS_STATE_PATH == "ali_ds_state.json"


# ----------------------------------------------------------------------
# .env / .env.example alignment
# ----------------------------------------------------------------------


def _declared_keys(path: Path):
    """Config keys in file order; commented-out declarations included."""
    pattern = re.compile(r"^#?([A-Za-z_][A-Za-z0-9_]*)=")
    return [
        match.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := pattern.match(line))
    ]


def test_env_declares_the_same_keys_in_the_same_order_as_env_example():
    root = Path(__file__).resolve().parents[1]
    live = root / ".env"
    if not live.exists():
        pytest.skip(".env is operator-local (gitignored) and absent here")
    assert _declared_keys(live) == _declared_keys(root / ".env.example")