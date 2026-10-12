"""Offline unit tests for the Step-2 Jev product-intent gate.

Hermetic: the System One endpoint is an `httpx.MockTransport` scripted with
the verified `{answers, usage}` envelope, and the candidate file is a tmp
fixture. No network, no OpenRouter spend, and — importantly — no HasData
credits: the gate exists precisely to stop non-product strings from reaching a
billable Step-3 scrape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.config import settings
from src.ranking.jev_client import JevConfigError, JevError
from src.research.trend_gate import (
    INTENT_OPTIONS,
    TREND_GATE_QUESTION,
    TrendCandidate,
    TrendGateError,
    TrendIntentGate,
    load_trend_candidates,
    write_gated_deliverable,
)

# ---------------------------------------------------------------------------
# Fixtures and scripted transport
# ---------------------------------------------------------------------------


def _candidate(keyword: str, pillar: str = "brew_fuel", evidence: str = "") -> Dict:
    return {
        "keyword": keyword,
        "pillar": pillar,
        "demand_evidence": evidence or "coverage 73, rising 5-y (geo=AU)",
    }


def _write_candidates(tmp_path: Path, rows: List[Dict], name: str = "trends.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"keywords": rows}), encoding="utf-8")
    return path


class ScriptedJevClient:
    """Stands in for `JevClient`: records calls, replays canned envelopes.

    A script entry is an `answers` mapping, an Exception (raised instead), or
    None (a response carrying no `answers` object).
    """

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


def _answers(count: int, verdicts: List[str]) -> Dict[str, dict]:
    """One choice answer per candidate index, in order."""
    assert len(verdicts) == count
    return {
        f"{index}__{TREND_GATE_QUESTION}": {"type": "choice", "choice": verdict}
        for index, verdict in enumerate(verdicts)
    }


def _gate(script, batch_size: int = 8) -> TrendIntentGate:
    return TrendIntentGate(
        client=ScriptedJevClient(script), batch_size=batch_size
    )


# ---------------------------------------------------------------------------
# The verdict set
# ---------------------------------------------------------------------------


def test_intent_options_are_the_closed_four_way_set():
    assert set(INTENT_OPTIONS) == {
        "product_search",
        "informational",
        "news_or_brand",
        "service_or_digital",
    }
    # Every option is a described choice, not a bare key.
    assert all(isinstance(text, str) and text for text in INTENT_OPTIONS.values())


def test_only_a_product_search_survives_the_gate():
    candidates = [
        TrendCandidate("portable milk frother", "brew_fuel", "cov 80"),
        TrendCandidate("how to froth milk without a frother", "brew_fuel", "cov 60"),
        TrendCandidate("morning routine news", "mindful_awakening", "cov 55"),
        TrendCandidate("meditation app free", "mindful_awakening", "cov 40"),
    ]
    gate = _gate(
        [
            _answers(
                4,
                [
                    "product_search",
                    "informational",
                    "news_or_brand",
                    "service_or_digital",
                ],
            )
        ]
    )
    results = gate.gate(candidates)

    assert [row.kept for row in results] == [True, False, False, False]
    assert results[0].intent == "product_search"
    assert results[1].intent == "informational"
    assert results[2].intent == "news_or_brand"
    assert results[3].intent == "service_or_digital"
    # Every drop explains itself.
    assert all(row.note for row in results[1:])
    assert results[0].note == ""


# ---------------------------------------------------------------------------
# Batch envelope
# ---------------------------------------------------------------------------


def test_one_call_carries_every_candidate_as_its_own_question():
    candidates = [
        TrendCandidate(f"query {i}", "morning_table") for i in range(3)
    ]
    client = ScriptedJevClient([_answers(3, ["product_search"] * 3)])
    TrendIntentGate(client=client, batch_size=8).gate(candidates)

    (call,) = client.calls
    state, questions = call["state"], call["questions"]
    # The shared state carries the batch's candidates, verbatim.
    assert [row["query"] for row in state["candidates"]] == [
        "query 0", "query 1", "query 2",
    ]
    assert [row["index"] for row in state["candidates"]] == [0, 1, 2]
    # One choice question per candidate, keyed `<index>__product_intent`.
    assert sorted(questions) == [
        f"0__{TREND_GATE_QUESTION}",
        f"1__{TREND_GATE_QUESTION}",
        f"2__{TREND_GATE_QUESTION}",
    ]
    for question in questions.values():
        assert question["type"] == "choice"
        assert question["criteria"] == INTENT_OPTIONS


def test_the_batch_is_split_at_the_configured_size():
    candidates = [TrendCandidate(f"query {i}", "brew_fuel") for i in range(5)]
    client = ScriptedJevClient(
        [_answers(2, ["product_search"] * 2), _answers(2, ["product_search"] * 2), _answers(1, ["product_search"])]
    )
    TrendIntentGate(client=client, batch_size=2).gate(candidates)

    assert [len(call["state"]["candidates"]) for call in client.calls] == [2, 2, 1]


def test_the_batch_instruction_addresses_the_candidate_by_index():
    client = ScriptedJevClient([_answers(1, ["product_search"])])
    TrendIntentGate(client=client, batch_size=8).gate(
        [TrendCandidate("portable milk frother", "brew_fuel")]
    )
    (call,) = client.calls
    instructions = call["questions"][f"0__{TREND_GATE_QUESTION}"]["instructions"]
    # The state is referenced by a backticked path, per TypeSafe's guidance.
    assert "`candidates.0`" in instructions


def test_the_gate_reads_its_batch_size_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "TREND_GATE_BATCH_SIZE", 3)
    candidates = [TrendCandidate(f"query {i}", "brew_fuel") for i in range(7)]
    client = ScriptedJevClient(
        [
            _answers(3, ["product_search"] * 3),
            _answers(3, ["product_search"] * 3),
            _answers(1, ["product_search"]),
        ]
    )
    TrendIntentGate(client=client).gate(candidates)
    assert [len(call["state"]["candidates"]) for call in client.calls] == [3, 3, 1]


def test_a_non_positive_batch_size_is_rejected():
    with pytest.raises(TrendGateError, match="batch_size"):
        TrendIntentGate(client=ScriptedJevClient(), batch_size=0)


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_a_failed_batch_retries_once_then_drops_only_that_batch():
    candidates = [TrendCandidate(f"query {i}", "brew_fuel") for i in range(4)]
    client = ScriptedJevClient(
        [
            JevError("HTTP 500"),
            _answers(2, ["product_search"] * 2),  # the retry succeeds
            JevError("HTTP 500"),
            JevError("HTTP 500"),  # the second batch fails twice
        ]
    )
    results = TrendIntentGate(client=client, batch_size=2).gate(candidates)

    assert len(client.calls) == 4  # 2 batches x (1 attempt + 1 retry)
    assert [row.kept for row in results] == [True, True, False, False]
    # The dropped batch says why, and carries no invented verdict.
    assert results[2].intent is None
    assert "failed twice" in results[2].note
    assert results[3].intent is None


def test_a_response_without_answers_is_retried_like_a_failure():
    # `decide()` raises JevError when the envelope carries no `answers`, so an
    # envelope drift degrades one batch instead of crashing the run.
    client = ScriptedJevClient([None, _answers(1, ["product_search"])])
    results = TrendIntentGate(client=client, batch_size=1).gate(
        [TrendCandidate("garlic press", "morning_table")]
    )
    assert [row.kept for row in results] == [True]


def test_an_unparsable_verdict_drops_the_string():
    # A choice answer with no usable `choice` key is not a pass.
    client = ScriptedJevClient([{"0__product_intent": {"type": "choice"}}])
    (row,) = TrendIntentGate(client=client, batch_size=1).gate(
        [TrendCandidate("garlic press", "morning_table")]
    )
    assert row.kept is False
    assert row.intent is None
    assert "unclassified" in row.note


def test_an_unexpected_verdict_key_drops_the_string():
    # A verdict outside the closed set (a hallucinated option) is a drop, never
    # a silent pass.
    client = ScriptedJevClient([_answers(1, ["maybe_a_product"])])
    (row,) = TrendIntentGate(client=client, batch_size=1).gate(
        [TrendCandidate("garlic press", "morning_table")]
    )
    assert row.kept is False
    assert row.intent == "maybe_a_product"


# ---------------------------------------------------------------------------
# Anti-hallucination: Jev classifies, it never authors
# ---------------------------------------------------------------------------


def test_the_gate_carries_the_candidate_fields_through_verbatim():
    candidate = TrendCandidate(
        "hands free dog leash", "sunrise_move", "coverage 64, rising 5-y"
    )
    results = _gate([_answers(1, ["product_search"])]).gate([candidate])
    (row,) = results
    assert (row.keyword, row.pillar, row.demand_evidence) == (
        candidate.keyword,
        candidate.pillar,
        candidate.demand_evidence,
    )


def test_the_question_never_asks_jev_to_produce_a_string():
    client = ScriptedJevClient([_answers(1, ["product_search"])])
    TrendIntentGate(client=client, batch_size=1).gate(
        [TrendCandidate("garlic press", "morning_table")]
    )
    (call,) = client.calls
    (question,) = call["questions"].values()
    # A choice question with a fixed criteria map: there is no slot for the
    # model to write a keyword into.
    assert question["type"] == "choice"
    assert set(question) == {"type", "instructions", "criteria"}


def test_a_retailer_qualified_survivor_is_screened_by_the_banned_boundary():
    # The pilot finding: Jev reads "milk frother kmart" as a product search,
    # because it is one — but the keyword engine's banned-token boundary
    # forbids the string, so scraping it would spend a Step-3 credit on a
    # keyword the Step-4 bank could never carry. The deterministic screen
    # catches it after the verdict and records both facts.
    client = ScriptedJevClient([_answers(2, ["product_search", "product_search"])])
    results = TrendIntentGate(client=client, batch_size=8).gate(
        [
            TrendCandidate("milk frother machine", "brew_fuel"),
            TrendCandidate("milk frother kmart", "brew_fuel"),
        ]
    )
    assert [row.kept for row in results] == [True, False]
    assert results[1].intent == "product_search"  # Jev said yes...
    assert results[1].note == "banned token 'kmart'"  # ...the boundary said no
    assert results[0].note == ""


def test_the_boundary_screen_costs_no_extra_model_call():
    client = ScriptedJevClient([_answers(1, ["product_search"])])
    TrendIntentGate(client=client, batch_size=8).gate(
        [TrendCandidate("milk frother kmart", "brew_fuel")]
    )
    assert len(client.calls) == 1


def test_gating_an_empty_list_makes_no_call():
    client = ScriptedJevClient([])
    assert TrendIntentGate(client=client, batch_size=8).gate([]) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# load_trend_candidates (fail closed)
# ---------------------------------------------------------------------------


def test_load_reads_the_research_rows(tmp_path):
    path = _write_candidates(
        tmp_path,
        [
            _candidate("portable milk frother", "brew_fuel", "cov 80"),
            _candidate("sand free beach towel", "sunrise_move"),
        ],
    )
    loaded = load_trend_candidates(path)
    assert [row.keyword for row in loaded] == [
        "portable milk frother",
        "sand free beach towel",
    ]
    assert loaded[0].pillar == "brew_fuel"
    assert loaded[0].demand_evidence == "cov 80"
    assert loaded[1].demand_evidence == "coverage 73, rising 5-y (geo=AU)"


def test_load_accepts_a_bare_list_payload(tmp_path):
    path = _write_candidates(tmp_path, [_candidate("garlic press", "morning_table")])
    payload = json.loads(path.read_text())
    path.write_text(json.dumps(payload["keywords"]), encoding="utf-8")
    assert [row.keyword for row in load_trend_candidates(path)] == ["garlic press"]


def test_load_defaults_to_the_configured_path(tmp_path, monkeypatch):
    path = _write_candidates(tmp_path, [_candidate("garlic press", "morning_table")])
    monkeypatch.setattr(
        settings, "MORNING_ANTHEM_TREND_KEYWORDS_PATH", str(path)
    )
    assert [row.keyword for row in load_trend_candidates()] == ["garlic press"]


def test_load_missing_file_names_the_research_step(tmp_path):
    with pytest.raises(TrendGateError) as excinfo:
        load_trend_candidates(tmp_path / "absent.json")
    message = str(excinfo.value)
    assert "not found" in message
    assert "Google Trends" in message


def test_load_rejects_malformed_json(tmp_path):
    path = tmp_path / "trends.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TrendGateError, match="not valid JSON"):
        load_trend_candidates(path)


def test_load_rejects_an_empty_candidate_list(tmp_path):
    with pytest.raises(TrendGateError, match="empty"):
        load_trend_candidates(_write_candidates(tmp_path, []))


def test_load_rejects_a_payload_without_a_keyword_list(tmp_path):
    path = _write_candidates(tmp_path, [])
    path.write_text(json.dumps({"keywords": {"not": "a list"}}), encoding="utf-8")
    with pytest.raises(TrendGateError, match="must be a JSON list"):
        load_trend_candidates(path)


def test_load_rejects_a_non_object_row(tmp_path):
    path = _write_candidates(tmp_path, [])
    path.write_text(json.dumps({"keywords": ["just a string"]}), encoding="utf-8")
    with pytest.raises(TrendGateError, match="not an object"):
        load_trend_candidates(path)


def test_load_rejects_a_blank_or_non_string_keyword(tmp_path):
    with pytest.raises(TrendGateError, match="no usable 'keyword'"):
        load_trend_candidates(_write_candidates(tmp_path, [_candidate("   ")]))
    with pytest.raises(TrendGateError, match="no usable 'keyword'"):
        load_trend_candidates(_write_candidates(tmp_path, [{"keyword": 42}]))


def test_load_rejects_a_pillar_outside_the_five_morning_anthem_keys(tmp_path):
    # A stale pillar from the retired scheme would silently mis-tag every row
    # downstream, so the loader fails closed and names the fix.
    path = _write_candidates(tmp_path, [_candidate("garlic press", "curated_home")])
    with pytest.raises(TrendGateError) as excinfo:
        load_trend_candidates(path)
    message = str(excinfo.value)
    assert "curated_home" in message
    assert "brew_fuel" in message


# ---------------------------------------------------------------------------
# Deliverable writers
# ---------------------------------------------------------------------------


def test_writers_keep_only_survivors_and_preserve_the_pre_gate_audit_trail(tmp_path):
    candidates = [
        TrendCandidate("portable milk frother", "brew_fuel", "cov 80"),
        TrendCandidate("how to froth milk", "brew_fuel", "cov 60"),
    ]
    results = _gate(
        [_answers(2, ["product_search", "informational"])]
    ).gate(candidates)

    output = tmp_path / "step-2.json"
    raw = tmp_path / "step-2-raw.json"
    write_gated_deliverable(results, output, raw)

    gated = json.loads(output.read_text())
    assert [row["keyword"] for row in gated["keywords"]] == ["portable milk frother"]
    assert gated["keywords"][0]["intent"] == "product_search"
    assert gated["gate"] == "jev_product_intent"
    assert gated["candidates_gated"] == 2
    assert gated["candidates_dropped"] == 1

    trail = json.loads(raw.read_text())
    assert [row["keyword"] for row in trail["keywords"]] == [
        "portable milk frother",
        "how to froth milk",
    ]
    assert [row["kept"] for row in trail["keywords"]] == [True, False]
    assert trail["keywords"][1]["note"] == "intent informational"


def test_writers_create_the_destination_directory(tmp_path):
    output = tmp_path / "nested" / "step-2.json"
    write_gated_deliverable([], output)
    assert json.loads(output.read_text())["keywords"] == []


# ---------------------------------------------------------------------------
# Runner: CLI, exit codes and the intervention blocks
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gate_trend_keywords_runner",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "gate_trend_keywords.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_runner_raw_sibling_names_the_audit_trail():
    runner = _runner()
    assert runner._raw_sibling(Path("outputs/json/step-2.json")) == Path(
        "outputs/json/step-2-raw.json"
    )


def test_runner_missing_input_halts_with_instructions(tmp_path, capsys):
    runner = _runner()
    code = runner.main(["--input", str(tmp_path / "absent.json")])
    assert code == 1
    out = capsys.readouterr().out
    assert "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]" in out
    assert "Step 2 Trend Keywords" in out
    assert "brew_fuel" in out  # the pillar list is spelled out for the fix


def test_runner_missing_jev_credentials_halts_with_instructions(
    tmp_path, capsys, monkeypatch
):
    # The step gates on the operator's credential cadence: an absent
    # OPENROUTER_API_KEY is an intervention, not a traceback.
    runner = _runner()
    source = _write_candidates(
        tmp_path, [_candidate("portable milk frother", "brew_fuel")]
    )
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code = runner.main(
        ["--input", str(source), "--output", str(tmp_path / "out.json")]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]" in out
    assert "Jev Gate Configuration" in out
    assert "OPENROUTER_API_KEY" in out


def test_runner_writes_the_filtered_file_and_reports_the_tally(
    tmp_path, capsys, monkeypatch
):
    runner = _runner()
    source = _write_candidates(
        tmp_path,
        [
            _candidate("portable milk frother", "brew_fuel"),
            _candidate("silk eye mask", "restful_reset"),
        ],
    )
    output = tmp_path / "out.json"

    # Patch the RUNNER's own binding: the script imports `TrendIntentGate` by
    # name at load time, so patching the module it came from would leave the
    # real client in place — and a real client is a live, billable call.
    monkeypatch.setattr(
        runner,
        "TrendIntentGate",
        lambda batch_size=None: _gate(
            [_answers(2, ["product_search", "news_or_brand"])], batch_size=batch_size
        ),
    )
    code = runner.main(
        ["--input", str(source), "--output", str(output), "--batch-size", "8"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "[STEP 2 COMPLETE]" in out
    assert "product_search=1" in out
    assert "news_or_brand: 1" in out
    gated = json.loads(output.read_text())
    assert [row["keyword"] for row in gated["keywords"]] == [
        "portable milk frother"
    ]


def test_runner_all_dropped_halts_with_the_exhausted_block(
    tmp_path, capsys, monkeypatch
):
    runner = _runner()
    source = _write_candidates(
        tmp_path, [_candidate("how to froth milk", "brew_fuel")]
    )
    output = tmp_path / "out.json"

    monkeypatch.setattr(
        runner,
        "TrendIntentGate",
        lambda batch_size=None: _gate(
            [_answers(1, ["informational"])], batch_size=batch_size
        ),
    )
    code = runner.main(
        ["--input", str(source), "--output", str(output)]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Step 2 Funnel Exhausted" in out
    assert "informational" in out
    # The research record is still written, so the drop is inspectable.
    assert json.loads(output.read_text())["keywords"] == []
