"""Offline unit tests for the Supplier-First data contract (plan F.2).

Covers `RawSupplierProduct` strict validation, the LLM-only
`ProvisionalProductEvaluation`, and the programmatic merge in
`ProductCandidateEvaluation.from_raw` — including the zero-URL COGS
contract and the ACCEPT-only margin gates. No LLM, no network.
"""

import pytest
from pydantic import ValidationError

from src.models import (
    KNOWN_SUPPLIERS,
    ProductCandidateEvaluation,
    ProvisionalProductEvaluation,
    RawSupplierProduct,
)

_ALI_URL = "https://www.aliexpress.com/item/1005006112233445.html"
_ETSY_URL = "https://www.etsy.com/listing/123456789/wooden-desk-organiser"
_CJ_URL = "https://developers.cjdropshipping.com/product/12345678.html"
_IMG1 = "https://ae01.alicdn.com/kf/S123.jpg"
_IMG2 = "https://ae01.alicdn.com/kf/S456.jpg"
_IMG3 = "https://ae01.alicdn.com/kf/S789.jpg"


def _raw_product(**overrides) -> RawSupplierProduct:
    base = dict(
        supplier_name="AliExpress",
        supplier_retail_url=_ALI_URL,
        product_title="Ergonomic Desk Cable Spine Organiser",
        product_description="Modular cable management spine with a steel base.",
        price_aud=12.50,
        shipping_cost_aud=4.30,
        image_urls=[_IMG1, _IMG2, _IMG3],
    )
    base.update(overrides)
    return RawSupplierProduct(**base)


def _provisional(**overrides) -> ProvisionalProductEvaluation:
    base = dict(
        verdict="ACCEPT",
        niche_category="Home Office",
        problem_solved="Desk cable clutter",
        suggested_retail_aud=49.99,
        marketing_ad_copy="Tame the cable snake under your desk.",
        saturation_risk="LOW",
        target_tags=["dropship", "workspace"],
        key_features=["Modular segments", "Steel base", "Under-desk mount"],
    )
    base.update(overrides)
    return ProvisionalProductEvaluation(**base)


def _low_cogs(**overrides) -> RawSupplierProduct:
    """A real-DS-Center-shaped cost: AUD 4.00 landed, no quoted shipping."""
    return _raw_product(price_aud=4.00, shipping_cost_aud=0.0, **overrides)


def _final(provisional=None, raw=None) -> ProductCandidateEvaluation:
    return ProductCandidateEvaluation.from_raw(
        provisional or _provisional(), raw or _raw_product()
    )


# ----------------------------------------------------------------------
# RawSupplierProduct validation
# ----------------------------------------------------------------------


def test_valid_raw_product_round_trips():
    raw = _raw_product()
    assert raw.supplier_name == "AliExpress"
    assert raw.price_aud == 12.50
    assert raw.shipping_cost_aud == 4.30
    assert raw.image_urls == [_IMG1, _IMG2, _IMG3]


def test_all_three_suppliers_are_known():
    assert KNOWN_SUPPLIERS == {"AliExpress", "Etsy", "CJdropshipping"}
    for supplier, url in [
        ("AliExpress", _ALI_URL),
        ("Etsy", _ETSY_URL),
        ("CJdropshipping", _CJ_URL),
    ]:
        assert _raw_product(supplier_name=supplier, supplier_retail_url=url)


def test_unknown_supplier_name_is_rejected():
    with pytest.raises(ValidationError, match="supplier_name"):
        _raw_product(supplier_name="Taobao")


def test_supplier_url_must_be_absolute():
    with pytest.raises(ValidationError, match="supplier_retail_url"):
        _raw_product(supplier_retail_url="/item/123.html")


def test_supplier_url_must_match_the_supplier_pdp_shape():
    # An AliExpress-named product whose URL is an Etsy listing shape.
    with pytest.raises(ValidationError, match="product-page shape"):
        _raw_product(supplier_retail_url=_ETSY_URL)
    # A CJ-named product pointing at an AliExpress item page.
    with pytest.raises(ValidationError, match="product-page shape"):
        _raw_product(
            supplier_name="CJdropshipping", supplier_retail_url=_ALI_URL
        )


def test_search_gateway_urls_are_rejected():
    with pytest.raises(ValidationError, match="search"):
        _raw_product(supplier_retail_url="https://www.aliexpress.com/w/wholesale-desk.html")
    with pytest.raises(ValidationError, match="search"):
        _raw_product(supplier_retail_url="https://www.aliexpress.com/wholesale?SearchText=desk")


def test_cj_canonical_and_legacy_pdp_shapes_both_validate():
    legacy = "https://cjdropshipping.com/product/Desk-Organiser-p-1234567.html"
    assert _raw_product(
        supplier_name="CJdropshipping", supplier_retail_url=legacy
    )


def test_cj_uuid_pid_pdp_shapes_validate():
    # A large slice of CJ's catalog uses UUID-style pids, not numeric ones.
    uuid_pid = "B03F2DFF-276D-481C-AD18-28DF22E411CC"
    assert _raw_product(
        supplier_name="CJdropshipping",
        supplier_retail_url=f"https://cjdropshipping.com/product/{uuid_pid}.html",
    )
    assert _raw_product(
        supplier_name="CJdropshipping",
        supplier_retail_url=(
            "https://www.cjdropshipping.com/product/"
            f"kitchen-gadgets-garlic-peeler-p-{uuid_pid}.html"
        ),
    )


def test_cj_pdp_shape_still_rejects_non_product_paths():
    for bad in (
        "https://cjdropshipping.com/product/list.html",
        "https://cjdropshipping.com/search?keyword=gadgets",
    ):
        with pytest.raises(ValidationError):
            _raw_product(supplier_name="CJdropshipping", supplier_retail_url=bad)


def test_image_urls_require_three_distinct_absolute_urls():
    with pytest.raises(ValidationError, match="image_urls"):
        _raw_product(image_urls=[_IMG1, _IMG2])
    with pytest.raises(ValidationError, match="image_urls"):
        _raw_product(image_urls=[])
    # Duplicates do not count toward the minimum.
    with pytest.raises(ValidationError, match="DISTINCT"):
        _raw_product(image_urls=[_IMG1, _IMG1, _IMG1])
    with pytest.raises(ValidationError, match="image_urls"):
        _raw_product(image_urls=["not-a-url", _IMG2, _IMG3])


def test_duplicate_image_urls_are_deduped_in_order():
    raw = _raw_product(image_urls=[_IMG1, _IMG2, _IMG1, _IMG3, _IMG2])
    assert raw.image_urls == [_IMG1, _IMG2, _IMG3]


def test_negative_prices_are_rejected():
    with pytest.raises(ValidationError):
        _raw_product(price_aud=-1.0)
    with pytest.raises(ValidationError):
        _raw_product(shipping_cost_aud=-0.5)


# ----------------------------------------------------------------------
# ProvisionalProductEvaluation (LLM-only fields)
# ----------------------------------------------------------------------


def test_provisional_accept_requires_complete_marketing_payload():
    assert _provisional()
    with pytest.raises(ValidationError, match="marketing_ad_copy"):
        _provisional(marketing_ad_copy="  ")
    with pytest.raises(ValidationError, match="key_features"):
        _provisional(key_features=["only one feature"])
    with pytest.raises(ValidationError, match="dropship"):
        _provisional(target_tags=["workspace"])


def test_provisional_reject_has_no_completeness_gates():
    relaxed = _provisional(
        verdict="REJECT",
        marketing_ad_copy="",
        problem_solved="",
        key_features=[],
        target_tags=["ignored"],
    )
    assert relaxed.verdict == "REJECT"


def test_provisional_model_has_no_supplier_cogs_or_image_fields():
    # The LLM response model must not even be able to carry the fields it
    # is forbidden to author.
    field_names = set(ProvisionalProductEvaluation.model_fields)
    for forbidden in (
        "supplier_name",
        "supplier_retail_url",
        "estimated_cogs_aud",
        "cogs_estimation_basis",
        "image_urls",
        "estimated_margin_aud",
        "markup_multiplier",
    ):
        assert forbidden not in field_names


# ----------------------------------------------------------------------
# ProductCandidateEvaluation.from_raw (programmatic mapping)
# ----------------------------------------------------------------------


def test_from_raw_maps_supplier_fields_verbatim():
    final = _final()
    raw = _raw_product()
    assert final.supplier_name == raw.supplier_name
    assert final.supplier_retail_url == raw.supplier_retail_url


def test_from_raw_computes_cogs_from_real_supplier_price():
    final = _final()
    assert final.estimated_cogs_aud == 12.50 + 4.30
    assert "AUD $12.50" in final.cogs_estimation_basis
    assert "AUD $4.30" in final.cogs_estimation_basis
    # Zero-URL contract holds by construction.
    assert "http" not in final.cogs_estimation_basis.lower()


def test_from_raw_basis_notes_unquoted_shipping():
    final = _final(raw=_raw_product(shipping_cost_aud=0.0))
    assert final.estimated_cogs_aud == 12.50
    assert "does not quote shipping" in final.cogs_estimation_basis


# ----------------------------------------------------------------------
# The derived shipping notice (plan §7)
# ----------------------------------------------------------------------


def test_shipping_notice_is_derived_from_the_quote():
    final = _final(
        raw=_raw_product(
            shipping_cost_aud=6.67,
            shipping_method="CJPacket Eub",
            shipping_transit_days="6-10",
        )
    )
    assert final.shipping_notice_au == (
        "Standard tracked international shipping to Australia via "
        "CJPacket Eub: 6-10 business days."
    )


def test_shipping_notice_never_claims_free_shipping():
    # The hole this closes: the LLM shipped "Free standard shipping on this
    # item" against a real quoted freight cost.
    final = _final(
        raw=_raw_product(
            shipping_cost_aud=14.52,
            shipping_method="CJPacket Eub",
            shipping_transit_days="6-10",
        )
    )
    assert "free" not in final.shipping_notice_au.lower()


def test_shipping_notice_tolerates_a_transit_that_carries_its_own_unit():
    final = _final(
        raw=_raw_product(shipping_transit_days="6-10 days", shipping_method="PostNL")
    )
    assert final.shipping_notice_au.endswith("6-10 days.")


def test_shipping_notice_falls_back_when_the_quote_has_no_transit():
    final = _final(raw=_raw_product(shipping_transit_days=None))
    assert "7-12 business days" in final.shipping_notice_au


def test_basis_credits_the_quoted_shipping_service():
    final = _final(
        raw=_raw_product(shipping_cost_aud=6.67, shipping_method="CJPacket Eub")
    )
    assert "AUD $6.67" in final.cogs_estimation_basis
    assert "via CJPacket Eub" in final.cogs_estimation_basis


def test_the_llm_may_not_author_the_shipping_notice():
    # A forbidden field must fail loudly rather than be silently dropped —
    # silently ignoring it is how an invented claim survives to production.
    with pytest.raises(ValidationError):
        ProvisionalProductEvaluation(
            verdict="ACCEPT",
            niche_category="Home Office",
            problem_solved="Cable clutter",
            suggested_retail_aud=49.99,
            marketing_ad_copy="Tame the cable snake.",
            saturation_risk="LOW",
            target_tags=["dropship"],
            key_features=["Modular", "Steel base", "Under-desk"],
            shipping_notice_au="Free standard shipping on this item.",
        )


def test_from_raw_recomputes_margin_math_from_real_cogs():
    final = _final()
    cogs = final.estimated_cogs_aud
    assert final.estimated_margin_aud == round(49.99 - cogs, 2)
    assert final.markup_multiplier == round(49.99 / cogs, 2)
    # LLM-authored fields flow through untouched.
    assert final.marketing_ad_copy == "Tame the cable snake under your desk."
    assert final.key_features == ["Modular segments", "Steel base", "Under-desk mount"]


def test_from_raw_reject_verdict_skips_margin_gates():
    final = _final(_provisional(
        verdict="REJECT", suggested_retail_aud=13.00, target_tags=["ignored"]
    ))
    assert final.verdict == "REJECT"
    assert final.estimated_margin_aud == -3.80  # math still recorded honestly


def test_final_accept_failing_margin_floor_is_rejected_by_validator():
    # Retail 14.00 vs COGS 16.80: markup 0.83x, margin -2.80 — must raise.
    with pytest.raises(ValidationError, match="markup_multiplier"):
        _final(_provisional(suggested_retail_aud=14.00))


def test_final_accept_passes_on_margin_over_25_even_below_3x():
    # Retail 45.00 vs COGS 16.80: markup 2.68x but margin 28.20 > 25.
    final = _final(_provisional(suggested_retail_aud=45.00))
    assert final.verdict == "ACCEPT"
    assert final.markup_multiplier < 3.0


def test_final_accept_survives_the_relaxed_floor_below_3x():
    # COGS 4.00 with a 2.75x retail: the original 3.0x / AUD 25 floor rejected
    # this, which is exactly the realistic AU pricing the DS Center cost basis
    # now produces. The floor is relaxed to 2.5x / AUD 20.
    final = _final(_provisional(suggested_retail_aud=11.00), raw=_low_cogs())
    assert final.verdict == "ACCEPT"
    assert final.markup_multiplier == pytest.approx(2.75, abs=0.01)


def test_final_accept_below_the_relaxed_floor_is_rejected():
    # 1.75x and AUD 3.00 margin — under both arms of the relaxed floor.
    with pytest.raises(ValidationError, match="markup_multiplier"):
        _final(_provisional(suggested_retail_aud=7.00), raw=_low_cogs())


def test_final_model_has_no_image_field():
    assert "image_urls" not in ProductCandidateEvaluation.model_fields


def test_final_cogs_basis_url_is_rejected():
    # Defense in depth: the zero-URL validator runs on every construction
    # path, not just from_raw.
    final = _final()
    fields = final.model_dump()
    fields["cogs_estimation_basis"] = "cheaper at https://x.com/item/1005006.html"
    with pytest.raises(ValidationError, match="cogs_estimation_basis"):
        ProductCandidateEvaluation(**fields)