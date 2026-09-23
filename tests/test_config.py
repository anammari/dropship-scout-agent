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
        "LLM_BASE_URL",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    fresh = load_settings()
    # Plain str (not Optional/None) — extractor-level NotConfigured checks
    # rely on a plain falsy value.
    for name in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
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


def test_supplier_priority_order_defaults_to_cjdropshipping_first(monkeypatch):
    monkeypatch.delenv("SUPPLIER_PRIORITY_ORDER", raising=False)
    assert load_settings().SUPPLIER_PRIORITY_ORDER == [
        "cjdropshipping",
        "aliexpress",
    ]


def test_supplier_priority_order_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "aliexpress,cjdropshipping")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == [
        "aliexpress",
        "cjdropshipping",
    ]


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
    ]


def test_supplier_priority_order_drops_blank_segments(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "aliexpress,,,cjdropshipping")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == [
        "aliexpress",
        "cjdropshipping",
    ]


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
# AliExpress Dropshipping Center ingestion
# ----------------------------------------------------------------------


def test_ali_ds_state_path_defaults_to_the_repo_root_file(monkeypatch):
    monkeypatch.delenv("ALI_DS_STATE_PATH", raising=False)
    assert load_settings().ALI_DS_STATE_PATH == "ali_ds_state.json"


def test_ali_ds_state_path_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("ALI_DS_STATE_PATH", "/tmp/ali-state.json")
    assert load_settings().ALI_DS_STATE_PATH == "/tmp/ali-state.json"


def test_ali_ds_state_path_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ALI_DS_STATE_PATH", "")
    assert load_settings().ALI_DS_STATE_PATH == "ali_ds_state.json"


def test_ali_ds_max_products_defaults_to_twenty(monkeypatch):
    monkeypatch.delenv("ALI_DS_MAX_PRODUCTS", raising=False)
    assert load_settings().ALI_DS_MAX_PRODUCTS == 20


def test_ali_ds_max_products_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("ALI_DS_MAX_PRODUCTS", "5")
    assert load_settings().ALI_DS_MAX_PRODUCTS == 5


def test_min_ds_order_count_defaults_to_five_hundred(monkeypatch):
    monkeypatch.delenv("MIN_DS_ORDER_COUNT", raising=False)
    assert load_settings().MIN_DS_ORDER_COUNT == 500


def test_min_ds_order_count_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_DS_ORDER_COUNT", "1200")
    assert load_settings().MIN_DS_ORDER_COUNT == 1200


def test_min_ds_rating_defaults_to_four_point_five(monkeypatch):
    monkeypatch.delenv("MIN_DS_RATING", raising=False)
    assert load_settings().MIN_DS_RATING == 4.5


def test_min_ds_rating_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_DS_RATING", "4.8")
    assert load_settings().MIN_DS_RATING == 4.8


def test_min_cj_listed_count_defaults_to_one_hundred_and_fifty(monkeypatch):
    # Raised from the original 20 after the live payload spike: on real AU
    # catalogue pages a floor of 20 passed every hit, so it filtered nothing.
    monkeypatch.delenv("MIN_CJ_LISTED_COUNT", raising=False)
    assert load_settings().MIN_CJ_LISTED_COUNT == 150


def test_min_cj_listed_count_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_CJ_LISTED_COUNT", "400")
    assert load_settings().MIN_CJ_LISTED_COUNT == 400


def test_cj_freight_method_defaults_to_blank_meaning_cheapest(monkeypatch):
    monkeypatch.delenv("CJ_FREIGHT_METHOD", raising=False)
    assert load_settings().CJ_FREIGHT_METHOD == ""


def test_cj_freight_method_blank_pin_normalises_to_cheapest(monkeypatch):
    monkeypatch.setenv("CJ_FREIGHT_METHOD", "   ")
    assert load_settings().CJ_FREIGHT_METHOD == ""


def test_cj_freight_method_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CJ_FREIGHT_METHOD", "CJPacket Eub")
    assert load_settings().CJ_FREIGHT_METHOD == "CJPacket Eub"


def test_min_markup_multiplier_defaults_to_the_relaxed_floor(monkeypatch):
    # Relaxed from the original 3.0x so realistic AU pricing against a real
    # DS Center cost is not auto-rejected downstream.
    monkeypatch.delenv("MIN_MARKUP_MULTIPLIER", raising=False)
    assert load_settings().MIN_MARKUP_MULTIPLIER == 2.5


def test_min_markup_multiplier_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_MARKUP_MULTIPLIER", "3.0")
    assert load_settings().MIN_MARKUP_MULTIPLIER == 3.0


def test_min_margin_aud_defaults_to_the_relaxed_floor(monkeypatch):
    monkeypatch.delenv("MIN_MARGIN_AUD", raising=False)
    assert load_settings().MIN_MARGIN_AUD == 20.0


def test_min_margin_aud_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_MARGIN_AUD", "25")
    assert load_settings().MIN_MARGIN_AUD == 25.0


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