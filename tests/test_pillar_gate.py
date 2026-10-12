"""Offline unit tests for the Step-3 Jev pillar gate.

Hermetic: the System One endpoint is an `httpx.MockTransport` scripted with the
verified `{answers}` envelope, and the products/rows are built in memory. No
network, no HasData credits, no OpenRouter spend.

The gate has two jobs worth pinning: classify every curated product into exactly
one of the five Morning Anthem pillars (or discard it), and do that with the
*enriched* context — the numeric price, rating and review count — because that is
the whole reason it runs after curation rather than before the scrape.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from src.config import settings
from src.evaluators.gold_curator import PILLARS, GoldStandardProduct
from src.extractors.shopping_row import ShoppingRow
from src.ranking.jev_client import PILLAR_OPTIONS, JevError
from src.research.pillar_gate import (
    DISCARD_NOTE,
    PILLAR_GATE_OPTIONS,
    PILLAR_GATE_QUESTION,
    GatedProduct,
    PillarCandidate,
    PillarGate,
    attach_pillars,
)
from src.research.trend_gate import TrendGateError

# ---------------------------------------------------------------------------
# Fixtures and scripted transport
# ---------------------------------------------------------------------------


def _product(
    name: str = "Bodum Chambord French Press",
    url: str = "https://www.google.com.au/shopping/product/123",
    **overrides: Any,
) -> GoldStandardProduct:
    base: Dict[str, Any] = dict(
        name=name,
        url=url,
        description="Borosilicate glass French press, 1 L.",
        retail_price_text="$89.95",
        compliance_note="Inert glass and steel kitchen tool.",
        demand_evidence="rating 4.7/5, 234 reviews (Google Shopping AU)",
        source_keyword="french press",
    )
    base.update(overrides)
    return GoldStandardProduct(**base)


def _row(url: str = "https://www.google.com.au/shopping/product/123", **overrides: Any) -> ShoppingRow:
    base: Dict[str, Any] = dict(
        title="Bodum Chambord French Press",
        url=url,
        merchant="Myer",
        price_text="$89.95",
        delivery_text="Free delivery",
        image_url="https://files.hasdata.com/x.webp",
        description="Borosilicate glass French press, 1 L.",
        rating=4.7,
        rating_count=234,
        source_keyword="french press",
        price_value=89.95,
    )
    base.update(overrides)
    return ShoppingRow(**base)


def _candidate(name: str = "Bodum Chambord French Press", url: str = "https://www.google.com.au/shopping/product/123") -> PillarCandidate:
    return PillarCandidate.from_product(_product(name, url), _row(url))


class ScriptedJevClient:
    """Stands in for `JevClient`: records calls, replays canned `answers`."""

    def __init__(self, script: Optional[List[Any]] = None) -> None:
        self.script = list(script or [])
        self.calls: List[Dict[str, Any]] = []

    def decide(self, state: Any, questions: Dict[str, Dict[str, Any]]):
        self.calls.append({"state": state, "questions": questions})
        if not self.script:
            raise AssertionError("ScriptedJevClient ran out of scripted responses")
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            raise JevError("System One response carries no `answers` object")
        return entry


def _answers(verdicts: List[str]) -> Dict[str, dict]:
    """One choice answer per product index, in order."""
    return {
        f"{index}__{PILLAR_GATE_QUESTION}": {"type": "choice", "choice": verdict}
        for index, verdict in enumerate(verdicts)
    }


def _gate(script, batch_size: int = 4) -> PillarGate:
    return PillarGate(client=ScriptedJevClient(script), batch_size=batch_size)


# ---------------------------------------------------------------------------
# The verdict set
# ---------------------------------------------------------------------------


def test_options_are_the_five_pillars_plus_this_stages_own_discard():
    assert set(PILLAR_GATE_OPTIONS) == set(PILLARS) | {"discard"}
    # The pillar descriptions are the journey-wide ones, reused rather than
    # re-spelled, so the gate and the Step-6 ranker cannot drift apart.
    for pillar in PILLARS:
        assert PILLAR_GATE_OPTIONS[pillar] == PILLAR_OPTIONS[pillar]
    # `discard` means more here than it does to the Step-6 ranker: a Step-3
    # product can fail simply by not being a Morning Anthem product.
    assert "fits none of the five" in PILLAR_GATE_OPTIONS["discard"]
    assert PILLAR_GATE_OPTIONS["discard"] == DISCARD_NOTE


def test_the_five_pillars_keep_and_discard_drops():
    candidates = [_candidate(name=f"product {i}") for i in range(6)]
    verdicts = [*PILLARS, "discard"]
    results = _gate([_answers(verdicts)], batch_size=6).gate(candidates)

    assert [row.kept for row in results] == [True] * 5 + [False]
    assert [row.pillar for row in results] == [*PILLARS, None]
    assert results[-1].note == "pillar discard"
    # A kept product carries no note; a discarded one always explains itself.
    assert all(row.note == "" for row in results if row.kept)
    assert all(row.note for row in results if not row.kept)


def test_an_unparsable_or_unknown_verdict_drops_the_product():
    # Neither a missing `choice` key nor a hallucinated option may pass as a
    # pillar: an unrecognised verdict is a drop, never a silent default.
    candidates = [_candidate(), _candidate()]
    results = _gate(
        [
            {
                "0__pillar": {"type": "choice"},
                "1__pillar": {"type": "choice", "choice": "kitchen_stuff"},
            }
        ]
    ).gate(candidates)

    assert [row.kept for row in results] == [False, False]
    assert results[0].pillar is None and "unclassified" in results[0].note
    assert results[1].pillar is None and "kitchen_stuff" in results[1].note


# ---------------------------------------------------------------------------
# The context the gate runs on — the reason it runs last
# ---------------------------------------------------------------------------


def test_the_state_carries_the_numeric_price_rating_and_review_count():
    candidate = PillarCandidate.from_product(
        _product(), _row(price_text="$89.95", price_value=89.95, rating=4.7, rating_count=234)
    )
    client = ScriptedJevClient([_answers(["morning_table"])])
    PillarGate(client=client, batch_size=4).gate([candidate])

    (call,) = client.calls
    (entry,) = call["state"]["products"]
    # Numbers as numbers, so the model reasons over them rather than parsing.
    assert entry["price_value"] == pytest.approx(89.95)
    assert entry["rating"] == pytest.approx(4.7)
    assert entry["review_count"] == 234
    # ...alongside the raw display string and the formatted evidence line.
    assert entry["price_text"] == "$89.95"
    assert "4.7/5" in entry["demand_evidence"]
    assert entry["name"] == "Bodum Chambord French Press"
    assert entry["source_keyword"] == "french press"


def test_the_state_describes_all_five_pillars_and_the_brand():
    client = ScriptedJevClient([_answers(["brew_fuel"])])
    PillarGate(client=client, batch_size=4).gate([_candidate()])
    (call,) = client.calls
    state = call["state"]
    assert set(state["pillars"]) == set(PILLARS)
    assert "The Morning Anthem" in state["brand"]
    assert "Australia" in state["brand"]


def test_the_state_never_carries_the_curators_own_pillar_guess():
    # The gate is the pillar's single author; a hint from the curator would bias
    # the classification the gate exists to make. The curator leaves the field
    # empty, and nothing here reads it.
    client = ScriptedJevClient([_answers(["brew_fuel"])])
    PillarGate(client=client, batch_size=4).gate([_candidate()])
    (call,) = client.calls
    (entry,) = call["state"]["products"]
    assert "pillar" not in entry
    for value in entry.values():
        assert value != "brew_fuel"


def test_the_batch_is_split_at_the_configured_size_and_reads_settings(monkeypatch):
    candidates = [_candidate(url=f"https://x/{i}") for i in range(5)]
    client = ScriptedJevClient(
        [_answers(["brew_fuel"] * 2), _answers(["brew_fuel"] * 2), _answers(["brew_fuel"])]
    )
    PillarGate(client=client, batch_size=2).gate(candidates)
    assert [len(call["state"]["products"]) for call in client.calls] == [2, 2, 1]

    monkeypatch.setattr(settings, "PILLAR_GATE_BATCH_SIZE", 5)
    assert PillarGate(client=ScriptedJevClient()).batch_size == 5


def test_a_non_positive_batch_size_is_rejected():
    with pytest.raises(TrendGateError, match="batch_size"):
        PillarGate(client=ScriptedJevClient(), batch_size=0)


# ---------------------------------------------------------------------------
# PillarCandidate.from_product — the join
# ---------------------------------------------------------------------------


def test_the_candidate_takes_its_numbers_from_the_row_and_its_text_from_the_product():
    candidate = PillarCandidate.from_product(_product(), _row())
    assert candidate.name == "Bodum Chambord French Press"
    assert candidate.price_value == pytest.approx(89.95)
    assert candidate.rating == pytest.approx(4.7)
    assert candidate.review_count == 234


def test_a_product_whose_row_is_missing_still_yields_a_candidate_without_numbers():
    # The join key is the url the curator already verified. A row that has gone
    # missing costs the numbers, not the product — and nothing is guessed.
    candidate = PillarCandidate.from_product(_product(url="https://x/gone"), None)
    assert candidate.url == "https://x/gone"
    assert candidate.price_value is None
    assert candidate.rating is None
    assert candidate.review_count is None
    assert candidate.retail_price_text == "$89.95"  # the product still has it
    assert candidate.demand_evidence


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_a_failed_batch_retries_once_then_drops_only_that_batch():
    candidates = [_candidate(url=f"https://x/{i}") for i in range(4)]
    client = ScriptedJevClient(
        [
            JevError("HTTP 500"),
            _answers(["brew_fuel", "silk" if False else "restful_reset"]),
            JevError("HTTP 500"),
            JevError("HTTP 500"),
        ]
    )
    results = PillarGate(client=client, batch_size=2).gate(candidates)

    assert len(client.calls) == 4  # 2 batches x (1 attempt + 1 retry)
    assert [row.kept for row in results] == [True, True, False, False]
    assert "failed twice" in results[2].note
    assert results[2].pillar is None


def test_a_response_without_answers_is_retried_like_a_failure():
    client = ScriptedJevClient([None, _answers(["morning_table"])])
    results = PillarGate(client=client, batch_size=1).gate([_candidate()])
    assert [row.kept for row in results] == [True]


def test_gating_an_empty_list_makes_no_call():
    client = ScriptedJevClient([])
    assert PillarGate(client=client).gate([]) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# Anti-hallucination: the gate authors the pillar and nothing else
# ---------------------------------------------------------------------------


def test_the_candidate_fields_survive_the_gate_verbatim():
    candidate = _candidate()
    (row,) = _gate([_answers(["brew_fuel"])]).gate([candidate])
    assert row.candidate is candidate
    for field_name in (
        "url", "name", "description", "retail_price_text",
        "price_value", "rating", "review_count",
        "demand_evidence", "source_keyword",
    ):
        assert getattr(row.candidate, field_name) == getattr(candidate, field_name)


def test_the_question_offers_no_slot_to_write_a_product_fact():
    client = ScriptedJevClient([_answers(["brew_fuel"])])
    PillarGate(client=client, batch_size=4).gate([_candidate()])
    (call,) = client.calls
    (question,) = call["questions"].values()
    # A choice over a fixed criteria map: there is no free-text slot at all.
    assert set(question) == {"type", "instructions", "criteria"}
    assert question["type"] == "choice"


def test_attach_pillars_writes_only_what_the_gate_issued():
    kept = _product(name="kept", url="https://x/1")
    dropped = _product(name="dropped", url="https://x/2")
    results = [
        GatedProduct(candidate=_candidate("kept", "https://x/1"), pillar="brew_fuel", kept=True),
        GatedProduct(candidate=_candidate("dropped", "https://x/2"), pillar=None, kept=False, note="pillar discard"),
    ]
    survivors = attach_pillars([kept, dropped], results)

    assert [p.name for p in survivors] == ["kept"]
    # The gate's verdict is on the surviving product; the dropped one keeps the
    # empty pillar the curator left, and never reaches the deliverable.
    assert survivors[0].pillar == "brew_fuel"
    assert dropped.pillar == ""


def test_attach_pillars_ignores_a_product_the_gate_never_saw():
    orphan = _product(name="orphan", url="https://x/unseen")
    assert attach_pillars([orphan], []) == []
    assert orphan.pillar == ""


# ---------------------------------------------------------------------------
# Runner integration: scrape -> curate -> gate -> write
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "step3_runner_pillar_gate",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_gold_standard_research.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


class _FakeScraper:
    def __init__(self, rows):
        self._rows = rows
        self.last_run = {"source": "hasdata", "credits": 10}

    def scrape_keywords(self, keywords):
        return list(self._rows)


def _run_with(tmp_path, monkeypatch, rows, products, gated, keywords=("french press",)):
    """Drive the runner with every live stage stubbed, and return its exit code."""
    import sys

    runner = _runner()
    source = tmp_path / "keywords.json"
    source.write_text(
        json.dumps({"keywords": [{"keyword": k, "pillar": "brew_fuel"} for k in keywords]}),
        encoding="utf-8",
    )
    output = tmp_path / "gold.json"

    monkeypatch.setattr(runner, "HasDataShoppingScraper", lambda **kw: _FakeScraper(rows))
    monkeypatch.setattr(runner, "GoldProductCurator", lambda: _FakeCurator(products))
    monkeypatch.setattr(runner, "PillarGate", lambda **kw: _FakeGate(gated))
    monkeypatch.setattr(
        sys, "argv",
        ["run_gold_standard_research.py", "--keywords", str(source),
         "--output", str(output), "--allow-over-pilot-budget"],
    )
    code = runner.main()
    return runner, code, output


class _FakeCurator:
    def __init__(self, products):
        self._products = products

    def curate(self, rows):
        return list(self._products)


class _FakeGate:
    def __init__(self, results):
        self._results = results

    def gate(self, candidates):
        return list(self._results)


def test_runner_classifies_after_curation_and_writes_the_pillars(tmp_path, monkeypatch, capsys):
    row = _row()
    product = _product()
    gated = [
        GatedProduct(
            candidate=PillarCandidate.from_product(product, row),
            pillar="morning_table",
            kept=True,
        )
    ]
    _, code, output = _run_with(tmp_path, monkeypatch, [row], [product], gated)

    assert code is None  # main() returns None when the run completes
    payload = json.loads(output.read_text())
    assert payload["products"][0]["pillar"] == "morning_table"
    # The gate's verdict is reported in the payload and the console summary.
    assert payload["pillar_gate"] == {
        "classified": 1, "discarded": 0, "by_pillar": {"morning_table": 1},
    }
    out = capsys.readouterr().out
    assert "after the Jev pillar gate" in out
    assert "morning_table" in out


def test_runner_halts_when_the_pillar_gate_discards_everything(tmp_path, monkeypatch, capsys):
    row = _row()
    product = _product()
    gated = [
        GatedProduct(
            candidate=PillarCandidate.from_product(product, row),
            pillar=None,
            kept=False,
            note="pillar discard",
        )
    ]
    # The helper raises before it can return the path, so name it here.
    output = tmp_path / "gold.json"

    with pytest.raises(SystemExit) as exit_info:
        _run_with(tmp_path, monkeypatch, [row], [product], gated)
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    # `parser.exit` writes its message to stderr; the per-product verdicts went
    # to stdout before it.
    assert "kept zero products" in captured.err
    assert "pillar discard" in captured.out
    # Nothing is written as Steps 4+6's input.
    assert not output.exists()


def test_runner_reports_the_gates_verdicts_for_each_discarded_product(tmp_path, monkeypatch, capsys):
    rows = [_row(), _row(url="https://x/2")]
    products = [_product(), _product(name="A candle holder", url="https://x/2")]
    gated = [
        GatedProduct(candidate=PillarCandidate.from_product(products[0], rows[0]),
                     pillar="brew_fuel", kept=True),
        GatedProduct(candidate=PillarCandidate.from_product(products[1], rows[1]),
                     pillar=None, kept=False, note="pillar discard"),
    ]
    _, code, _ = _run_with(tmp_path, monkeypatch, rows, products, gated)

    assert code is None
    out = capsys.readouterr().out
    assert "discarded by the gate: 1" in out
    assert "A candle holder" in out
