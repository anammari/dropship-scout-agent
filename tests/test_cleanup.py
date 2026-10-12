"""Offline tests for the Step-7 cleanup.

This is the only step that deletes anything, so the tests are as much about what
it REFUSES to do as about what it removes: a plan that keeps an unrecognised row,
a path the containment check rejects, a dry run that touches nothing. Everything
happens in `tmp_path` — no network, no supplier tree, no credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.config import settings
from src.pipeline.cleanup import (
    CleanupAction,
    CleanupError,
    apply_cleanup,
    load_ranked_packages,
    plan_cleanup,
    summarize,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, names: List[str]) -> Path:
    """A fake supplier tree with one directory per name; returns the root."""
    root = tmp_path / "morning-anthem-candidates"
    supplier = root / "cjdropshipping"
    for name in names:
        package = supplier / name
        package.mkdir(parents=True)
        (package / "metadata.json").write_text("{}", encoding="utf-8")
        (package / "images").mkdir()
        (package / "images" / "image-1.jpg").write_bytes(b"x")
    supplier.mkdir(parents=True, exist_ok=True)
    return root


def _row(root: Path, name: str, tier: str, score: float) -> Dict[str, Any]:
    return {
        "package_dir": str(root / "cjdropshipping" / name),
        "tier": tier,
        "rank_score": score,
    }


def _report(tmp_path: Path, rows: List[Dict[str, Any]]) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"packages": rows}), encoding="utf-8")
    return path


def _actions(root: Path, rows: List[Dict[str, Any]], **kwargs) -> List[CleanupAction]:
    return plan_cleanup(rows, export_root=root, **kwargs)


def _deleted_names(actions) -> List[str]:
    return sorted(a.package_dir.name for a in actions if a.delete)


# ---------------------------------------------------------------------------
# Planning — the tiers
# ---------------------------------------------------------------------------


def test_disregard_is_deleted_and_shortlist_is_kept(tmp_path):
    root = _tree(tmp_path, ["product-01", "product-02"])
    actions = _actions(
        root,
        [
            _row(root, "product-01", "shortlist", 4.4),
            _row(root, "product-02", "disregard", 1.2),
        ],
    )
    assert _deleted_names(actions) == ["product-02"]
    by_name = {a.package_dir.name: a for a in actions}
    assert by_name["product-01"].reason == "shortlist — kept"


def test_review_is_capped_at_the_strongest_by_rank_score(tmp_path):
    names = [f"product-{n:02d}" for n in range(1, 14)]  # 13 review packages
    root = _tree(tmp_path, names)
    rows = [
        _row(root, name, "review", 3.0 + index / 10)
        for index, name in enumerate(names)
    ]
    actions = _actions(root, rows, review_keep=10)

    # The three weakest go; the ten strongest stay.
    assert _deleted_names(actions) == ["product-01", "product-02", "product-03"]
    kept = [a for a in actions if not a.delete]
    assert len(kept) == 10
    assert "review #1 of 13 — kept (cap 10)" in kept[0].reason


def test_review_below_the_cap_is_entirely_kept(tmp_path):
    root = _tree(tmp_path, ["product-01", "product-02"])
    actions = _actions(
        root,
        [
            _row(root, "product-01", "review", 3.0),
            _row(root, "product-02", "review", 2.6),
        ],
        review_keep=10,
    )
    assert _deleted_names(actions) == []


def test_a_review_tie_breaks_deterministically_by_path(tmp_path):
    root = _tree(tmp_path, ["product-02", "product-01", "product-03"])
    rows = [_row(root, name, "review", 3.0) for name in
            ("product-02", "product-01", "product-03")]
    first = _deleted_names(_actions(root, rows, review_keep=1))
    second = _deleted_names(_actions(root, list(reversed(rows)), review_keep=1))
    # Same report content, different row order: the same two go either way.
    assert first == second == ["product-02", "product-03"]


def test_a_zero_cap_deletes_every_review_package(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    actions = _actions(root, [_row(root, "product-01", "review", 4.0)], review_keep=0)
    assert _deleted_names(actions) == ["product-01"]


def test_a_negative_cap_is_rejected(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    with pytest.raises(CleanupError, match="review_keep"):
        _actions(root, [_row(root, "product-01", "review", 4.0)], review_keep=-1)


def test_an_unknown_tier_is_kept_not_guessed_at(tmp_path):
    """Cleanup deletes only what it positively recognises.

    A future report that grows a tier, or a hand-edited one, must not turn into a
    mass deletion.
    """
    root = _tree(tmp_path, ["product-01"])
    actions = _actions(root, [_row(root, "product-01", "maybe", 3.0)])
    assert _deleted_names(actions) == []
    assert "unknown tier 'maybe'" in actions[0].reason
    assert summarize(actions)["refused"] == 1


def test_a_row_with_no_tier_is_kept(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    row = _row(root, "product-01", "", 3.0)
    actions = _actions(root, [row])
    assert _deleted_names(actions) == []


# ---------------------------------------------------------------------------
# Planning — containment. The report is data, and a path in it could be anything.
# ---------------------------------------------------------------------------


def test_a_path_outside_the_supplier_folder_is_refused(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    outside = tmp_path / "somewhere-else" / "product-99"
    outside.mkdir(parents=True)
    actions = _actions(
        root,
        [
            _row(root, "product-01", "disregard", 1.0),
            {"package_dir": str(outside), "tier": "disregard", "rank_score": 1.0},
        ],
    )
    assert _deleted_names(actions) == ["product-01"]  # the safe one still goes
    refused = [a for a in actions if "refused" in a.reason]
    assert len(refused) == 1 and refused[0].package_dir == outside


def test_a_traversal_path_is_refused(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    traversal = root / "cjdropshipping" / ".." / ".." / ".." / "etc"
    actions = _actions(
        root, [{"package_dir": str(traversal), "tier": "disregard", "rank_score": 1.0}]
    )
    assert _deleted_names(actions) == []
    assert summarize(actions)["refused"] == 1


def test_the_supplier_folder_itself_is_never_deleted(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    supplier = root / "cjdropshipping"
    actions = _actions(
        root, [{"package_dir": str(supplier), "tier": "disregard", "rank_score": 1.0}]
    )
    assert _deleted_names(actions) == []
    assert supplier.is_dir()


def test_a_directory_not_named_product_nn_is_refused(tmp_path):
    root = _tree(tmp_path, ["product-01", "notes"])
    actions = _actions(
        root, [{"package_dir": str(root / "cjdropshipping" / "notes"),
                "tier": "disregard", "rank_score": 1.0}]
    )
    assert _deleted_names(actions) == []


def test_a_row_with_no_path_is_refused(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    actions = _actions(root, [{"tier": "disregard", "rank_score": 1.0}])
    assert _deleted_names(actions) == []
    assert summarize(actions)["refused"] == 1


def test_a_symlink_pointing_outside_the_folder_is_refused(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    outside = tmp_path / "outside" / "product-77"
    outside.mkdir(parents=True)
    link = root / "cjdropshipping" / "product-77"
    link.symlink_to(outside)
    actions = _actions(root, [_row(root, "product-77", "disregard", 1.0)])
    # The resolved path is outside the supplier folder, so it is refused and the
    # real directory survives.
    assert _deleted_names(actions) == []
    assert outside.is_dir()


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_apply_removes_only_the_planned_directories(tmp_path):
    root = _tree(tmp_path, ["product-01", "product-02", "product-03"])
    rows = [
        _row(root, "product-01", "shortlist", 4.4),
        _row(root, "product-02", "disregard", 1.0),
        _row(root, "product-03", "review", 3.0),
    ]
    report = apply_cleanup(_actions(root, rows), export_root=root)

    assert [p.name for p in report.deleted] == ["product-02"]
    assert not (root / "cjdropshipping" / "product-02").exists()
    assert (root / "cjdropshipping" / "product-01").is_dir()
    assert (root / "cjdropshipping" / "product-03").is_dir()


def test_apply_takes_the_whole_package_with_it(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    apply_cleanup(
        _actions(root, [_row(root, "product-01", "disregard", 1.0)]), export_root=root
    )
    # Directory, metadata and images are all gone — nothing is left half-removed.
    assert not (root / "cjdropshipping" / "product-01").exists()


def test_applying_twice_is_a_no_op_not_a_failure(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    rows = [_row(root, "product-01", "disregard", 1.0)]
    first = apply_cleanup(_actions(root, rows), export_root=root)
    second = apply_cleanup(_actions(root, rows), export_root=root)

    assert [p.name for p in first.deleted] == ["product-01"]
    assert second.deleted == []
    assert [p.name for p in second.already_absent] == ["product-01"]
    assert second.failed == []


def test_apply_refuses_an_action_built_outside_the_plan(tmp_path):
    """`apply_cleanup` is a separate entry point, so it re-checks containment.

    A hand-built action must not be able to reach outside the supplier folder.
    """
    root = _tree(tmp_path, ["product-01"])
    outside = tmp_path / "precious" / "product-99"
    outside.mkdir(parents=True)
    action = CleanupAction(
        package_dir=outside, tier="disregard", rank_score=1.0, delete=True,
        reason="hand-built",
    )
    report = apply_cleanup([action], export_root=root)

    assert report.deleted == []
    assert len(report.failed) == 1
    assert outside.is_dir()  # untouched


def test_a_failed_deletion_is_reported_not_swallowed(tmp_path, monkeypatch):
    root = _tree(tmp_path, ["product-01"])
    rows = [_row(root, "product-01", "disregard", 1.0)]

    def _explode(path, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("src.pipeline.cleanup.shutil.rmtree", _explode)
    report = apply_cleanup(_actions(root, rows), export_root=root)

    assert report.deleted == []
    assert report.failed and "permission denied" in report.failed[0][1]
    assert (root / "cjdropshipping" / "product-01").is_dir()


def test_a_kept_package_is_never_touched(tmp_path):
    root = _tree(tmp_path, ["product-01"])
    actions = _actions(root, [_row(root, "product-01", "shortlist", 4.4)])
    report = apply_cleanup(actions, export_root=root)
    assert report.deleted == [] and report.failed == []
    assert (root / "cjdropshipping" / "product-01" / "metadata.json").is_file()


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_counts_kept_deleted_and_refused(tmp_path):
    root = _tree(tmp_path, ["product-01", "product-02", "product-03"])
    actions = _actions(
        root,
        [
            _row(root, "product-01", "shortlist", 4.0),
            _row(root, "product-02", "disregard", 1.0),
            _row(root, "product-03", "mystery", 2.0),
        ],
    )
    assert summarize(actions) == {"kept": 1, "deleted": 1, "refused": 1}


# ---------------------------------------------------------------------------
# Loading the report (fail closed)
# ---------------------------------------------------------------------------


def test_load_reads_the_reports_packages(tmp_path):
    report = _report(tmp_path, [{"package_dir": "/x/product-01", "tier": "review"}])
    assert len(load_ranked_packages(report)) == 1


def test_load_missing_report_names_step_6(tmp_path):
    with pytest.raises(CleanupError, match="run Step 6 first"):
        load_ranked_packages(tmp_path / "absent.json")


def test_load_rejects_malformed_json(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CleanupError, match="not valid JSON"):
        load_ranked_packages(path)


def test_load_rejects_a_payload_without_packages(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")
    with pytest.raises(CleanupError, match="no 'packages' list"):
        load_ranked_packages(path)


def test_load_rejects_an_empty_package_list(tmp_path):
    with pytest.raises(CleanupError, match="no packages"):
        load_ranked_packages(_report(tmp_path, []))


def test_load_defaults_to_the_configured_path(tmp_path, monkeypatch):
    report = _report(tmp_path, [{"package_dir": "/x/product-01", "tier": "review"}])
    monkeypatch.setattr(
        settings, "MORNING_ANTHEM_RANKED_CANDIDATES_PATH", str(report)
    )
    assert len(load_ranked_packages()) == 1


# ---------------------------------------------------------------------------
# Runner: dry run by default
# ---------------------------------------------------------------------------


def _runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "step7_runner",
        Path(__file__).resolve().parents[1] / "scripts" / "cleanup_candidates.py",
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_the_runner_is_a_dry_run_unless_told_otherwise(tmp_path, capsys):
    runner = _runner()
    root = _tree(tmp_path, ["product-01", "product-02"])
    report = _report(
        tmp_path,
        [
            _row(root, "product-01", "shortlist", 4.4),
            _row(root, "product-02", "disregard", 1.0),
        ],
    )
    code = runner.main(["--report", str(report), "--export-root", str(root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "DRY RUN — nothing will be deleted" in out
    assert "product-02" in out  # the plan names it
    # And nothing was touched.
    assert (root / "cjdropshipping" / "product-02").is_dir()


def test_the_runner_deletes_only_with_apply(tmp_path, capsys):
    runner = _runner()
    root = _tree(tmp_path, ["product-01", "product-02"])
    report = _report(
        tmp_path,
        [
            _row(root, "product-01", "shortlist", 4.4),
            _row(root, "product-02", "disregard", 1.0),
        ],
    )
    code = runner.main(
        ["--report", str(report), "--export-root", str(root), "--apply"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "APPLYING DELETIONS" in out
    assert "deleted 1 package(s)" in out
    assert "[STEP 7 COMPLETE]" in out
    assert not (root / "cjdropshipping" / "product-02").exists()
    assert (root / "cjdropshipping" / "product-01").is_dir()


def test_the_runner_names_what_it_refused(tmp_path, capsys):
    runner = _runner()
    root = _tree(tmp_path, ["product-01"])
    outside = tmp_path / "elsewhere" / "product-99"
    outside.mkdir(parents=True)
    report = _report(
        tmp_path,
        [{"package_dir": str(outside), "tier": "disregard", "rank_score": 1.0}],
    )
    assert runner.main(
        ["--report", str(report), "--export-root", str(root), "--apply"]
    ) == 0
    out = capsys.readouterr().out
    assert "refused (NOT deleted)" in out
    assert outside.is_dir()


def test_the_runner_halts_without_a_report(tmp_path, capsys):
    runner = _runner()
    code = runner.main(
        ["--report", str(tmp_path / "absent.json"), "--export-root", str(tmp_path)]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "Step 7 Ranking Report" in out
    assert "rank_optimal_candidates.py" in out


def test_the_runner_halts_when_a_deletion_fails(tmp_path, capsys, monkeypatch):
    runner = _runner()
    root = _tree(tmp_path, ["product-01"])
    report = _report(tmp_path, [_row(root, "product-01", "disregard", 1.0)])

    def _explode(path, *args, **kwargs):
        raise OSError("file in use")

    monkeypatch.setattr("src.pipeline.cleanup.shutil.rmtree", _explode)
    assert runner.main(
        ["--report", str(report), "--export-root", str(root), "--apply"]
    ) == 1
    out = capsys.readouterr().out
    assert "Step 7 Cleanup" in out
    assert "file in use" in out


def test_the_runner_review_cap_flag_is_respected(tmp_path, capsys):
    runner = _runner()
    root = _tree(tmp_path, ["product-01", "product-02"])
    report = _report(
        tmp_path,
        [
            _row(root, "product-01", "review", 4.0),
            _row(root, "product-02", "review", 3.0),
        ],
    )
    assert runner.main(
        ["--report", str(report), "--export-root", str(root),
         "--review-keep", "1", "--apply"]
    ) == 0
    assert (root / "cjdropshipping" / "product-01").is_dir()
    assert not (root / "cjdropshipping" / "product-02").exists()
