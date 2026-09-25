"""Offline unit tests for `src.keywords.generator` (plan Step 3).

The LLM endpoint is a scripted `httpx.MockTransport` — no network, no real
model. Covers the batched prompt assembly (each call carries only its own
products), the merged-pool validation rules (broad/modifier pairing, AICIS
banned tokens, the 50-70 band, product coverage), and the truncation salvage
for the documented `finish_reason=length` failure mode.
"""

import json
import logging

import httpx
import pytest

from src.config import settings
from src.keywords.generator import (
    CandidateKeyword,
    KeywordGenerationConfigError,
    KeywordGenerationError,
    KeywordGenerator,
    load_prompt_parts,
    parse_llm_keywords,
    validate_pool,
)


def _keyword_rows(products, per_product=8):
    """Build a structurally valid pool: half broad, half modifier per product."""
    rows = []
    for index, product in enumerate(products):
        head = f"kw {index} head"
        for i in range(per_product // 2):
            rows.append(
                {
                    "keyword": f"{head} {i}",
                    "product": product,
                    "pillar": "curated_home",
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
                    "pillar": "curated_home",
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


def _generator(bodies, log, finish_reason="stop"):
    return KeywordGenerator(
        client=httpx.Client(transport=_scripted_transport(bodies, log, finish_reason)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )


def test_prompt_parts_parses_the_product_table():
    parts = load_prompt_parts()
    assert len(parts.product_rows) == 8
    assert parts.products[0] == "Dry body brush"
    assert "50–70" in parts.requirements
    assert parts.system.startswith("You are a senior e-commerce sourcing analyst")
    assert parts.table_header.startswith("| # |")


def test_generate_returns_validated_pool():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    log = []
    generator = _generator([_batch_json(rows[:32]), _batch_json(rows[32:])], log)

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
    assert first["max_tokens"] == 8000
    assert first["messages"][0]["content"] == parts.system
    first_user = first["messages"][1]["content"]
    second_user = second["messages"][1]["content"]
    assert parts.products[0] in first_user and parts.products[0] not in second_user
    assert parts.products[-1] in second_user and parts.products[-1] not in first_user
    assert "covers only the 4 products" in first_user
    assert "(32-36 in total)" in first_user
    assert log[0]["auth"] == "Bearer mock-test-llm_api_key"


def test_generate_salvages_truncated_batch(caplog):
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    truncated1 = _batch_json(rows[:32])[: len(_batch_json(rows[:32])) - 40]
    truncated2 = _batch_json(rows[32:])[: len(_batch_json(rows[32:])) - 40]
    log = []
    generator = _generator([truncated1, truncated2], log, finish_reason="length")

    with caplog.at_level(logging.WARNING, logger="src.keywords.generator"):
        pool = generator.generate()

    # Each batch's last (incomplete) row is gone; complete rows survive.
    assert len(pool) == len(rows) - 2
    assert "output limit" in caplog.text
    assert "salvaged" in caplog.text


def test_generate_handles_fenced_json():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    log = []
    fenced = "```json\n" + _batch_json(rows) + "\n```"
    generator = _generator([fenced], log)
    generator.batch_size = 100  # one chunk -> one call

    pool = generator.generate()

    assert len(pool) == 64
    assert len(log) == 1


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


def test_validate_pool_banned_token():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    rows[5]["keyword"] = "dead sea mud brush"
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("banned token 'dead sea'" in p for p in problems)


def test_validate_pool_dangling_tightens():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    rows[5]["tightens"] = "kw 99 head"
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("not in the pool" in p for p in problems)


def test_validate_pool_tightens_itself():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    # A modifier that carries a real broad term as both its keyword and its
    # tightens target (this also duplicates that broad keyword).
    rows[5]["keyword"] = "kw 0 head 1"
    rows[5]["tightens"] = "kw 0 head 1"
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("tightens itself" in p for p in problems)


def test_validate_pool_broad_carries_tightens():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    rows[0]["tightens"] = "kw 0 head 1"
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("is broad but carries tightens" in p for p in problems)


def test_validate_pool_unknown_pillar_and_role():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    rows[5]["pillar"] = "beauty"
    rows[5]["role"] = "longtail"
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("unknown pillar 'beauty'" in p for p in problems)
    assert any("unknown role 'longtail'" in p for p in problems)


def test_validate_pool_duplicates():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    problems = validate_pool(rows + [dict(rows[0])], parts.products)
    assert any("duplicate keywords" in p for p in problems)


def test_validate_pool_size_band():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)[:40]
    problems = validate_pool(rows, parts.products)
    assert any("outside the 50-70 band" in p for p in problems)


def test_validate_pool_product_coverage():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products)
    rows[0]["product"] = "Mystery item"
    problems = validate_pool(rows, parts.products)
    assert any("outside the prompt table" in p for p in problems)

    dropped = [r for r in _keyword_rows(parts.products) if r["product"] != "Dry body brush"]
    problems = validate_pool(dropped, parts.products)
    assert any("'Dry body brush'" in p for p in problems)


def test_validate_pool_per_product_minimum():
    parts = load_prompt_parts()
    rows = _keyword_rows(parts.products, per_product=4)
    problems = validate_pool(rows, parts.products, min_keywords=10, max_keywords=100)
    assert any("5-keyword minimum" in p for p in problems)


def test_missing_llm_config_raises(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(KeywordGenerationConfigError) as excinfo:
        KeywordGenerator()
    assert "LLM_BASE_URL" in str(excinfo.value)
    assert "LLM_API_KEY" in str(excinfo.value)


def test_http_error_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    generator = KeywordGenerator(
        client=httpx.Client(transport=transport),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    with pytest.raises(KeywordGenerationError, match="LLM HTTP 500"):
        generator.generate()


def test_transport_error_raises():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    generator = KeywordGenerator(
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
        base_url="http://llm.test",
        api_key="mock-test-llm_api_key",
    )
    with pytest.raises(KeywordGenerationError, match="LLM transport error"):
        generator.generate()