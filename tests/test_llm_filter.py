"""Offline unit tests for `src.evaluators.llm_filter` (plan F.4).

The LLM client is a stub returning canned `ProvisionalProductEvaluation`
objects — no network, no real model. Verifies the anti-hallucination
plumbing around the call: prompt content, the reconcile-against-real-COGS
margin gate, and the final programmatic mapping.
"""

import json
from typing import List

import pytest

from src.evaluators.llm_filter import (
    LLMConfigError,
    LLMEvaluationFilter,
    _reconcile,
    build_messages,
)
from src.models import (
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)

_ALI_URL = "https://www.aliexpress.com/item/1005006112233445.html"
_IMAGES = [
    "https://ae01.alicdn.com/kf/S1.jpg",
    "https://ae01.alicdn.com/kf/S2.jpg",
    "https://ae01.alicdn.com/kf/S3.jpg",
]


def _raw_product(**overrides) -> RawSupplierProduct:
    base = dict(
        supplier_name="AliExpress",
        supplier_retail_url=_ALI_URL,
        product_title="Ergonomic Desk Cable Spine Organiser",
        product_description="Modular cable spine with steel base.",
        price_aud=12.50,
        shipping_cost_aud=4.30,
        image_urls=list(_IMAGES),
    )
    base.update(overrides)
    return RawSupplierProduct(**base)


def _provisional(**overrides) -> ProvisionalProductEvaluation:
    base = dict(
        verdict="ACCEPT",
        niche_category="Home Office",
        problem_solved="Cable clutter",
        suggested_retail_aud=49.99,
        marketing_ad_copy="Tame the cable snake.",
        saturation_risk="LOW",
        target_tags=["dropship"],
        key_features=["Modular", "Steel base", "Under-desk"],
    )
    base.update(overrides)
    return ProvisionalProductEvaluation(**base)


class FakeInstructorClient:
    """Stands in for the patched AsyncOpenAI client; returns canned outputs."""

    def __init__(self, results: List[ProvisionalProductEvaluation]):
        self.results = list(results)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            raise RuntimeError("no canned results left")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# ----------------------------------------------------------------------
# Prompt construction (F.4 anti-hallucination contract)
# ----------------------------------------------------------------------


def test_prompt_contains_supplier_data_and_country():
    raw = _raw_product()
    user = build_messages(raw)[1]["content"]
    assert raw.product_title in user
    assert raw.product_description in user
    assert raw.supplier_name in user
    assert json.loads(user.split("\n\n", 1)[1])["price_aud"] == 12.50
    assert json.loads(user.split("\n\n", 1)[1])["target_country"] == "AU"


def test_prompt_carries_the_real_listed_price_not_an_estimate():
    payload = json.loads(build_messages(_raw_product())[1]["content"].split("\n\n", 1)[1])
    assert payload["shipping_cost_aud"] == 4.30


def test_system_prompt_demands_margin_floor_and_forbids_urls():
    system = build_messages(_raw_product())[0]["content"]
    assert "2.5x" in system
    assert "dropship" in system
    assert "REJECT" in system


def test_system_prompt_prices_realistically_instead_of_by_multiplier():
    # Phase 5: the model must price for the AU market in the store's niche and
    # must not mechanically apply a fixed 3x-4x multiplier to the real cost.
    system = build_messages(_raw_product())[0]["content"]
    assert "fixed 3x-4x multiplier" in system
    assert "Modern Arab-Aussie Lifestyle & Cultural Nostalgia" in system
    # The cost is authoritative when a quote backs it: no cheaper basis may
    # be invented.
    assert "VERIFIED" in system
    assert "do not invent a cheaper basis" in system


def test_system_prompt_distinguishes_an_unquoted_cost_from_a_verified_one():
    # AliExpress quotes no freight, so its landed cost is a floor.
    # Describing it as verified is how margin gets overstated, so the prompt
    # must tell the model which kind of number it is judging.
    system = build_messages(_raw_product())[0]["content"]
    assert "shipping_quoted" in system
    assert "FLOOR rather than a verified cost" in system


def test_payload_carries_the_shipping_quote_state():
    quoted = _raw_product()  # supplier-quoted shipping
    unquoted = _raw_product(shipping_cost_aud=0.0)  # no quote, as AliExpress
    assert '"shipping_quoted": true' in build_messages(quoted)[1]["content"]
    assert '"shipping_quoted": false' in build_messages(unquoted)[1]["content"]


# ----------------------------------------------------------------------
# Reconcile: margin math recomputed against REAL supplier cost
# ----------------------------------------------------------------------


def test_reconcile_keeps_an_accept_clearing_the_margin_floor():
    raw = _raw_product()  # landed COGS 16.80
    provisional = _provisional(suggested_retail_aud=49.99)  # markup ~2.98x... wait
    result = _reconcile(provisional, raw)
    # 49.99 - 16.80 = 33.19 > 25 — passes on the margin alternative.
    assert result.verdict == "ACCEPT"


def test_reconcile_keeps_a_realistic_price_under_the_relaxed_floor():
    # A real DS Center cost basis (COGS 4.00) priced at a realistic AU retail
    # of 11.00 is 2.75x — below the original 3.0x arm and under the AUD 25
    # margin arm, so the OLD floor would have downgraded it. The relaxed floor
    # (2.5x / AUD 20) is what lets realistic premium pricing survive.
    raw = _raw_product(price_aud=4.00, shipping_cost_aud=0.0)
    result = _reconcile(_provisional(suggested_retail_aud=11.00), raw)
    assert result.verdict == "ACCEPT"


def test_reconcile_downgrades_an_accept_below_the_relaxed_floor():
    # 7.00 vs COGS 4.00 is 1.75x with AUD 3.00 margin — under both arms.
    raw = _raw_product(price_aud=4.00, shipping_cost_aud=0.0)
    result = _reconcile(_provisional(suggested_retail_aud=7.00), raw)
    assert result.verdict == "REJECT"


def test_reconcile_downgrades_accept_failing_both_floor_arms():
    raw = _raw_product()
    provisional = _provisional(suggested_retail_aud=20.00)  # 1.19x, margin 3.20
    result = _reconcile(provisional, raw)
    assert result.verdict == "REJECT"


def test_reconcile_leaves_reject_verdicts_untouched():
    raw = _raw_product()
    provisional = _provisional(verdict="REJECT", suggested_retail_aud=100.00)
    assert _reconcile(provisional, raw).verdict == "REJECT"


# ----------------------------------------------------------------------
# LLMEvaluationFilter wiring
# ----------------------------------------------------------------------


def test_missing_llm_config_raises_llm_config_error(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(LLMConfigError, match="LLM_BASE_URL"):
        LLMEvaluationFilter()


async def test_evaluate_returns_final_evaluation_with_programmatic_fields():
    raw = _raw_product()
    fake = FakeInstructorClient([_provisional()])
    evaluator = LLMEvaluationFilter(client=fake, model="test-model")
    final = await evaluator.evaluate(raw)

    # LLM-authored fields from the canned provisional...
    assert final.verdict == "ACCEPT"
    assert final.marketing_ad_copy == "Tame the cable snake."
    # ...supplier/cost fields mapped from the raw product, never the LLM.
    assert final.supplier_name == "AliExpress"
    assert final.supplier_retail_url == _ALI_URL
    assert final.estimated_cogs_aud == 16.80
    assert final.estimated_margin_aud == 33.19
    assert "http" not in final.cogs_estimation_basis.lower()
    # The instructor call carried the provisional response model only.
    assert fake.calls[0]["response_model"] is ProvisionalProductEvaluation
    assert fake.calls[0]["model"] == "test-model"


async def test_evaluate_propagates_llm_call_failure():
    fake = FakeInstructorClient([RuntimeError("boom")])
    evaluator = LLMEvaluationFilter(client=fake, model="test-model")
    with pytest.raises(RuntimeError):
        await evaluator.evaluate(_raw_product())


async def test_evaluate_does_not_pass_image_urls_to_the_llm():
    raw = _raw_product()
    fake = FakeInstructorClient([_provisional()])
    await LLMEvaluationFilter(client=fake, model="test-model").evaluate(raw)
    user_content = fake.calls[0]["messages"][1]["content"]
    for image_url in _IMAGES:
        assert image_url not in user_content
    assert "alicdn" not in user_content


async def test_evaluate_margin_downgrade_surfaces_as_reject():
    raw = _raw_product()
    fake = FakeInstructorClient([_provisional(suggested_retail_aud=20.00)])
    final = await LLMEvaluationFilter(client=fake, model="test-model").evaluate(raw)
    assert final.verdict == "REJECT"