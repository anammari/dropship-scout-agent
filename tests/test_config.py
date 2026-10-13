"""Unit tests for `src.config` settings resolution.

Uses the `load_settings()` factory (not the cached `settings` singleton)
so each test is isolated from import order / env pollution.
"""

import re
from pathlib import Path

import pytest

from src.config import digest_path, load_settings


# ----------------------------------------------------------------------
# LLM config
# ----------------------------------------------------------------------


def test_llm_model_defaults_to_the_live_deepseek_flash_build(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert load_settings().LLM_MODEL == "deepseek-v4.1-flash"


def test_llm_model_env_override_still_respected(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-other-model:cloud")
    assert load_settings().LLM_MODEL == "some-other-model:cloud"


def test_llm_model_empty_string_normalises_to_default(monkeypatch):
    # An empty pin is treated as unset, not as a literal empty model name.
    monkeypatch.setenv("LLM_MODEL", "")
    assert load_settings().LLM_MODEL == "deepseek-v4.1-flash"


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


def test_supplier_priority_order_defaults_to_cjdropshipping_only(monkeypatch):
    # The Morning Anthem sources from CJdropshipping exclusively, so the
    # default chain has exactly one engine.
    monkeypatch.delenv("SUPPLIER_PRIORITY_ORDER", raising=False)
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["cjdropshipping"]


def test_supplier_priority_order_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "cjdropshipping")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["cjdropshipping"]


def test_supplier_priority_order_is_normalised(monkeypatch):
    # Keys are trimmed and lowercased so " CJdropshipping " and shell quoting
    # quirks resolve to canonical extractor keys.
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", " CJdropshipping ")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["cjdropshipping"]


def test_supplier_priority_order_empty_string_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", "")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["cjdropshipping"]


def test_supplier_priority_order_drops_blank_segments(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PRIORITY_ORDER", ",,cjdropshipping,,")
    assert load_settings().SUPPLIER_PRIORITY_ORDER == ["cjdropshipping"]


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


def test_min_margin_aud_defaults_to_the_widened_floor(monkeypatch):
    # Widened to AUD 10 on 2026-09-29 to match the operator's Step-3 $10
    # target, which the old AUD 20 default silently overrode.
    monkeypatch.delenv("MIN_MARGIN_AUD", raising=False)
    assert load_settings().MIN_MARGIN_AUD == 10.0


def test_min_margin_aud_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MIN_MARGIN_AUD", "25")
    assert load_settings().MIN_MARGIN_AUD == 25.0


# ----------------------------------------------------------------------
# Updated-pipeline deliverable paths
# ----------------------------------------------------------------------


def test_gold_products_path_defaults_to_the_step_3_deliverable(monkeypatch):
    monkeypatch.delenv("MORNING_ANTHEM_GOLD_PRODUCTS_PATH", raising=False)
    assert (
        load_settings().MORNING_ANTHEM_GOLD_PRODUCTS_PATH
        == "outputs/json/morning-anthem-step-3-gold-products.json"
    )


def test_trend_keywords_path_defaults_to_the_step_2_deliverable(monkeypatch):
    monkeypatch.delenv("MORNING_ANTHEM_TREND_KEYWORDS_PATH", raising=False)
    assert (
        load_settings().MORNING_ANTHEM_TREND_KEYWORDS_PATH
        == "outputs/json/morning-anthem-step-2-trend-keywords.json"
    )


def test_hasdata_api_key_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("HASDATA_API_KEY", raising=False)
    assert load_settings().HASDATA_API_KEY is None


def test_hasdata_api_key_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("HASDATA_API_KEY", "hd-test")
    assert load_settings().HASDATA_API_KEY == "hd-test"


def test_hasdata_api_key_empty_string_normalises_to_none(monkeypatch):
    monkeypatch.setenv("HASDATA_API_KEY", "")
    assert load_settings().HASDATA_API_KEY is None


def test_hasdata_gs_base_url_defaults_to_the_rest_api(monkeypatch):
    monkeypatch.delenv("HASDATA_GS_BASE_URL", raising=False)
    assert load_settings().HASDATA_GS_BASE_URL == "https://api.hasdata.com"


def test_hasdata_gs_base_url_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("HASDATA_GS_BASE_URL", "")
    assert load_settings().HASDATA_GS_BASE_URL == "https://api.hasdata.com"


def test_hasdata_gs_base_url_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("HASDATA_GS_BASE_URL", "https://proxy.test")
    assert load_settings().HASDATA_GS_BASE_URL == "https://proxy.test"


def test_hasdata_gs_max_results_defaults_to_twenty(monkeypatch):
    # The local grid cap, NOT an API parameter (one request = 10 credits
    # whatever the slice); 20 keeps the curation budget tight under the
    # Morning Anthem credit envelope.
    monkeypatch.delenv("HASDATA_GS_MAX_RESULTS_PER_KEYWORD", raising=False)
    assert load_settings().HASDATA_GS_MAX_RESULTS_PER_KEYWORD == 20


def test_hasdata_gs_max_results_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("HASDATA_GS_MAX_RESULTS_PER_KEYWORD", "65")
    assert load_settings().HASDATA_GS_MAX_RESULTS_PER_KEYWORD == 65


def test_hasdata_gs_max_results_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("HASDATA_GS_MAX_RESULTS_PER_KEYWORD", "")
    assert load_settings().HASDATA_GS_MAX_RESULTS_PER_KEYWORD == 20


def test_keyword_bank_path_defaults_to_the_step_4_deliverable(monkeypatch):
    monkeypatch.delenv("MORNING_ANTHEM_KEYWORD_BANK_PATH", raising=False)
    assert (
        load_settings().MORNING_ANTHEM_KEYWORD_BANK_PATH
        == "outputs/json/morning-anthem-step-4-keyword-bank.json"
    )


def test_keyword_bank_path_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MORNING_ANTHEM_KEYWORD_BANK_PATH", "/tmp/bank.json")
    assert load_settings().MORNING_ANTHEM_KEYWORD_BANK_PATH == "/tmp/bank.json"


def test_keyword_bank_path_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("MORNING_ANTHEM_KEYWORD_BANK_PATH", "")
    assert (
        load_settings().MORNING_ANTHEM_KEYWORD_BANK_PATH
        == "outputs/json/morning-anthem-step-4-keyword-bank.json"
    )


def test_digest_path_moves_the_markdown_out_of_the_json_folder():
    # The split is only real if a default run honours it: with_suffix would
    # write the digest back into outputs/json/ beside its source.
    assert (
        digest_path(Path("outputs/json/step-4-gold-keywords.json"))
        == Path("outputs/md/step-4-gold-keywords.md")
    )


def test_digest_path_keeps_a_custom_destination_beside_its_source():
    # A --output outside the organised tree must not be silently relocated
    # into outputs/md/.
    assert digest_path(Path("/tmp/bank.json")) == Path("/tmp/bank.md")


def test_digest_path_ignores_a_json_folder_outside_the_outputs_tree():
    # Only the `outputs/json` pair is specialised: a folder that merely shares
    # the name `json` is not the organised tree.
    assert digest_path(Path("/tmp/json/report.json")) == Path("/tmp/json/report.md")


def test_digest_path_specialises_an_absolute_outputs_json_path():
    # An absolute --output pointing into the real tree still gets the split.
    assert (
        digest_path(Path("/repo/outputs/json/step-4-gold-keywords.json"))
        == Path("/repo/outputs/md/step-4-gold-keywords.md")
    )


def test_bank_target_per_keyword_defaults_to_two(monkeypatch):
    # Per (keyword, engine) leg: the bank's keyword count multiplies it, so
    # the default has to stay small for a 60-keyword bank to stay tractable.
    monkeypatch.delenv("BANK_TARGET_PER_PRODUCT", raising=False)
    assert load_settings().BANK_TARGET_PER_PRODUCT == 2


def test_bank_target_per_keyword_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("BANK_TARGET_PER_PRODUCT", "1")
    assert load_settings().BANK_TARGET_PER_PRODUCT == 1


def test_bank_target_per_keyword_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("BANK_TARGET_PER_PRODUCT", "")
    assert load_settings().BANK_TARGET_PER_PRODUCT == 2


def test_cleanup_review_keep_defaults_to_ten(monkeypatch):
    # The only number that decides a borderline deletion: shortlist is always
    # kept, disregard always deleted.
    monkeypatch.delenv("CLEANUP_REVIEW_KEEP", raising=False)
    assert load_settings().CLEANUP_REVIEW_KEEP == 10


def test_cleanup_review_keep_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CLEANUP_REVIEW_KEEP", "4")
    assert load_settings().CLEANUP_REVIEW_KEEP == 4


def test_ranked_candidates_path_defaults_to_the_step_6_report(monkeypatch):
    monkeypatch.delenv("MORNING_ANTHEM_RANKED_CANDIDATES_PATH", raising=False)
    assert (
        load_settings().MORNING_ANTHEM_RANKED_CANDIDATES_PATH
        == "outputs/json/morning-anthem-step-6-ranked-candidates.json"
    )


def test_morning_anthem_export_dir_defaults_to_the_gold_kernel_tree(monkeypatch):
    # Distinct from EXPORT_DIR: the gold-kernel intake is its own tree, with
    # the CJdropshipping supplier subfolder under it.
    monkeypatch.delenv("MORNING_ANTHEM_EXPORT_DIR", raising=False)
    assert load_settings().MORNING_ANTHEM_EXPORT_DIR == (
        "/Users/ahmadammari/PD/my-store-build/inspiration/"
        "morning-anthem-candidates"
    )


def test_morning_anthem_export_dir_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("MORNING_ANTHEM_EXPORT_DIR", "/tmp/ma-candidates")
    assert load_settings().MORNING_ANTHEM_EXPORT_DIR == "/tmp/ma-candidates"


def test_morning_anthem_export_dir_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("MORNING_ANTHEM_EXPORT_DIR", "")
    assert load_settings().MORNING_ANTHEM_EXPORT_DIR == (
        "/Users/ahmadammari/PD/my-store-build/inspiration/"
        "morning-anthem-candidates"
    )


# ----------------------------------------------------------------------
# The Morning Anthem: AU-first demand geography & HasData budget guardrails
# ----------------------------------------------------------------------


def test_demand_geography_defaults_to_the_au_market(monkeypatch):
    # Australia-first launch: demand signals and price benchmarks must reflect
    # Australian shopper behaviour and AUD retail pricing.
    for key in ("TRENDS_GEO", "SHOPPING_GL", "SHOPPING_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    loaded = load_settings()
    assert loaded.TRENDS_GEO == "AU"
    assert loaded.SHOPPING_GL == "au"
    assert loaded.SHOPPING_DOMAIN == "google.com.au"


def test_demand_geography_env_overrides_are_respected(monkeypatch):
    monkeypatch.setenv("TRENDS_GEO", "US")
    monkeypatch.setenv("SHOPPING_GL", "us")
    monkeypatch.setenv("SHOPPING_DOMAIN", "google.com")
    loaded = load_settings()
    assert (loaded.TRENDS_GEO, loaded.SHOPPING_GL, loaded.SHOPPING_DOMAIN) == (
        "US", "us", "google.com",
    )


def test_target_country_defaults_to_australia_for_the_launch(monkeypatch):
    monkeypatch.delenv("TARGET_COUNTRY", raising=False)
    assert load_settings().TARGET_COUNTRY == "AU"


def test_cj_freight_destination_defaults_to_the_australian_lane(monkeypatch):
    # All launch products ship from CJ's China warehouse directly into
    # Australia, so the freight quote must target an Australian lane.
    monkeypatch.delenv("CJ_DEFAULT_DEST_COUNTRY", raising=False)
    assert load_settings().CJ_DEFAULT_DEST_COUNTRY == "AU"


def test_cj_freight_destination_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("CJ_DEFAULT_DEST_COUNTRY", "US")
    assert load_settings().CJ_DEFAULT_DEST_COUNTRY == "US"


def test_cj_freight_destination_blank_falls_back_to_australia(monkeypatch):
    monkeypatch.setenv("CJ_DEFAULT_DEST_COUNTRY", "")
    assert load_settings().CJ_DEFAULT_DEST_COUNTRY == "AU"


def test_hasdata_budget_guardrails_have_their_defaults(monkeypatch):
    for key in (
        "HASDATA_MONTHLY_BUDGET",
        "HASDATA_PRODUCTION_CAP",
        "HASDATA_GS_MAX_KEYWORDS",
        "TRENDS_MAX_CALLS",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = load_settings()
    assert loaded.HASDATA_MONTHLY_BUDGET == 1000
    assert loaded.HASDATA_PRODUCTION_CAP == 400
    assert loaded.HASDATA_GS_MAX_KEYWORDS == 30
    assert loaded.TRENDS_MAX_CALLS == 10


def test_credit_rates_have_their_defaults(monkeypatch):
    # Trends is budgeted at the OBSERVED 10 credits/call (2026-10-12: two live
    # pilot calls moved the account balance by exactly 20) even though the
    # published tables say 5 — the conservative figure wins until the
    # dashboard usage log settles it.
    monkeypatch.delenv("HASDATA_TRENDS_CREDITS_PER_CALL", raising=False)
    monkeypatch.delenv("HASDATA_SHOPPING_CREDITS_PER_REQUEST", raising=False)
    loaded = load_settings()
    assert loaded.HASDATA_TRENDS_CREDITS_PER_CALL == 10
    assert loaded.HASDATA_SHOPPING_CREDITS_PER_REQUEST == 10


def test_credit_rates_are_env_overridable(monkeypatch):
    monkeypatch.setenv("HASDATA_TRENDS_CREDITS_PER_CALL", "5")
    monkeypatch.setenv("HASDATA_SHOPPING_CREDITS_PER_REQUEST", "10")
    loaded = load_settings()
    assert loaded.HASDATA_TRENDS_CREDITS_PER_CALL == 5


def test_the_launch_run_envelope_equals_the_production_cap(monkeypatch):
    # The launch-run profile is sized so the two halves sum to the cap
    # exactly (Step 2: 10 calls x 10 = 100; Step 3: 30 keywords x 10 = 300),
    # so HASDATA_PRODUCTION_CAP is the arithmetic, not an arbitrary ceiling.
    for key in (
        "HASDATA_PRODUCTION_CAP",
        "HASDATA_TRENDS_CREDITS_PER_CALL",
        "HASDATA_SHOPPING_CREDITS_PER_REQUEST",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = load_settings()
    trend_credits = (
        loaded.TRENDS_MAX_CALLS * loaded.HASDATA_TRENDS_CREDITS_PER_CALL
    )
    shopping_credits = (
        loaded.HASDATA_GS_MAX_KEYWORDS
        * loaded.HASDATA_SHOPPING_CREDITS_PER_REQUEST
    )
    assert trend_credits == 100
    assert shopping_credits == 300
    assert trend_credits + shopping_credits == loaded.HASDATA_PRODUCTION_CAP
    # The monthly tier keeps a reserve for the per-step pilots and reruns.
    assert loaded.HASDATA_PRODUCTION_CAP <= loaded.HASDATA_MONTHLY_BUDGET
    assert (
        loaded.HASDATA_MONTHLY_BUDGET - loaded.HASDATA_PRODUCTION_CAP >= 500
    )


def test_hasdata_budget_env_overrides_are_respected(monkeypatch):
    monkeypatch.setenv("HASDATA_MONTHLY_BUDGET", "5000")
    monkeypatch.setenv("HASDATA_PRODUCTION_CAP", "400")
    monkeypatch.setenv("HASDATA_GS_MAX_KEYWORDS", "10")
    monkeypatch.setenv("TRENDS_MAX_CALLS", "4")
    loaded = load_settings()
    assert loaded.HASDATA_MONTHLY_BUDGET == 5000
    assert loaded.HASDATA_PRODUCTION_CAP == 400
    assert loaded.HASDATA_GS_MAX_KEYWORDS == 10
    assert loaded.TRENDS_MAX_CALLS == 4


def test_jev_gate_batch_sizes_have_their_defaults(monkeypatch):
    monkeypatch.delenv("TREND_GATE_BATCH_SIZE", raising=False)
    monkeypatch.delenv("PILLAR_GATE_BATCH_SIZE", raising=False)
    loaded = load_settings()
    assert loaded.TREND_GATE_BATCH_SIZE == 8
    assert loaded.PILLAR_GATE_BATCH_SIZE == 4


def test_jev_gate_batch_sizes_env_overrides_are_respected(monkeypatch):
    monkeypatch.setenv("TREND_GATE_BATCH_SIZE", "12")
    monkeypatch.setenv("PILLAR_GATE_BATCH_SIZE", "6")
    loaded = load_settings()
    assert loaded.TREND_GATE_BATCH_SIZE == 12
    assert loaded.PILLAR_GATE_BATCH_SIZE == 6


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

# ----------------------------------------------------------------------
# No client may fall back to a live default when a credential is absent
# ----------------------------------------------------------------------


def test_no_real_client_reaches_a_live_endpoint_without_credentials():
    """Blanking the credentials must fail every client closed.

    `tests/conftest.py` blanks `settings.LLM_API_KEY`, `settings.HASDATA_API_KEY`
    and `settings.OPENROUTER_API_KEY` for the whole suite, so a test that
    accidentally builds a real client gets a config error instead of spending the
    operator's balance. That guarantee only holds if no client has a hardcoded
    fallback that would let it proceed — this is the guard for that, and it fails
    the moment someone adds an endpoint default that makes a credential optional.
    """
    from src.evaluators.gold_curator import GoldCurationConfigError, GoldProductCurator
    from src.evaluators.llm_filter import LLMConfigError, LLMEvaluationFilter
    from src.extractors.google_shopping_hasdata import (
        HasDataNotConfiguredError,
        HasDataShoppingScraper,
    )
    from src.ranking.jev_client import JevClient, JevConfigError

    with pytest.raises(LLMConfigError):
        LLMEvaluationFilter()
    with pytest.raises(GoldCurationConfigError):
        GoldProductCurator()
    with pytest.raises(HasDataNotConfiguredError):
        HasDataShoppingScraper()
    with pytest.raises(JevConfigError):
        JevClient()


def test_the_supplier_engine_fails_closed_without_its_token():
    # CJ is the only supplier, so an unconfigured token must raise rather than
    # attempt a session — the conftest blanks it on the settings singleton.
    import asyncio

    from src.extractors.base import ExtractorNotConfiguredError
    from src.extractors.cj_mcp_extractor import CjMcpExtractor

    with pytest.raises(ExtractorNotConfiguredError, match="CJ_MCP_TOKEN"):
        asyncio.run(CjMcpExtractor().fetch_products(["garlic press"]))
