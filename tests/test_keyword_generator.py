"""Offline unit tests for `src.keywords.generator` (plan Step 4).

The LLM endpoint is a scripted `httpx.MockTransport` and the Step-3 gold
deliverable is a tmp_path fixture — no network, no real model, no real
`outputs/` read. Covers the template prompt (the product table is a slot the
generator fills from the gold deliverable), the table rendering (cells
verbatim, nulls as em dashes), the gold-file read with its remediation
errors, the strongest-products selection, the batched prompt assembly, the
merged-pool validation rules (broad/modifier pairing, AICIS banned tokens,
the 50-70 band, per-product coverage and floor) and the truncation salvage
for the documented `finish_reason=length` failure mode.
"""

import json
import logging

import httpx
import pytest

from src.config import settings
from src.keywords.generator import (
    BANNED_TOKENS,
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
    build_product_table,
    carries_banned_token,
    dedupe_keywords,
    default_per_product_min,
    load_gold_products,
    load_prompt_parts,
    parse_llm_keywords,
    per_product_target,
    select_gold_products,
    validate_pool,
)

#: The plan's deliverable targets 8-20 gold products (§5.4), which is the zone
#: where the 50-70 band, the adaptive per-product target and the adaptive
#: floor are all mutually satisfiable — a fixture of that size exercises the
#: real contract rather than a degenerate one.
GOLD_PRODUCTS = [
    {
        "name": "Stainless garlic press",
        "pillar": "curated_home",
        "demand_evidence": "rating 4.7/5, 1,204 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert stainless-steel kitchen tool, AICIS-exempt.",
        "retail_price_text": "$24.95",
    },
    {
        "name": "Olive wood serving board",
        "pillar": "curated_home",
        "demand_evidence": "rating 4.6/5, 880 reviews (Google Shopping AU)",
        "description": "Solid olive wood board, 30 cm, hand-finished.",
        "compliance_note": "Inert wooden tabletop object.",
        "retail_price_text": "$49.00",
    },
    {
        "name": "Ceramic bath tray",
        "pillar": "self_care_rituals",
        "demand_evidence": "rating 4.5/5, 430 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert ceramic bath accessory.",
        "retail_price_text": "$19.95",
    },
    {
        "name": "Bamboo bath caddy",
        "pillar": "self_care_rituals",
        "demand_evidence": "rating 4.8/5, 2,310 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert bamboo bath accessory.",
        "retail_price_text": "$39.95",
    },
    {
        "name": "Brass incense holder",
        "pillar": "self_care_rituals",
        "demand_evidence": "rating 4.4/5, 260 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert brass object.",
        "retail_price_text": "$22.50",
    },
    {
        "name": "Wooden spice rack",
        "pillar": "curated_home",
        "demand_evidence": "rating 4.3/5, 150 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert wooden storage object.",
        "retail_price_text": "$34.00",
    },
    {
        "name": "Linen tea towel set",
        "pillar": "curated_home",
        "demand_evidence": "rating 4.9/5, 3,120 reviews (Google Shopping AU)",
        "description": "Set of two stonewashed linen tea towels.",
        "compliance_note": "Inert textile.",
        "retail_price_text": "$29.00",
    },
    {
        "name": "Copper water carafe",
        "pillar": "other",
        "demand_evidence": "rating 4.2/5, 96 reviews (Google Shopping AU)",
        "description": None,
        "compliance_note": "Inert copper tabletop object.",
        "retail_price_text": "$54.00",
    },
]

GOLD_NAMES = [p["name"] for p in GOLD_PRODUCTS]


def _write_gold(tmp_path, products=None, name="gold.json"):
    """Write a Step-3-shaped deliverable and return its path."""
    path = tmp_path / name
    path.write_text(
        json.dumps({"products": GOLD_PRODUCTS if products is None else products}),
        encoding="utf-8",
    )
    return path


def _keyword_rows(products, per_product=8, pillar="curated_home"):
    """Build a structurally valid pool: half broad, half modifier per product."""
    rows = []
    for index, product in enumerate(products):
        head = f"kw {index} head"
        for i in range(per_product // 2):
            rows.append(
                {
                    "keyword": f"{head} {i}",
                    "product": product,
                    "pillar": pillar,
                    "role": "broad",
                    "tightens": None,
                    "rationale": "head term",
                }
            )
        for i in range(per_product // 2):
            rows.append(
                {
                    "keyword": f"kw {index} tight {i}",
                    "product": product,
                    "pillar": pillar,
                    "role": "modifier",
                    "tightens": f"{head} 0",
                    "rationale": "tightens the head",
                }
            )
    return rows


def _batch_json(rows):
    return json.dumps({"keywords": rows})


def _scripted_transport(bodies, log, finish_reason="stop"):
    """One 200 response per POST, recording each request payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(
            {
                "payload": json.loads(request.content),
                "auth": request.headers.get("authorization"),
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": bodies[len(log) - 1]},
                        "finish_reason": finish_reason,
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


def _generator(bodies, log, tmp_path, finish_reason="stop", **kwargs):
    return KeywordGenerator(
        client=httpx.Client(transport=_scripted_transport(bodies, log, finish_reason)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
        gold_products_path=_write_gold(tmp_path),
        **kwargs,
    )


# --- the prompt resource and the rendered table ---------------------------


def test_prompt_parts_is_a_template_with_a_product_slot():
    parts = load_prompt_parts()
    # The prompt file carries NO products any more: the table is a slot the
    # generator fills from the live Step-3 deliverable.
    assert "{PRODUCT_TABLE}" in parts.user_template
    assert "| # |" not in parts.user_template
    assert "50–70 supplier" in parts.user_template
    assert "50–70 keywords in total" in parts.requirements
    assert parts.system.startswith("You are a senior dropship supplier-sourcing")
    for token in ("CJDropshipping", "DSers", "Zendrop"):
        assert token in parts.system


def test_build_product_table_renders_verbatim_cells():
    lines = build_product_table(GOLD_PRODUCTS)
    assert lines[0] == (
        "| # | Product | Pillar | AU demand evidence | "
        "Physical attributes from the listing | Retail price |"
    )
    assert lines[1].startswith("| --- |")
    rows = lines[2:]
    assert len(rows) == len(GOLD_PRODUCTS)
    assert rows[0].startswith(f"| 1 | {GOLD_NAMES[0]} | curated_home |")
    assert "rating 4.7/5, 1,204 reviews (Google Shopping AU)" in rows[0]
    assert "$24.95" in rows[0]
    # `description` is null on every production row, so the compliance note —
    # the remaining listing-derived description of the object — stands in.
    assert "Inert stainless-steel kitchen tool" in rows[0]
    # A populated description wins over the compliance note.
    assert "Solid olive wood board, 30 cm" in rows[1]
    # A null cell renders as an em dash, never a blank or the word None.
    only_nulls = build_product_table([{"name": "Bare item"}])[2]
    assert "—" in only_nulls
    assert "None" not in only_nulls


def test_load_gold_products_missing_file_names_the_fix(tmp_path):
    with pytest.raises(KeywordGenerationError, match="run Step 3 first"):
        load_gold_products(tmp_path / "nope.json")


def test_load_gold_products_malformed_json_names_the_fix(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeywordGenerationError, match="run Step 3 first"):
        load_gold_products(path)


def test_load_gold_products_empty_list_names_the_fix(tmp_path):
    with pytest.raises(KeywordGenerationError, match="no products"):
        load_gold_products(_write_gold(tmp_path, products=[]))


def test_load_gold_products_drops_unnamed_rows(tmp_path):
    products = [{"name": "  "}, {"name": "Real item"}]
    loaded = load_gold_products(_write_gold(tmp_path, products=products))
    assert [p["name"] for p in loaded] == ["Real item"]


def test_select_gold_products_ranks_caps_and_balances_pillars():
    selected = select_gold_products(GOLD_PRODUCTS, limit=4)
    names = [p["name"] for p in selected]
    # Strongest demand first: linen (3,120) then bamboo (2,310)...
    assert names[0] == "Linen tea towel set"
    assert names[1] == "Bamboo bath caddy"
    # 4 slots round-robin across the three pillars, so `other` is not starved.
    assert len(set(names)) == 4
    assert any(p["pillar"] == "other" for p in selected)

    # Under the limit, every product survives untouched.
    assert len(select_gold_products(GOLD_PRODUCTS, limit=50)) == len(GOLD_PRODUCTS)


def test_select_gold_products_excludes_boundary_named_products():
    """A product named for an AICIS token cannot yield a compliant keyword."""
    products = GOLD_PRODUCTS + [
        {
            "name": "Rose Quartz Gua Sha Tool",
            "pillar": "self_care_rituals",
            "demand_evidence": "rating 4.9/5, 9,000 reviews (Google Shopping AU)",
            "compliance_note": "Inert quartz tool.",
            "retail_price_text": "$30.00",
        }
    ]
    # It is the strongest by demand, yet never enters the keyword table.
    selected = select_gold_products(products, limit=50)
    assert "Rose Quartz Gua Sha Tool" not in [p["name"] for p in selected]
    assert len(selected) == len(GOLD_PRODUCTS)
    assert carries_banned_token("Rose Quartz Gua Sha Tool") == "quartz"
    assert carries_banned_token("Stainless garlic press") is None


def test_adaptive_target_and_floor_math():
    # 8-20 gold products is the plan's design zone (§5.4).
    assert per_product_target(8) == 8
    assert per_product_target(12) == 5
    assert per_product_target(20) == 3
    # Below six products the clamp would miss the 50-keyword band, so the
    # target lifts just enough to reach it.
    assert per_product_target(4) == 13
    assert per_product_target(0) == 0
    assert default_per_product_min(8) == 5
    assert default_per_product_min(12) == 3
    assert default_per_product_min(4) == 11
    assert default_per_product_min(1) == 49


# --- the generation flow --------------------------------------------------


def test_generate_returns_validated_pool(tmp_path):
    parts = load_prompt_parts()
    rows = _keyword_rows(GOLD_NAMES)
    log = []
    generator = _generator([_batch_json(rows[:32]), _batch_json(rows[32:])], log, tmp_path)

    pool = generator.generate()

    assert len(pool) == 64
    assert all(isinstance(row, CandidateKeyword) for row in pool)
    broad = {row.keyword for row in pool if row.role == "broad"}
    for row in pool:
        if row.role == "modifier":
            assert row.tightens in broad
            assert row.tightens != row.keyword

    assert len(log) == 2
    first, second = (entry["payload"] for entry in log)
    assert first["model"] == generator.model
    assert first["temperature"] == 0.6
    assert first["max_tokens"] == 16000
    assert first["messages"][0]["content"] == parts.system
    first_user = first["messages"][1]["content"]
    second_user = second["messages"][1]["content"]
    # Each call carries only its own products' table.
    assert GOLD_NAMES[0] in first_user and GOLD_NAMES[0] not in second_user
    assert GOLD_NAMES[-1] in second_user and GOLD_NAMES[-1] not in first_user
    assert "{PRODUCT_TABLE}" not in first_user
    assert "covers only the 4 products" in first_user
    assert "**8 keywords per product**" in first_user
    assert "(32 in total)" in first_user
    assert "all 8 products" in first_user
    assert parts.requirements in first_user
    assert log[0]["auth"] == "Bearer mock-test-llm_api_key"


def test_generate_accepts_the_other_pillar(tmp_path):
    rows = _keyword_rows(GOLD_NAMES, pillar="other")
    log = []
    generator = _generator([_batch_json(rows[:32]), _batch_json(rows[32:])], log, tmp_path)

    pool = generator.generate()

    assert len(pool) == 64
    assert {row.pillar for row in pool} == {"other"}


def test_generate_salvages_truncated_batch(tmp_path, caplog):
    rows = _keyword_rows(GOLD_NAMES)
    first, second = _batch_json(rows[:32]), _batch_json(rows[32:])
    log = []
    generator = _generator(
        [first[: len(first) - 40], second[: len(second) - 40]], log, tmp_path,
        finish_reason="length",
    )

    with caplog.at_level(logging.WARNING, logger="src.keywords.generator"):
        pool = generator.generate()

    # Each batch's last (incomplete) row is gone; complete rows survive.
    assert len(pool) == len(rows) - 2
    assert "output limit" in caplog.text
    assert "salvaged" in caplog.text


def test_generate_handles_fenced_json(tmp_path):
    rows = _keyword_rows(GOLD_NAMES)
    log = []
    fenced = "```json\n" + _batch_json(rows) + "\n```"
    generator = _generator([fenced], log, tmp_path)
    generator.batch_size = 100  # one chunk -> one call

    pool = generator.generate()

    assert len(pool) == 64
    assert len(log) == 1


def test_generate_rejects_a_thin_batch(tmp_path):
    # A batch the model answered under the floor leaves the pool under the
    # per-product minimum; the run must fail loudly, not export a thin bank.
    rows = _keyword_rows(GOLD_NAMES, per_product=4)
    log = []
    generator = _generator([_batch_json(rows[:16]), _batch_json(rows[16:])], log, tmp_path)

    with pytest.raises(KeywordGenerationError, match="failed validation"):
        generator.generate()


def test_generate_raises_on_empty_content(tmp_path):
    """The reasoning model's documented empties are named, not mis-parsed."""
    log = []
    generator = _generator([""], log, tmp_path, finish_reason="length")
    generator.batch_size = 100

    with pytest.raises(KeywordGenerationError, match="empty content"):
        generator.generate()


def test_generate_dedupes_repeated_keywords(tmp_path, caplog):
    """Two listings of one product type must not put a keyword in twice.

    The live case (2026-09-28): the gold list carries two garlic presses and
    the model wrote `stainless garlic press` for both. The repeat — including
    any modifier written twice against the shared head term — collapses to
    one row, and the modifiers still resolve to it.
    """
    rows = _keyword_rows(GOLD_NAMES)
    log = []
    generator = _generator(
        [_batch_json(rows[:32]), _batch_json(rows[32:] + [dict(rows[0]), dict(rows[4])])],
        log,
        tmp_path,
    )

    with caplog.at_level(logging.WARNING, logger="src.keywords.generator"):
        pool = generator.generate()

    assert len(pool) == len(rows)
    assert len({row.keyword for row in pool}) == len(pool)
    assert "dropped 2 repeated keyword(s)" in caplog.text


def test_dedupe_keywords_keeps_the_first_occurrence():
    rows = [
        {"keyword": "a", "product": "one"},
        {"keyword": "b", "product": "two"},
        {"keyword": "a", "product": "two"},
    ]
    kept, dropped = dedupe_keywords(rows)
    assert [r["product"] for r in kept] == ["one", "two"]
    assert dropped == ["a"]


def test_parse_llm_keywords_reports_salvage():
    rows = _keyword_rows(["Dry body brush"])
    complete, salvaged = parse_llm_keywords(_batch_json(rows))
    assert complete == rows
    assert salvaged is False

    cut = _batch_json(rows[:5])[:-30]
    partial, salvaged = parse_llm_keywords(cut)
    assert len(partial) == 4
    assert salvaged is True


def test_parse_llm_keywords_rejects_unsalvageable():
    with pytest.raises(KeywordGenerationError, match="could not be parsed"):
        parse_llm_keywords("the model replied in prose, not JSON")


# --- pool validation ------------------------------------------------------


def _valid_pool():
    return _keyword_rows(GOLD_NAMES)


def test_validate_pool_accepts_the_reference_pool():
    assert validate_pool(_valid_pool(), GOLD_NAMES) == []


def test_validate_pool_banned_token():
    rows = _valid_pool()
    rows[5]["keyword"] = "dead sea mud brush"
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("banned token 'dead sea'" in p for p in problems)
    # The prompt's boundary list and the code's check are the same tuple.
    assert "dead sea" in BANNED_TOKENS


def test_validate_pool_dangling_tightens():
    rows = _valid_pool()
    rows[5]["tightens"] = "kw 99 head"
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("not in the pool" in p for p in problems)


def test_validate_pool_tightens_itself():
    rows = _valid_pool()
    # A modifier that carries a real broad term as both its keyword and its
    # tightens target (this also duplicates that broad keyword).
    rows[5]["keyword"] = "kw 0 head 1"
    rows[5]["tightens"] = "kw 0 head 1"
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("tightens itself" in p for p in problems)


def test_validate_pool_broad_carries_tightens():
    rows = _valid_pool()
    rows[0]["tightens"] = "kw 0 head 1"
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("is broad but carries tightens" in p for p in problems)


def test_validate_pool_unknown_pillar_and_role():
    rows = _valid_pool()
    rows[5]["pillar"] = "beauty"
    rows[5]["role"] = "longtail"
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("unknown pillar 'beauty'" in p for p in problems)
    assert any("unknown role 'longtail'" in p for p in problems)


def test_validate_pool_missing_field():
    rows = _valid_pool()
    del rows[5]["rationale"]
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("missing fields ['rationale']" in p for p in problems)


def test_validate_pool_duplicates():
    problems = validate_pool(_valid_pool() + [_valid_pool()[0]], GOLD_NAMES)
    assert any("duplicate keywords" in p for p in problems)


def test_validate_pool_size_band():
    rows = _valid_pool()[:40]
    problems = validate_pool(rows, GOLD_NAMES)
    assert any("outside the 50-70 band" in p for p in problems)


def test_validate_pool_product_coverage():
    rows = _valid_pool()
    rows[0]["product"] = "Mystery item"
    problems = validate_pool(rows, GOLD_NAMES)
    assert any("outside the gold product table" in p for p in problems)

    dropped = [r for r in _valid_pool() if r["product"] != GOLD_NAMES[0]]
    problems = validate_pool(dropped, GOLD_NAMES)
    assert any(f"'{GOLD_NAMES[0]}'" in p for p in problems)


def test_validate_pool_per_product_minimum():
    rows = _keyword_rows(GOLD_NAMES, per_product=4)
    problems = validate_pool(rows, GOLD_NAMES, min_keywords=10, max_keywords=100)
    assert any("5-keyword minimum" in p for p in problems)

    # An explicit floor overrides the adaptive default.
    problems = validate_pool(
        rows, GOLD_NAMES, min_keywords=10, max_keywords=100, per_product_min=1
    )
    assert not any("minimum" in p for p in problems)


# --- endpoint failures ----------------------------------------------------


def test_missing_llm_config_raises(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(KeywordGenerationConfigError) as excinfo:
        KeywordGenerator()
    assert "LLM_BASE_URL" in str(excinfo.value)
    assert "LLM_API_KEY" in str(excinfo.value)


def test_http_error_raises(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    generator = KeywordGenerator(
        client=httpx.Client(transport=transport),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
        gold_products_path=_write_gold(tmp_path),
    )
    with pytest.raises(KeywordGenerationError, match="LLM HTTP 500"):
        generator.generate()


def test_transport_error_raises(tmp_path):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    generator = KeywordGenerator(
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
        gold_products_path=_write_gold(tmp_path),
    )
    with pytest.raises(KeywordGenerationError, match="LLM transport error"):
        generator.generate()
