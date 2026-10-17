"""Offline unit tests for the Step-4 keyword bank (exact title first).

Hermetic: the LLM is an `httpx.MockTransport` returning scripted completions. No
network, no tokens.

The strategy has two halves that fail differently, so they are tested apart: the
**exact** keywords are code-derived and must match their product's own title
exactly (a mismatch is a code bug and fails the run), while the **fallback**
keywords are model-authored and are re-validated in code (a bad one is dropped,
because the product still has its exact keyword).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.keywords.generator import (
    DEFAULT_MAX_PRODUCTS,
    MAX_EXACT_WORDS,
    MAX_FALLBACK_WORDS,
    MAX_PER_SOURCE_KEYWORD,
    PILLARS,
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
    _content_words,
    build_exact_rows,
    build_product_table,
    carries_banned_token,
    dedupe_keywords,
    build_ladder_rows,
    exact_keyword,
    fallback_budgets,
    sub_terms,
    load_gold_products,
    load_prompt_parts,
    normalized_title_words,
    parse_llm_keywords,
    select_gold_products,
    validate_bank,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _gold(
    name: str,
    pillar: str = "morning_table",
    source: str = "",
    evidence: str = "rating 4.7/5, 1,204 reviews (Google Shopping AU)",
) -> Dict[str, Any]:
    return {
        "name": name,
        "pillar": pillar,
        "demand_evidence": evidence,
        "description": None,
        "compliance_note": "Inert kitchen tool.",
        "retail_price_text": "$24.95",
        "source_keyword": source,
    }


GOLD = [
    _gold("Bodum Chambord French Press", "brew_fuel", "french press"),
    _gold("Stainless Steel Garlic Press", "morning_table", "garlic press"),
    _gold("Silk Sleep Eye Mask", "restful_reset", "silk eye mask"),
]


def _write_gold(tmp_path: Path, products: Optional[List[Dict]] = None, name="gold.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"products": GOLD if products is None else products}),
        encoding="utf-8",
    )
    return path


def _llm_response(rows: List[Dict], finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": json.dumps({"keywords": rows})},
                    "finish_reason": finish_reason,
                }
            ]
        },
    )


class ScriptedLLM:
    """Replays scripted completions and records the calls it received."""

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        entry = self._responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, httpx.Response):
            return entry
        return _llm_response(entry)


def _generator(
    tmp_path: Path,
    responses: List[Any],
    products: Optional[List[Dict]] = None,
    **kwargs: Any,
) -> KeywordGenerator:
    """A generator whose LLM is scripted and whose gold file is a tmp fixture."""
    scripted = ScriptedLLM(responses)
    generator = KeywordGenerator(
        client=httpx.Client(transport=httpx.MockTransport(scripted)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
        model="test-model",
        gold_products_path=_write_gold(tmp_path, products),
        **kwargs,
    )
    generator.scripted = scripted  # type: ignore[attr-defined]
    return generator


# ---------------------------------------------------------------------------
# exact_keyword — the deterministic transform
# ---------------------------------------------------------------------------


def test_exact_keyword_is_the_titles_own_words_in_order():
    assert exact_keyword("Bodum Chambord French Press") == "bodum chambord french press"


def test_exact_keyword_strips_punctuation_and_collapses_runs():
    assert exact_keyword("Garlic Press, 1L — Off White!") == "garlic press 1l off white"


def test_exact_keyword_removes_apostrophes_rather_than_splitting_the_word():
    # "chef's" must not become "chef s": the possessive would leave a stray
    # letter that no catalogue indexes.
    assert exact_keyword("Chef's Knife") == "chefs knife"
    assert exact_keyword("Chef’s Knife") == "chefs knife"  # the curly form too


def test_exact_keyword_is_idempotent_and_lowercase():
    once = exact_keyword("Stainless Steel  Garlic Press")
    assert once == "stainless steel garlic press"
    assert exact_keyword(once) == once


def test_exact_keyword_of_nothing_is_empty():
    assert exact_keyword(None) == ""
    assert exact_keyword("   ") == ""
    assert exact_keyword("---") == ""


def test_exact_keyword_adds_and_reorders_nothing():
    keyword = exact_keyword("Silicone Pastry Mat 40x50cm, Non-Stick")
    assert keyword.split() == [
        "silicone", "pastry", "mat", "40x50cm", "non", "stick",
    ]


# ---------------------------------------------------------------------------
# build_exact_rows
# ---------------------------------------------------------------------------


def test_build_exact_rows_makes_one_exact_row_per_product():
    rows = build_exact_rows(GOLD)
    assert [row["match"] for row in rows] == ["exact"] * 3
    # `product` is the title VERBATIM; `keyword` is the derived search string.
    assert rows[0]["product"] == "Bodum Chambord French Press"
    assert rows[0]["keyword"] == "bodum chambord french press"
    assert rows[0]["pillar"] == "brew_fuel"


def test_a_stuffed_title_is_truncated_rather_than_cost_its_product_a_keyword(caplog):
    # A stuffed title is still a usable search prefix; dropping the product
    # entirely would lose it from the bank for no good reason.
    long_title = " ".join(f"word{i}" for i in range(MAX_EXACT_WORDS + 4))
    with caplog.at_level("INFO"):  # the notice is INFO: a run logs at INFO
        rows = build_exact_rows([_gold(long_title), _gold("Garlic Press")])
    assert [row["product"] for row in rows] == [long_title, "Garlic Press"]
    assert len(rows[0]["keyword"].split()) == MAX_EXACT_WORDS
    assert any("truncated a stuffed title" in r.message for r in caplog.records)


def test_a_truncated_title_does_not_end_on_a_function_word():
    # "…mincer with" reads as a cut-off sentence and matches worse than a query
    # ending on an object word.
    title = " ".join(["alpha"] * (MAX_EXACT_WORDS - 1) + ["with", "beta", "gamma"])
    keyword = exact_keyword(title)
    assert not keyword.endswith(" with")
    assert keyword.split()[-1] == "alpha"


def test_a_title_within_the_bound_is_left_completely_alone():
    title = " ".join(f"word{i}" for i in range(MAX_EXACT_WORDS))
    assert len(normalized_title_words(title)) == MAX_EXACT_WORDS
    assert exact_keyword(title) == title


def test_normalized_title_words_is_the_titles_own_words():
    assert normalized_title_words("Bodum Chambord, 1L — Off White") == [
        "bodum", "chambord", "1l", "off", "white",
    ]


def test_build_exact_rows_skips_a_product_with_no_usable_title(caplog):
    rows = build_exact_rows([_gold("   "), _gold("Garlic Press")])
    assert [row["product"] for row in rows] == ["Garlic Press"]


# ---------------------------------------------------------------------------
# the prompt resource
# ---------------------------------------------------------------------------


def test_prompt_is_a_template_with_a_product_slot_and_no_products():
    parts = load_prompt_parts()
    assert "{PRODUCT_TABLE}" in parts.user_template
    assert "| # |" not in parts.user_template
    # The job is the fallback; the title is explicitly not the model's to write.
    assert "fallback" in parts.user_template.lower()
    assert "Never write a" in parts.system
    assert "CJdropshipping" in parts.system
    assert "AliExpress" not in parts.system


def test_prompt_states_the_boundary_and_the_per_product_fallback_budget():
    parts = load_prompt_parts()
    assert "Hard AICIS boundary" in parts.requirements
    # The cap is per product and lives in the table's `Fallbacks` column, so the
    # prompt points at the column rather than naming a single number.
    assert "fallbacks` budget" in parts.requirements.lower()
    # The fallback must share a concrete word with the title, and the prompt says
    # so because the code enforces it.
    assert "share at least one concrete word" in parts.requirements


def test_prompt_asks_for_no_pillar_or_match_field():
    # Those are code-attached: a model that writes them is overstepping, and the
    # prompt says so rather than leaving it to chance.
    parts = load_prompt_parts()
    assert "`pillar` or a `match` field" in parts.requirements


def test_build_product_table_renders_verbatim_cells_with_em_dashes_for_nulls():
    lines = build_product_table(GOLD)
    assert lines[0] == (
        "| # | Product | Pillar | AU demand evidence | "
        "Physical attributes from the listing | Retail price | Fallbacks |"
    )
    assert lines[2].startswith("| 1 | Bodum Chambord French Press | brew_fuel |")
    # The last two cells are the verbatim retail price and the ranked budget.
    # Bodum is the strongest of the three, so it sits in the top third.
    assert lines[2].endswith("| Inert kitchen tool. | $24.95 | 5 |")


# ---------------------------------------------------------------------------
# load_gold_products (fail closed)
# ---------------------------------------------------------------------------


def test_load_gold_products_reads_the_deliverable(tmp_path):
    loaded = load_gold_products(_write_gold(tmp_path))
    assert [p["name"] for p in loaded] == [p["name"] for p in GOLD]


def test_load_gold_products_missing_file_names_step_3(tmp_path):
    with pytest.raises(KeywordGenerationError, match="run Step 3 first"):
        load_gold_products(tmp_path / "absent.json")


def test_load_gold_products_rejects_malformed_json(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeywordGenerationError, match="not valid JSON"):
        load_gold_products(path)


def test_load_gold_products_rejects_a_payload_without_a_products_list(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")
    with pytest.raises(KeywordGenerationError, match="no 'products' list"):
        load_gold_products(path)


def test_load_gold_products_rejects_unnamed_rows(tmp_path):
    path = _write_gold(tmp_path, products=[{"name": "  "}])
    with pytest.raises(KeywordGenerationError, match="no named products"):
        load_gold_products(path)


# ---------------------------------------------------------------------------
# select_gold_products (kept machinery)
# ---------------------------------------------------------------------------


def test_select_gold_products_ranks_by_demand_and_caps():
    products = [
        _gold("Weak", evidence="rating 4.1/5, 10 reviews"),
        _gold("Strong", evidence="rating 4.9/5, 3,120 reviews"),
        _gold("Middle", evidence="rating 4.5/5, 500 reviews"),
    ]
    selected = select_gold_products(products, limit=2)
    assert [p["name"] for p in selected] == ["Strong", "Middle"]


def test_select_gold_products_excludes_boundary_named_products():
    products = GOLD + [_gold("Rose Quartz Gua Sha Tool")]
    names = [p["name"] for p in select_gold_products(products, limit=50)]
    assert "Rose Quartz Gua Sha Tool" not in names
    assert len(names) == len(GOLD)


def test_select_gold_products_caps_products_per_source_keyword():
    """Different products sharing a Step-2 keyword now coexist — up to the cap.

    The exact-title primary keyword removed the reason the old rule kept only the
    strongest row: distinct titles no longer collide in the bank, so a topic may
    supply several products without one swallowing the table.
    """
    products = [
        _gold("Garlic Press A", source="garlic press",
              evidence="rating 4.9/5, 900 reviews"),
        _gold("Garlic Press B", source="garlic press",
              evidence="rating 4.2/5, 90 reviews"),
        _gold("Eye Mask", source="silk eye mask"),
    ]
    names = [p["name"] for p in select_gold_products(products, limit=50)]
    assert sorted(names) == ["Eye Mask", "Garlic Press A", "Garlic Press B"]


def test_select_gold_products_cap_keeps_the_strongest_rows_of_a_topic():
    products = [
        _gold(f"Harness {label}", source="no pull dog harness", evidence=evidence)
        for label, evidence in [
            ("A", "rating 4.9/5, 4,000 reviews"),
            ("B", "rating 4.8/5, 3,000 reviews"),
            ("C", "rating 4.7/5, 2,000 reviews"),
            ("D", "rating 4.6/5, 1,000 reviews"),
        ]
    ]
    names = [p["name"] for p in select_gold_products(products, limit=50)]
    # MAX_PER_SOURCE_KEYWORD strong rows survive; the weakest of the topic is out.
    assert names == ["Harness A", "Harness B", "Harness C"]
    assert len(names) == MAX_PER_SOURCE_KEYWORD


def test_select_gold_products_collapses_rows_sharing_a_normalised_title():
    """Two rows whose titles normalise alike emit the same exact keyword.

    The dedupe key is now the title the primary keyword is derived from, so
    rows differing only in case or punctuation cannot both spend a slot.
    """
    products = [
        _gold("Silk Sleep Eye Mask", source="silk eye mask",
              evidence="rating 4.9/5, 900 reviews"),
        _gold("silk sleep eye mask!!", source="silk eye mask",
              evidence="rating 4.2/5, 90 reviews"),
    ]
    names = [p["name"] for p in select_gold_products(products, limit=50)]
    assert names == ["Silk Sleep Eye Mask"]


def test_select_gold_products_under_the_cap_returns_everything():
    assert len(select_gold_products(GOLD, limit=DEFAULT_MAX_PRODUCTS)) == len(GOLD)


# ---------------------------------------------------------------------------
# validate_bank — code-invariant problems fail, model problems are reported
# ---------------------------------------------------------------------------


def _valid_bank() -> List[Dict]:
    rows = build_exact_rows(GOLD)
    rows.append(
        {
            "keyword": "french press with plunger",
            "product": "Bodum Chambord French Press",
            "pillar": "brew_fuel",
            "match": "tight_fallback",
            "rationale": "names the plunger",
        }
    )
    return rows


def _messages(problems) -> List[str]:
    return [problem.message for problem in problems]


def test_a_well_formed_bank_validates_clean():
    assert validate_bank(_valid_bank(), GOLD) == []


def test_an_exact_keyword_not_matching_its_title_is_a_problem():
    rows = _valid_bank()
    rows[0]["keyword"] = "something else entirely"
    problems = _messages(validate_bank(rows, GOLD))
    assert any("does not match its product's own title" in p for p in problems)


def test_a_product_with_no_exact_row_is_a_problem():
    rows = [r for r in _valid_bank() if r["product"] != "Silk Sleep Eye Mask"]
    problems = _messages(validate_bank(rows, GOLD))
    assert any("no exact keyword" in p for p in problems)


def test_a_row_naming_an_unknown_product_is_a_problem():
    rows = _valid_bank()
    rows.append(
        {
            "keyword": "ghost product tool",
            "product": "Ghost Product",
            "pillar": "morning_table",
            "match": "tight_fallback",
            "rationale": "invented",
        }
    )
    problems = _messages(validate_bank(rows, GOLD))
    assert any("not in the table" in p for p in problems)


def test_a_duplicate_keyword_is_a_problem():
    rows = _valid_bank()
    rows.append(dict(rows[0]))
    problems = _messages(validate_bank(rows, GOLD))
    assert any("appears 2 times" in p for p in problems)


def test_a_pillar_that_is_not_its_products_pillar_is_a_problem():
    rows = _valid_bank()
    rows[1]["pillar"] = "brew_fuel"  # the garlic press is morning_table
    problems = _messages(validate_bank(rows, GOLD))
    assert any("which is not its product's" in p for p in problems)


def test_a_fallback_outside_the_word_bounds_is_a_problem():
    rows = _valid_bank()
    rows[-1]["keyword"] = "press"
    assert any("word(s)" in p for p in _messages(validate_bank(rows, GOLD)))

    rows[-1]["keyword"] = " ".join(f"w{i}" for i in range(MAX_FALLBACK_WORDS + 1))
    assert any("word(s)" in p for p in _messages(validate_bank(rows, GOLD)))


def test_a_fallback_with_punctuation_or_capitals_is_a_problem():
    rows = _valid_bank()
    rows[-1]["keyword"] = "french press, large"
    assert any("punctuation or capitals" in p for p in _messages(validate_bank(rows, GOLD)))

    rows[-1]["keyword"] = "French Press Large"
    assert any("punctuation or capitals" in p for p in _messages(validate_bank(rows, GOLD)))


def test_a_fallback_carrying_a_banned_token_is_a_problem():
    rows = _valid_bank()
    rows[-1]["keyword"] = "french press with salt block"
    assert any("banned token" in p for p in _messages(validate_bank(rows, GOLD)))


def test_an_exact_keyword_carrying_a_banned_token_is_a_problem():
    # The table pre-screens banned-token NAMES, so an exact keyword carrying one
    # means that screen was bypassed — a code bug, not a model slip.
    products = GOLD + [_gold("Bath Salt Scoop")]
    rows = build_exact_rows(products)
    problems = _messages(validate_bank(rows, products))
    assert any("should not have reached the table" in p for p in problems)


def test_a_loosely_coupled_fallback_is_a_problem():
    # A fallback sharing no concrete word with its own title is a broader
    # category or a different product, not a variant of this one.
    rows = _valid_bank()
    rows[-1]["keyword"] = "kitchen tools"
    assert any("too loosely coupled" in p for p in _messages(validate_bank(rows, GOLD)))


def test_a_fallback_may_be_a_valid_variant_of_its_title():
    assert _content_words("french press with plunger") & _content_words(
        "Bodum Chambord French Press"
    )


# ---------------------------------------------------------------------------
# generate_bank — the exact rows are code's, the fallbacks are the model's
# ---------------------------------------------------------------------------


def test_generate_bank_emits_an_exact_row_per_product_plus_the_models_fallbacks(tmp_path):
    generator = _generator(
        tmp_path,
        [
            [
                {
                    "keyword": "french press with plunger",
                    "product": "Bodum Chambord French Press",
                    "rationale": "names the plunger",
                }
            ]
        ],
    )
    bank = generator.generate_bank()

    exact = [c for c in bank if c.match == "exact"]
    fallbacks = [c for c in bank if c.match == "tight_fallback"]
    assert len(exact) == len(GOLD)
    assert [c.keyword for c in fallbacks] == ["french press with plunger"]
    assert all(isinstance(c, CandidateKeyword) for c in bank)
    assert {c.pillar for c in exact} == {p["pillar"] for p in GOLD}
    assert fallbacks[0].pillar == "brew_fuel"


def test_generate_bank_accepts_an_empty_fallback_list(tmp_path):
    # "An empty list is a valid and common answer" — most products need none.
    # An empty fallback list still leaves the code-derived ladder in place.
    bank = _generator(tmp_path, [[]]).generate_bank()
    assert [c.match for c in bank].count("tight_fallback") == 0
    assert [c.match for c in bank].count("exact") == len(GOLD)
    assert {c.match for c in bank} <= {"exact", "sub_term", "head_term"}


def test_the_batch_note_asks_for_the_budget_and_lists_what_is_taken(tmp_path):
    generator = _generator(tmp_path, [[], []], batch_size=2)
    generator.generate_bank()

    calls = generator.scripted.calls
    assert len(calls) == 2  # 3 products at 2 per call
    first = calls[0]["messages"][1]["content"]
    assert "`fallbacks` column" in first.lower()
    # The exact keywords are already claimed, so the model is told not to reuse
    # them as a fallback.
    assert "bodum chambord french press" in first


def test_each_batch_table_carries_only_its_own_products(tmp_path):
    # The table is demand-ranked, so which product lands in which batch follows
    # the ranking — the property to pin is that the batches partition the table.
    generator = _generator(tmp_path, [[], []], batch_size=2)
    generator.generate_bank()
    calls = generator.scripted.calls
    names = [p["name"] for p in generator.products]
    assert len(calls) == 2
    first, second = (call["messages"][1]["content"] for call in calls)
    assert names[0] in first and names[1] in first and names[2] not in first
    assert names[2] in second and names[1] not in second


def test_a_model_row_claiming_to_be_an_exact_keyword_is_ignored(tmp_path, caplog):
    # The model's job is the fallback and nothing else; claiming a match of
    # "exact" is not something it gets to decide.
    generator = _generator(
        tmp_path,
        [
            [
                {
                    "keyword": "sneaky exact title",
                    "product": "Bodum Chambord French Press",
                    "match": "exact",
                }
            ]
        ],
    )
    bank = generator.generate_bank()
    assert "sneaky exact title" not in [c.keyword for c in bank]
    assert any("is not a fallback" in r.message for r in caplog.records)


def test_a_model_row_naming_an_unknown_product_is_ignored(tmp_path, caplog):
    generator = _generator(
        tmp_path,
        [[{"keyword": "ghost tool", "product": "Ghost Product"}]],
    )
    bank = generator.generate_bank()
    assert "ghost tool" not in [c.keyword for c in bank]
    assert any("is not a fallback" in r.message for r in caplog.records)


def test_a_fallback_that_fails_validation_is_dropped_not_fatal(tmp_path, caplog):
    """A bad fallback costs its own row, not the run: the exact keyword stands."""
    generator = _generator(
        tmp_path,
        [[{"keyword": "kitchen tools", "product": "Bodum Chambord French Press"}]],
    )
    bank = generator.generate_bank()
    # The bad fallback is gone; the exact rows and the code-derived ladder stand.
    assert "kitchen tools" not in [c.keyword for c in bank]
    assert "tight_fallback" not in {c.match for c in bank}


def test_a_fallback_duplicating_another_products_exact_keyword_is_deduped(tmp_path):
    # The model must not be able to steal another product's exact string: the
    # neediest owner keeps it, and the duplicate is dropped.
    generator = _generator(
        tmp_path,
        [[{"keyword": "stainless steel garlic press",
           "product": "Bodum Chambord French Press"}]],
    )
    bank = generator.generate_bank()
    garlic = [c for c in bank if c.keyword == "stainless steel garlic press"]
    assert len(garlic) == 1
    assert garlic[0].product == "Stainless Steel Garlic Press"
    assert garlic[0].match == "exact"


def test_a_truncated_batch_is_salvaged_rather_than_paid_for_again(tmp_path, caplog):
    # One complete row, then a second cut off mid-object — the documented
    # `finish_reason="length"` failure mode.
    truncated = (
        '{"keywords": [{"keyword": "french press with plunger", '
        '"product": "Bodum Chambord French Press", "rationale": "plunger"}, '
        '{"keyword": "garlic press with cleaner", "prod'
    )
    response = httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": truncated}, "finish_reason": "length"}
            ]
        },
    )
    generator = _generator(tmp_path, [response])
    bank = generator.generate_bank()
    # The complete row survived the salvage and no second call was paid for.
    assert len(generator.scripted.calls) == 1
    assert "french press with plunger" in [c.keyword for c in bank]


def test_an_empty_completion_names_the_finish_reason(tmp_path):
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
    )
    generator = _generator(tmp_path, [response])
    with pytest.raises(KeywordGenerationError, match="finish_reason='length'"):
        generator.generate_bank()


def test_an_http_error_fails_the_run(tmp_path):
    generator = _generator(tmp_path, [httpx.Response(500, text="boom")])
    with pytest.raises(KeywordGenerationError, match="LLM HTTP 500"):
        generator.generate_bank()


def test_a_transport_error_fails_the_run(tmp_path):
    generator = _generator(tmp_path, [httpx.ConnectError("refused")])
    with pytest.raises(KeywordGenerationError, match="transport error"):
        generator.generate_bank()


def test_a_fenced_json_response_is_accepted(tmp_path):
    body = "```json\n" + json.dumps(
        {
            "keywords": [
                {
                    "keyword": "french press with plunger",
                    "product": "Bodum Chambord French Press",
                    "rationale": "plunger",
                }
            ]
        }
    ) + "\n```"
    response = httpx.Response(
        200, json={"choices": [{"message": {"content": body}, "finish_reason": "stop"}]}
    )
    bank = _generator(tmp_path, [response]).generate_bank()
    assert "french press with plunger" in [c.keyword for c in bank]


def test_an_unparsable_response_raises(tmp_path):
    response = httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": "no json here"}, "finish_reason": "stop"}
            ]
        },
    )
    generator = _generator(tmp_path, [response])
    with pytest.raises(KeywordGenerationError, match="could not be parsed"):
        generator.generate_bank()


def test_the_bank_never_writes_a_partial_result(tmp_path):
    """A validation failure must reach the caller as an exception, not a bank."""
    generator = _generator(tmp_path, [httpx.Response(500, text="boom")])
    with pytest.raises(KeywordGenerationError):
        generator.generate_bank()


def test_generate_bank_raises_when_no_product_is_eligible(tmp_path):
    # Every product's own name carries a banned token, so none can be named.
    products = [_gold("Dead Sea Salt Scrub"), _gold("Jade Gua Sha")]
    with pytest.raises(KeywordGenerationError, match="no eligible gold products"):
        _generator(tmp_path, [], products=products)


def test_the_generator_requires_an_llm_model(tmp_path, monkeypatch):
    from src.config import settings

    # An explicit empty `model=` falls back to the settings default (the repo's
    # blank-means-default rule), so the error needs the setting itself empty.
    monkeypatch.setattr(settings, "LLM_MODEL", "")
    with pytest.raises(KeywordGenerationConfigError, match="LLM_MODEL"):
        KeywordGenerator(
            client=httpx.Client(transport=httpx.MockTransport(lambda r: r)),
            gold_products_path=_write_gold(tmp_path),
        )


def test_a_non_positive_batch_size_is_rejected(tmp_path):
    with pytest.raises(KeywordGenerationError, match="batch_size"):
        _generator(tmp_path, [], batch_size=0)


# ---------------------------------------------------------------------------
# dedupe_keywords (kept machinery)
# ---------------------------------------------------------------------------


def test_dedupe_awards_a_contested_keyword_to_the_neediest_product():
    rows = [
        {"keyword": "shared tool", "product": "A"},
        {"keyword": "shared tool", "product": "B"},
        {"keyword": "b only", "product": "B"},
    ]
    kept, dropped = dedupe_keywords(rows)
    assert dropped == ["shared tool"]
    # B already has its own keyword, so A — which has none — wins the share.
    assert [(r["keyword"], r["product"]) for r in kept] == [
        ("shared tool", "A"),
        ("b only", "B"),
    ]


def test_dedupe_keeps_the_earliest_row_on_a_tie():
    rows = [
        {"keyword": "shared", "product": "A"},
        {"keyword": "shared", "product": "B"},
    ]
    kept, dropped = dedupe_keywords(rows)
    assert dropped == ["shared"]
    assert kept[0]["product"] == "A"


def test_dedupe_leaves_unique_rows_alone():
    rows = [{"keyword": "one", "product": "A"}, {"keyword": "two", "product": "B"}]
    kept, dropped = dedupe_keywords(rows)
    assert len(kept) == 2 and dropped == []


# ---------------------------------------------------------------------------
# parse_llm_keywords (kept machinery)
# ---------------------------------------------------------------------------


def test_parse_llm_keywords_reads_a_plain_object():
    rows, salvaged = parse_llm_keywords('{"keywords": [{"keyword": "x"}]}')
    assert rows == [{"keyword": "x"}] and salvaged is False


def test_parse_llm_keywords_salvages_a_truncated_tail():
    rows, salvaged = parse_llm_keywords(
        '{"keywords": [{"keyword": "a"}, {"keyword": "b", "ration'
    )
    assert [r["keyword"] for r in rows] == ["a"]
    assert salvaged is True


# ---------------------------------------------------------------------------
# carries_banned_token / PILLARS
# ---------------------------------------------------------------------------


def test_carries_banned_token_finds_the_boundary_tokens():
    assert carries_banned_token("jade roller") == "jade"
    assert carries_banned_token("french press") is None
    assert carries_banned_token(None) is None


def test_pillars_are_the_five_morning_anthem_keys():
    assert set(PILLARS) == {
        "brew_fuel", "sunrise_move", "mindful_awakening",
        "restful_reset", "morning_table",
    }


# ---------------------------------------------------------------------------
# Runner: scripts/generate_gold_keywords.py
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "step4_runner",
        Path(__file__).resolve().parents[1] / "scripts" / "generate_gold_keywords.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_the_digest_lists_each_product_with_its_exact_and_fallback_keyword():
    runner = _runner()
    candidates = [
        CandidateKeyword(
            keyword="bodum chambord french press",
            product="Bodum Chambord French Press",
            pillar="brew_fuel",
            match="exact",
            rationale="the gold product's own title",
        ),
        CandidateKeyword(
            keyword="french press with plunger",
            product="Bodum Chambord French Press",
            pillar="brew_fuel",
            match="tight_fallback",
            rationale="names the plunger",
        ),
    ]
    digest = runner.render_markdown(candidates)
    assert "### Bodum Chambord French Press" in digest
    assert "- **exact** — `bodum chambord french press`" in digest
    assert "- fallback — `french press with plunger` (names the plunger)" in digest
    assert "1 exact-title" in digest and "1 fallback" in digest


def test_the_plan_prints_the_bank_size_and_the_call_count_before_spending(tmp_path, capsys):
    runner = _runner()
    generator = _generator(tmp_path, [])
    runner.print_plan(generator)

    out = capsys.readouterr().out
    assert "eligible product(s)" in out
    assert "exact" in out and "fallback" in out
    # The step spends LLM tokens but never HasData credits, and says so.
    assert "no HasData credits" in out


def test_the_runner_writes_the_bank_and_the_digest(tmp_path, monkeypatch, capsys):
    import sys

    runner = _runner()
    gold = _write_gold(tmp_path)
    output = tmp_path / "bank.json"

    # Stub the generator so the runner is driven end to end without an LLM.
    monkeypatch.setattr(
        runner, "KeywordGenerator",
        lambda **kwargs: _StubGenerator(tmp_path, gold),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["generate_gold_keywords.py", "--gold-products", str(gold),
         "--output", str(output)],
    )
    assert runner.main() == 0

    payload = json.loads(output.read_text())
    assert payload["strategy"] == "exact_title_first"
    assert payload["counts"] == {"exact": 1, "tight_fallback": 1}
    assert payload["source_products"] == 1
    digest = (tmp_path / "bank.md").read_text()
    assert "exact" in digest and "fallback" in digest
    assert "[STEP 4 COMPLETE]" in capsys.readouterr().out


def test_the_runner_halts_when_the_gold_file_is_missing(tmp_path, capsys, monkeypatch):
    import sys

    runner = _runner()
    monkeypatch.setattr(
        sys, "argv",
        ["generate_gold_keywords.py", "--gold-products", str(tmp_path / "absent.json"),
         "--output", str(tmp_path / "bank.json")],
    )
    assert runner.main() == 1
    out = capsys.readouterr().out
    assert "Step 4 Gold Products" in out
    assert "run_gold_standard_research.py" in out
    assert not (tmp_path / "bank.json").exists()


def test_the_runner_halts_when_the_bank_fails_validation(tmp_path, capsys, monkeypatch):
    import sys

    runner = _runner()
    gold = _write_gold(tmp_path)
    output = tmp_path / "bank.json"
    monkeypatch.setattr(
        runner, "KeywordGenerator", lambda **kwargs: _FailingGenerator(gold)
    )
    monkeypatch.setattr(
        sys, "argv",
        ["generate_gold_keywords.py", "--gold-products", str(gold),
         "--output", str(output)],
    )
    assert runner.main() == 1
    out = capsys.readouterr().out
    assert "Step 4 Keyword Bank" in out
    assert "code bug, not a model slip" in out
    # Nothing is written: a failed bank is not a partial deliverable.
    assert not output.exists()


class _StubGenerator:
    """A generator stand-in returning a fixed one-exact-one-fallback bank."""

    def __init__(self, tmp_path: Path, gold: Path) -> None:
        self.products = GOLD[:1]
        self.batch_size = 4

    def generate_bank(self) -> List[CandidateKeyword]:
        return [
            CandidateKeyword(
                keyword="bodum chambord french press",
                product="Bodum Chambord French Press",
                pillar="brew_fuel",
                match="exact",
                rationale="the gold product's own title",
            ),
            CandidateKeyword(
                keyword="french press with plunger",
                product="Bodum Chambord French Press",
                pillar="brew_fuel",
                match="tight_fallback",
                rationale="names the plunger",
            ),
        ]


class _FailingGenerator:
    def __init__(self, gold: Path) -> None:
        self.products = GOLD[:1]
        self.batch_size = 4

    def generate_bank(self):
        raise KeywordGenerationError(
            "keyword bank failed validation:\n- row 0 is an exact keyword "
            "('x') that does not match its product's own title ('y')"
        )


# ---------------------------------------------------------------------------
# The per-product fallback budget (ranked thirds, operator directive 2026-10-16)
#
# Six fallbacks for every product meant every gold product spent six supplier
# searches whether or not it was worth it. The budget is ranked instead, so the
# best-evidenced products get the widest enrichment.
# ---------------------------------------------------------------------------


def test_fallback_budgets_split_the_table_into_ranked_thirds():
    products = [
        _gold(f"Product {n}", evidence=f"rating 4.{n}/5, {n}00 reviews")
        for n in range(1, 7)
    ]
    budgets = fallback_budgets(products)

    # Ordered by rating desc, so Product 6 is strongest and Product 1 weakest.
    assert budgets["Product 6"] == 5
    assert budgets["Product 5"] == 5
    assert budgets["Product 4"] == 3
    assert budgets["Product 3"] == 3
    assert budgets["Product 2"] == 1
    assert budgets["Product 1"] == 1


def test_a_single_product_still_earns_the_top_budget():
    """Ranked thirds, not a floor: one product is its own top third."""
    assert fallback_budgets([_gold("Only Product")])["Only Product"] == 5


def test_the_budget_is_ranked_not_absolute():
    """A weak table and a strong one produce the same spread."""
    weak = [
        _gold(f"Weak {n}", evidence=f"rating 3.{n}/5, {n}0 reviews")
        for n in range(1, 4)
    ]
    assert sorted(fallback_budgets(weak).values(), reverse=True) == [5, 3, 1]


def test_validate_bank_drops_a_fallback_past_its_budget():
    products = [
        _gold(f"Product {n}", evidence=f"rating 4.{n}/5, {n}00 reviews")
        for n in range(1, 7)
    ]
    # Six products, so ranked thirds put `Product 1` last on a budget of 1.
    assert fallback_budgets(products)["Product 1"] == 1
    rows = build_exact_rows(products)
    for suffix in ("alpha", "beta"):
        rows.append(
            {
                "keyword": f"product one {suffix}",
                "product": "Product 1",
                "pillar": "morning_table",
                "match": "tight_fallback",
                "rationale": suffix,
            }
        )

    problems = validate_bank(rows, products)

    past = [p for p in problems if "past its budget" in p.message]
    assert len(past) == 1
    assert past[0].droppable is True
    assert "budget of 1 fallback(s)" in past[0].message


# ---------------------------------------------------------------------------
# The descent ladder (operator directive 2026-10-16)
#
# An exact title is high-precision and near-zero recall: the CJ catalogue does
# not stock "Epicurean Kitchen Series Cutting Board" but stocks plenty of
# chopping boards. The ladder falls from the title down to the pillar's own
# head term, in code, so the descent costs no model call.
# ---------------------------------------------------------------------------


def test_sub_terms_are_trailing_windows_narrowest_first():
    assert sub_terms("Epicurean Kitchen Series Cutting Board") == [
        "cutting board",
        "series cutting board",
        "kitchen series cutting board",
    ]


def test_sub_terms_are_capped_and_skip_short_titles():
    assert len(sub_terms("A Very Long Descriptive Product Title Here")) == 3
    # Two words leave no room for a window that is not the whole title.
    assert sub_terms("Cutting Board") == []


def test_ladder_rows_end_with_the_pillar_head_term():
    rows = build_ladder_rows([_gold("Epicurean Kitchen Series Cutting Board",
                                    pillar="morning_table")])
    assert [r["match"] for r in rows] == ["sub_term", "sub_term", "sub_term", "head_term"]
    assert rows[-1]["keyword"] == "breakfast"


def test_the_head_term_shares_no_word_with_the_title():
    """It is the one rung not derived from the title, so coupling never applies."""
    rows = build_ladder_rows([_gold("Bodum Chambord French Press", pillar="brew_fuel")])
    head = rows[-1]
    assert head["match"] == "head_term"
    assert head["keyword"] == "coffee"


def test_a_product_with_no_usable_title_gets_no_ladder():
    assert build_ladder_rows([_gold("   ")]) == []
