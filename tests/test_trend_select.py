"""Offline unit tests for the Step-2 Jev keyword-selection pass.

Hermetic: the System One endpoint is an `httpx.MockTransport` scripted with the
verified `{answers}` envelope, and the pool is built in memory. No network, no
OpenRouter spend, no HasData credits.

The pass has one job that matters — turning a scored pool into a **stable**
selection of N. These tests pin the properties that make it trustworthy: the
cut is code (not the model), it is deterministic, the per-pillar floor is
honoured, a failed batch cannot promote a candidate, and Jev never authors a
keyword.
"""

from __future__ import annotations

import contextlib
import json
import signal
from typing import Any, Dict, List, Optional

import pytest

from src.config import settings
from src.ranking.jev_client import JevError
from src.research.trend_gate import GatedKeyword, TrendGateError
from src.research.trend_select import (
    PILLARS,
    QUALITY_LEVELS,
    TREND_SELECT_QUESTION,
    TrendKeywordSelector,
    write_selected_deliverable,
)

# ---------------------------------------------------------------------------
# Fixtures and scripted transport
# ---------------------------------------------------------------------------


def _kept(keyword: str, pillar: str = "brew_fuel", evidence: str = "mean 10") -> GatedKeyword:
    return GatedKeyword(
        keyword=keyword,
        pillar=pillar,
        demand_evidence=evidence,
        intent="product_search",
        kept=True,
    )


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


def _scores(values: List[float]) -> Dict[str, dict]:
    """One score answer per candidate index; Jev's raw score is 0-based."""
    return {
        f"{index}__{TREND_SELECT_QUESTION}": {
            "type": "score",
            "score": value - 1,  # the ranker's +1 shift puts 1..5 back
            "legend": {str(i): level for i, level in enumerate(QUALITY_LEVELS)},
        }
        for index, value in enumerate(values)
    }


def _selector(script, **kwargs) -> TrendKeywordSelector:
    kwargs.setdefault("batch_size", 12)
    kwargs.setdefault("target", 2)
    kwargs.setdefault("min_per_pillar", 0)
    return TrendKeywordSelector(client=ScriptedJevClient(script), **kwargs)


# ---------------------------------------------------------------------------
# The verdict set and the batch envelope
# ---------------------------------------------------------------------------


def test_quality_levels_are_five_ordered_levels():
    assert len(QUALITY_LEVELS) == 5
    assert all(isinstance(level, str) and level for level in QUALITY_LEVELS)
    # The bottom level must be able to reject a brand/retailer string outright.
    assert "brand or retailer" in QUALITY_LEVELS[0].lower()
    # The top level is the bar: specific, physical, evidenced, in-pillar.
    assert "pillar" in QUALITY_LEVELS[-1].lower()


def test_one_score_question_per_candidate():
    pool = [_kept(f"kw {i}") for i in range(3)]
    client = ScriptedJevClient([_scores([5, 4, 3])])
    TrendKeywordSelector(
        client=client, batch_size=12, target=2, min_per_pillar=0
    ).select(pool)

    (call,) = client.calls
    questions = call["questions"]
    assert sorted(questions) == [
        f"0__{TREND_SELECT_QUESTION}",
        f"1__{TREND_SELECT_QUESTION}",
        f"2__{TREND_SELECT_QUESTION}",
    ]
    for question in questions.values():
        assert question["type"] == "score"
        assert question["criteria"] == QUALITY_LEVELS


def test_the_state_carries_the_context_a_decision_model_needs():
    pool = [
        _kept("coffee milk frother", "brew_fuel", "related query, index 100"),
        _kept("silk eye mask", "restful_reset", "5-y mean interest 3"),
    ]
    client = ScriptedJevClient([_scores([5, 4])])
    TrendKeywordSelector(
        client=client, batch_size=12, target=2, min_per_pillar=0
    ).select(pool)

    (call,) = client.calls
    state = call["state"]
    # The brand frame, the task and the selection size ride once per call.
    assert "The Morning Anthem" in state["brand"]
    assert "CJdropshipping" in state["brand"]
    assert "2" in state["task"]
    # Every one of the five pillars is described, so pillar fit is judgeable.
    assert set(state["pillars"]) == {
        "brew_fuel", "sunrise_move", "mindful_awakening",
        "restful_reset", "morning_table",
    }
    # Per candidate: the query, the pillar, the evidence VERBATIM and — because
    # a 5-year mean and a rising query are not the same claim — its kind.
    first, second = state["candidates"]
    assert first["query"] == "coffee milk frother"
    assert first["pillar"] == "brew_fuel"
    assert first["trends_evidence"] == "related query, index 100"
    assert first["evidence_kind"] == "related-query index"
    assert second["trends_evidence"] == "5-y mean interest 3"
    assert second["evidence_kind"] == "5-year mean interest"


def test_the_batch_is_split_at_the_configured_size():
    pool = [_kept(f"kw {i}") for i in range(5)]
    client = ScriptedJevClient([_scores([5, 5]), _scores([5, 5]), _scores([5])])
    TrendKeywordSelector(
        client=client, batch_size=2, target=5, min_per_pillar=0
    ).select(pool)
    assert [len(call["state"]["candidates"]) for call in client.calls] == [2, 2, 1]


def test_the_instruction_names_the_supplier_catalogue():
    client = ScriptedJevClient([_scores([5])])
    TrendKeywordSelector(
        client=client, batch_size=12, target=1, min_per_pillar=0
    ).select([_kept("garlic press")])
    (call,) = client.calls
    instructions = call["questions"][f"0__{TREND_SELECT_QUESTION}"]["instructions"]
    assert "`candidates.0`" in instructions
    assert "CJdropshipping" in instructions


def test_a_non_positive_target_or_batch_is_rejected():
    with pytest.raises(TrendGateError, match="target"):
        TrendKeywordSelector(client=ScriptedJevClient(), target=0)
    with pytest.raises(TrendGateError, match="batch_size"):
        TrendKeywordSelector(client=ScriptedJevClient(), batch_size=0)
    with pytest.raises(TrendGateError, match="min_per_pillar"):
        TrendKeywordSelector(client=ScriptedJevClient(), min_per_pillar=-1)


# ---------------------------------------------------------------------------
# The cut is code, and it is deterministic
# ---------------------------------------------------------------------------


def test_the_cut_takes_the_highest_scores_in_order():
    pool = [_kept("low"), _kept("high"), _kept("mid")]
    selected = _selector([_scores([2, 5, 3])], target=2).select(pool)

    picked = [row for row in selected if row.selected]
    assert [row.keyword for row in picked] == ["high", "mid"]
    assert [row.rank for row in picked] == [1, 2]
    # The rest is returned too, unranked and unselected.
    rest = [row for row in selected if not row.selected]
    assert [row.keyword for row in rest] == ["low"]
    assert rest[0].rank is None


def test_the_cut_is_exactly_the_target():
    pool = [_kept(f"kw {i}") for i in range(6)]
    selected = _selector([_scores([5, 4, 3, 2, 1, 1])], target=3).select(pool)
    assert sum(1 for row in selected if row.selected) == 3


def test_a_target_wider_than_the_pool_selects_everything():
    pool = [_kept("a"), _kept("b")]
    selected = _selector([_scores([5, 4])], target=30).select(pool)
    assert all(row.selected for row in selected)


def test_equal_scores_break_deterministically_by_evidence_then_string():
    # Same score: the candidate carrying demand evidence wins, then the
    # alphabetically-earlier string. Without a total order the selection would
    # depend on Jev's answer order and stop being reproducible.
    pool = [
        _kept("zebra tool", evidence=""),
        _kept("alpha tool", evidence="mean 4"),
        _kept("beta tool", evidence="mean 4"),
    ]
    selected = _selector([_scores([3, 3, 3])], target=2).select(pool)
    picked = [row.keyword for row in selected if row.selected]
    assert picked == ["alpha tool", "beta tool"]


def test_the_same_pool_and_scores_always_produce_the_same_selection():
    pool = [_kept(f"kw {i}") for i in range(5)]
    scores = [3, 5, 5, 1, 4]
    first = _selector([_scores(scores)], target=3).select(pool)
    second = _selector([_scores(scores)], target=3).select(pool)
    assert [r.keyword for r in first if r.selected] == [
        r.keyword for r in second if r.selected
    ]


def test_the_score_is_shifted_onto_the_1_to_5_scale():
    # Jev's raw score is 0-based along the level list; the reader shifts it, so
    # a raw 4 must land as 5.0 and a raw 0 as 1.0.
    pool = [_kept("a"), _kept("b")]
    selected = _selector([_scores([5, 1])], target=1).select(pool)
    by_keyword = {row.keyword: row for row in selected}
    assert by_keyword["a"].quality_score == pytest.approx(5.0)
    assert by_keyword["b"].quality_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The per-pillar floor
# ---------------------------------------------------------------------------


def test_the_pillar_floor_lifts_a_weak_pillar_into_the_selection():
    # Three brew_fuel rows outscore the only sunrise_move row, so a pure
    # best-of cut would drop that pillar from the launch entirely.
    pool = [
        _kept("brew a", "brew_fuel"),
        _kept("brew b", "brew_fuel"),
        _kept("brew c", "brew_fuel"),
        _kept("move a", "sunrise_move"),
    ]
    selected = _selector(
        [_scores([5, 5, 5, 1])], target=2, min_per_pillar=1
    ).select(pool)
    picked = [row for row in selected if row.selected]
    assert "move a" in [row.keyword for row in picked]
    # Floors fill first, in PILLARS order — so brew_fuel leads and the weak
    # pillar takes the second slot ahead of two higher-scoring brew rows.
    assert picked[0].keyword == "brew a"
    assert len(picked) == 2


def test_the_default_cut_is_proportional_not_a_score_race():
    # Two brew rows outscore the only sunrise_move row, but a score race would
    # hand both brew slots over and leave that pillar unrepresented in the
    # launch. Proportional quotas give brew its share and sunrise_move its one.
    pool = [
        _kept("brew a", "brew_fuel"),
        _kept("brew b", "brew_fuel"),
        _kept("move a", "sunrise_move"),
    ]
    selected = _selector([_scores([5, 4, 1])], target=2, min_per_pillar=0).select(pool)
    assert [row.keyword for row in selected if row.selected] == ["brew a", "move a"]


def test_a_pure_best_of_cut_is_still_available():
    pool = [
        _kept("brew a", "brew_fuel"),
        _kept("brew b", "brew_fuel"),
        _kept("move a", "sunrise_move"),
    ]
    selector = TrendKeywordSelector(
        client=ScriptedJevClient([_scores([5, 4, 1])]),
        target=2,
        min_per_pillar=0,
        pillar_quota=False,
    )
    selected = selector.select(pool)
    assert [row.keyword for row in selected if row.selected] == ["brew a", "brew b"]


def test_the_floor_never_exceeds_what_a_pillar_has():
    pool = [
        _kept("brew a", "brew_fuel"),
        _kept("move a", "sunrise_move"),
    ]
    # Floor of 2 per pillar with only one candidate each: both still fit target.
    selected = _selector([_scores([5, 4])], target=2, min_per_pillar=2).select(pool)
    assert {row.pillar for row in selected if row.selected} == {
        "brew_fuel", "sunrise_move",
    }


def test_a_floor_that_cannot_fit_the_target_fails_loudly():
    # Five pillars x floor 2 = 10 slots for a target of 2. Overspending the
    # Step-3 budget silently would be worse than stopping, and ignoring the
    # floor would break the representation promise — so it raises.
    pool = [
        _kept("a", "brew_fuel"),
        _kept("b", "sunrise_move"),
        _kept("c", "mindful_awakening"),
        _kept("d", "restful_reset"),
        _kept("e", "morning_table"),
    ]
    with pytest.raises(TrendGateError, match="min_per_pillar"):
        _selector([_scores([5, 5, 5, 5, 5])], target=2, min_per_pillar=2).select(pool)


def test_quotas_are_proportional_to_the_pool():
    from src.research.trend_select import pillar_quotas

    # A pool shaped like the researched demand: one pillar dominates.
    quotas = pillar_quotas(
        {"brew_fuel": 40, "sunrise_move": 8, "mindful_awakening": 6,
         "restful_reset": 4, "morning_table": 2},
        target=30,
        min_per_pillar=2,
    )
    assert sum(quotas.values()) == 30
    # Roughly the pool's proportions — not all 30 to the loudest pillar. The
    # dominant pillar lands at 19, not its ideal 20: the floor lifts the
    # smallest pillar from 1 to 2 and that slot comes off the biggest one.
    assert quotas["brew_fuel"] == 19
    assert quotas["sunrise_move"] == 4
    assert quotas["mindful_awakening"] == 3
    assert quotas["morning_table"] == 2


def test_quotas_sum_to_the_target_whenever_the_pool_can_fill_it():
    from src.research.trend_select import pillar_quotas

    for counts in (
        {"brew_fuel": 9, "restful_reset": 1, "morning_table": 1, "mindful_awakening": 1},
        {"brew_fuel": 40, "sunrise_move": 8, "morning_table": 2},
        {"brew_fuel": 1},
        {"brew_fuel": 6, "morning_table": 6},
    ):
        quotas = pillar_quotas(counts, target=20, min_per_pillar=2)
        assert sum(quotas.values()) == min(20, sum(counts.values()))


def test_an_infeasible_floor_raises_rather_than_overspending():
    from src.research.trend_select import pillar_quotas

    # 3 pillars x floor 2 cannot fit a target of 5, and no pillar holds more
    # than its floor to trim from. Overspending the budget silently is the
    # failure mode this guard exists to prevent.
    with pytest.raises(TrendGateError, match="floor alone needs"):
        pillar_quotas(
            {"brew_fuel": 7, "sunrise_move": 7, "morning_table": 6},
            target=5,
            min_per_pillar=2,
        )


def test_quotas_never_exceed_what_a_pillar_actually_holds():
    from src.research.trend_select import pillar_quotas

    quotas = pillar_quotas({"brew_fuel": 30, "morning_table": 1}, target=20, min_per_pillar=2)
    assert quotas["morning_table"] == 1
    assert quotas["brew_fuel"] == 19


def test_every_pillar_holding_candidates_is_represented():
    from src.research.trend_select import pillar_quotas

    # The floor is what stops a pillar being crowded out entirely.
    quotas = pillar_quotas(
        {"brew_fuel": 50, "sunrise_move": 3, "mindful_awakening": 3,
         "restful_reset": 2, "morning_table": 2},
        target=30,
        min_per_pillar=2,
    )
    assert all(count >= 2 for count in quotas.values())


def test_the_cut_selects_each_pillars_best_up_to_its_quota():
    # Within a pillar the best rows win; across pillars the quotas decide.
    pool = [
        _kept("brew top", "brew_fuel"),
        _kept("brew mid", "brew_fuel"),
        _kept("brew low", "brew_fuel"),
        _kept("brew lowest", "brew_fuel"),
        _kept("move only", "sunrise_move"),
        _kept("mind only", "mindful_awakening"),
    ]
    selected = _selector(
        [_scores([5, 4, 3, 2, 1, 1])], target=4, min_per_pillar=1
    ).select(pool)
    picked = [row.keyword for row in selected if row.selected]
    # brew's quota is 2 of 4 (its proportional share) so its two best go in...
    assert picked[:2] == ["brew top", "brew mid"]
    # ...and each of the other pillars takes its floor slot.
    assert set(picked) == {"brew top", "brew mid", "move only", "mind only"}
    assert "brew low" not in picked


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_a_failed_batch_retries_once_then_ranks_its_candidates_last():
    pool = [_kept("a"), _kept("b"), _kept("c")]
    client = ScriptedJevClient(
        [
            JevError("HTTP 500"),
            _scores([5, 5]),          # the retry succeeds
            JevError("HTTP 500"),
            JevError("HTTP 500"),     # the second batch fails twice
        ]
    )
    selected = TrendKeywordSelector(
        client=client, batch_size=2, target=3, min_per_pillar=0
    ).select(pool)

    by_keyword = {row.keyword: row for row in selected}
    # The unclassified candidate is not promoted ahead of a scored one...
    assert by_keyword["c"].quality_score is None
    assert by_keyword["c"].note and "failed twice" in by_keyword["c"].note
    assert [row.keyword for row in selected if row.selected][:2] == ["a", "b"]
    # ...but it still fills the remaining slot rather than silently vanishing.
    assert by_keyword["c"].selected is True


def test_an_unusable_score_answer_is_treated_as_unclassified():
    pool = [_kept("a"), _kept("b")]
    client = ScriptedJevClient(
        [{"0__keyword_quality": {"type": "score"}, "1__keyword_quality": {"score": 4}}]
    )
    selected = TrendKeywordSelector(
        client=client, batch_size=12, target=1, min_per_pillar=0
    ).select(pool)
    picked = [row for row in selected if row.selected]
    assert [row.keyword for row in picked] == ["b"]
    by_keyword = {row.keyword: row for row in selected}
    assert by_keyword["a"].quality_score is None
    assert "no usable score" in by_keyword["a"].note


def test_every_batch_failing_selects_nothing():
    pool = [_kept("a"), _kept("b")]
    selected = _selector(
        [JevError("x"), JevError("x")], target=2
    ).select(pool)
    # Nothing scored at all, so NOTHING is selected: spending the Step-3
    # budget on an unranked pool is the failure this pass prevents.
    assert [row.keyword for row in selected if row.selected] == []
    # ...and they come back so the caller can see what happened.
    assert len(selected) == 2


def test_a_partial_failure_still_fills_slots_with_the_unranked_rows():
    # A pool of 3 with one bad batch: the scored rows rank first, and the
    # unranked row still fills the last slot rather than wasting a paid-for
    # Step-3 place. (Contrast the all-failed case above.)
    pool = [_kept("a"), _kept("b"), _kept("c")]
    client = ScriptedJevClient(
        [
            JevError("x"), _scores([1, 5]),  # batch 1 (a, b): both scored, b wins
            JevError("x"), JevError("x"),    # batch 2 (c): fails twice
        ]
    )
    selected = TrendKeywordSelector(
        client=client, batch_size=2, target=3, min_per_pillar=0
    ).select(pool)
    picked = [row for row in selected if row.selected]
    assert picked[0].keyword == "b"
    assert len(picked) == 3  # the unranked rows fill the rest


def test_selecting_an_empty_pool_makes_no_call():
    client = ScriptedJevClient([])
    assert TrendKeywordSelector(client=client).select([]) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# Anti-hallucination: Jev scores, it never authors
# ---------------------------------------------------------------------------


def test_the_selection_carries_the_candidate_fields_through_verbatim():
    pool = [_kept("hands free dog leash", "sunrise_move", "rising query +90%")]
    (row,) = _selector([_scores([4])], target=1).select(pool)
    assert (row.keyword, row.pillar, row.demand_evidence) == (
        "hands free dog leash",
        "sunrise_move",
        "rising query +90%",
    )


def test_the_question_offers_no_slot_to_write_a_keyword():
    client = ScriptedJevClient([_scores([5])])
    TrendKeywordSelector(
        client=client, batch_size=12, target=1, min_per_pillar=0
    ).select([_kept("garlic press")])
    (call,) = client.calls
    (question,) = call["questions"].values()
    assert set(question) == {"type", "instructions", "criteria"}


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_the_writer_keeps_only_the_selected_keywords_in_the_intake(tmp_path):
    pool = [_kept("high"), _kept("low")]
    selected = _selector([_scores([5, 1])], target=1).select(pool)
    output = tmp_path / "step-2.json"
    gated = tmp_path / "step-2-gated.json"
    write_selected_deliverable(selected, output, gated)

    payload = json.loads(output.read_text())
    # Step 3 pays per keyword, so its intake holds ONLY the selection.
    assert [row["keyword"] for row in payload["keywords"]] == ["high"]
    assert payload["selected"] == 1
    assert payload["gated_pool"] == 2
    assert payload["reserve"] == 1
    assert payload["selection"] == "jev_keyword_quality"
    assert payload["keywords"][0]["rank"] == 1
    assert payload["keywords"][0]["quality_score"] == pytest.approx(5.0)


def test_the_writer_preserves_the_pool_as_the_rerun_reserve(tmp_path):
    pool = [_kept("high"), _kept("low")]
    selected = _selector([_scores([5, 1])], target=1).select(pool)
    output = tmp_path / "step-2.json"
    gated = tmp_path / "step-2-gated.json"
    write_selected_deliverable(selected, output, gated)

    reserve = json.loads(gated.read_text())
    assert [row["keyword"] for row in reserve["keywords"]] == ["high", "low"]
    assert [row["selected"] for row in reserve["keywords"]] == [True, False]
    # The reserve keeps every score, so a different --target can be tried
    # without paying for trends again.
    assert [row["quality_score"] for row in reserve["keywords"]] == [
        pytest.approx(5.0), pytest.approx(1.0),
    ]


def test_the_writer_creates_the_destination_directory(tmp_path):
    output = tmp_path / "nested" / "step-2.json"
    write_selected_deliverable([], output)
    assert json.loads(output.read_text())["keywords"] == []


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "gate_trend_keywords_runner_select",
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "gate_trend_keywords.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_runner_gated_sibling_names_the_reserve():
    from pathlib import Path

    runner = _runner()
    assert runner._gated_sibling(Path("outputs/json/step-2.json")) == Path(
        "outputs/json/step-2-gated.json"
    )


def test_runner_gates_then_selects_and_writes_both_files(tmp_path, capsys, monkeypatch):
    import sys

    runner = _runner()
    source = tmp_path / "candidates.json"
    source.write_text(
        json.dumps(
            {
                "keywords": [
                    {"keyword": "high", "pillar": "brew_fuel", "demand_evidence": "m 9"},
                    {"keyword": "low", "pillar": "brew_fuel", "demand_evidence": "m 1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "step-2.json"

    # Patch the RUNNER's own bindings: a real client would be a live call.
    monkeypatch.setattr(
        runner, "TrendIntentGate",
        lambda batch_size=None: _FakeGate(),
    )
    monkeypatch.setattr(
        runner, "TrendKeywordSelector",
        lambda **kwargs: _selector([_scores([5, 1])], target=1),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["gate_trend_keywords.py", "--input", str(source), "--output", str(output)],
    )
    code = runner.main()

    assert code == 0
    out = capsys.readouterr().out
    assert "[STEP 2 COMPLETE]" in out
    assert "selected=1" in out
    assert json.loads(output.read_text())["keywords"][0]["keyword"] == "high"
    gated = json.loads((tmp_path / "step-2-gated.json").read_text())
    assert len(gated["keywords"]) == 2


def test_runner_skip_selection_keeps_the_whole_pool(tmp_path, capsys, monkeypatch):
    import sys

    runner = _runner()
    source = tmp_path / "candidates.json"
    source.write_text(
        json.dumps(
            {
                "keywords": [
                    {"keyword": "a", "pillar": "brew_fuel", "demand_evidence": "m 9"},
                    {"keyword": "b", "pillar": "brew_fuel", "demand_evidence": "m 1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "step-2.json"
    monkeypatch.setattr(runner, "TrendIntentGate", lambda batch_size=None: _FakeGate())
    monkeypatch.setattr(
        sys, "argv",
        [
            "gate_trend_keywords.py", "--input", str(source),
            "--output", str(output), "--skip-selection",
        ],
    )
    assert runner.main() == 0
    # The gate-only path leaves every survivor as Step 3's intake — and the
    # banner says the pool is what Step 3 will pay for.
    payload = json.loads(output.read_text())
    assert len(payload["keywords"]) == 2
    assert "survivor" in capsys.readouterr().out


class _FakeGate:
    """Gates every candidate as a product search (the selection pass is what
    these tests drive)."""

    def __init__(self) -> None:
        pass

    def gate(self, candidates):
        return [
            GatedKeyword(
                keyword=row.keyword,
                pillar=row.pillar,
                demand_evidence=row.demand_evidence,
                intent="product_search",
                kept=True,
            )
            for row in candidates
        ]


def test_runner_halts_when_jev_scored_nothing(tmp_path, capsys, monkeypatch):
    # The gate answered but the selection scored nothing: spending 300 credits
    # on an unranked pool is the failure this pass prevents, so it stops.
    import sys

    runner = _runner()
    source = tmp_path / "candidates.json"
    source.write_text(
        json.dumps(
            {
                "keywords": [
                    {"keyword": "a", "pillar": "brew_fuel", "demand_evidence": "m 9"},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "step-2.json"
    monkeypatch.setattr(runner, "TrendIntentGate", lambda batch_size=None: _FakeGate())
    monkeypatch.setattr(
        runner, "TrendKeywordSelector",
        lambda **kwargs: _selector([JevError("x"), JevError("x")], target=1),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["gate_trend_keywords.py", "--input", str(source), "--output", str(output)],
    )
    code = runner.main()

    assert code == 1
    out = capsys.readouterr().out
    assert "[ACTION REQUIRED: HUMAN INTERVENTION NEEDED]" in out
    assert "Step 2 Selection Failed" in out
    assert "--skip-selection" in out  # the documented way through, unranked
    # Nothing was written as Step 3's intake.
    assert not output.exists()


def test_selector_reads_its_defaults_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "TREND_SELECT_TARGET", 7)
    monkeypatch.setattr(settings, "TREND_SELECT_BATCH_SIZE", 3)
    monkeypatch.setattr(settings, "TREND_SELECT_MIN_PER_PILLAR", 0)
    selector = TrendKeywordSelector(client=ScriptedJevClient())
    assert (selector.target, selector.batch_size, selector.min_per_pillar) == (7, 3, 0)


# ---------------------------------------------------------------------------
# Quota-loop safety (review question 1)
#
# The distribution loops must be bounded by the AVAILABLE CANDIDATES, not by the
# slot count alone — a loop that chases slots the pool cannot fill never ends.
# These tests pin the bound, and `_deadline` turns a regression into a failure
# instead of a hung test run.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _deadline(seconds: float = 5.0):
    """Fail if the block runs longer than `seconds` (a non-terminating loop)."""

    def _boom(signum, frame):  # pragma: no cover - only on regression
        raise AssertionError(
            f"quota allocation exceeded {seconds}s — a loop is not terminating"
        )

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(max(1, int(seconds)))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_quota_allocation_terminates_when_the_pool_is_far_smaller_than_the_target():
    from src.research.trend_select import pillar_quotas

    with _deadline():
        quotas = pillar_quotas({"brew_fuel": 1}, target=1000, min_per_pillar=2)
    assert quotas == {"brew_fuel": 1}


def test_quota_allocation_terminates_when_every_pillar_is_at_its_cap():
    from src.research.trend_select import pillar_quotas

    # Every pillar holding exactly one candidate, target far beyond the pool:
    # the fill loop must stop at the pool, not chase the remaining slots.
    counts = {p: 1 for p in PILLARS}
    with _deadline():
        quotas = pillar_quotas(counts, target=500, min_per_pillar=1)
    assert quotas == counts
    assert sum(quotas.values()) == 5


def test_quota_allocation_terminates_on_degenerate_inputs():
    from src.research.trend_select import pillar_quotas

    with _deadline():
        # One candidate, target 1.
        assert pillar_quotas({"brew_fuel": 1}, target=1, min_per_pillar=0) == {
            "brew_fuel": 1
        }
        # Zero floor, many pillars, tiny target.
        assert sum(
            pillar_quotas(
                {"brew_fuel": 3, "sunrise_move": 3, "morning_table": 3},
                target=2,
                min_per_pillar=0,
            ).values()
        ) == 2
        # Equal pools, odd target.
        assert sum(
            pillar_quotas(
                {"brew_fuel": 10, "morning_table": 10}, target=7, min_per_pillar=0
            ).values()
        ) == 7
        # Very lopsided pools with a floor.
        quotas = pillar_quotas(
            {"brew_fuel": 1000, "morning_table": 1}, target=999, min_per_pillar=2
        )
        assert quotas["morning_table"] == 1
        assert quotas["brew_fuel"] == 998


def test_quota_invariants_hold_across_a_random_sweep():
    """Termination + the three invariants, over many synthetic pools.

    Seeded, so a failure is reproducible. This is the property test that would
    have caught both bugs found by hand while writing the allocation (a quota
    above the pool size, and a target the floor cannot fit).
    """
    import random

    from src.research.trend_select import pillar_quotas

    rng = random.Random(20261012)
    checked = 0
    with _deadline(20.0):
        for _ in range(400):
            pillar_count = rng.randint(1, len(PILLARS))
            counts = {
                p: rng.randint(1, 60) for p in list(PILLARS)[:pillar_count]
            }
            target = rng.randint(1, 120)
            floor = rng.randint(0, 4)
            total = sum(counts.values())

            # A floor that cannot fit the target must raise, not loop.
            feasible_floor = floor * len(counts) <= total and (
                floor * len(counts) <= target or total <= target
            )
            try:
                quotas = pillar_quotas(counts, target=target, min_per_pillar=floor)
            except TrendGateError:
                assert not feasible_floor, (
                    f"raised on a feasible pool: counts={counts} "
                    f"target={target} floor={floor}"
                )
                continue

            checked += 1
            # (a) never more than the pillar holds
            assert all(quotas[p] <= counts[p] for p in counts), (counts, quotas)
            # (b) never fewer than the floor, where the pool can afford it
            assert all(
                quotas[p] >= min(floor, counts[p]) for p in counts
            ), (counts, quotas, floor)
            # (c) as many slots as the target, or exactly the pool when the
            #     pool is the smaller of the two
            assert sum(quotas.values()) == min(target, total), (
                counts,
                target,
                floor,
                quotas,
            )
    assert checked > 300  # the sweep exercised real allocations, not mostly raises
