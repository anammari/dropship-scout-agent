"""Offline tests for the rehearsal harness itself.

The harness drives the whole chain with every external service stubbed, so nothing
here touches the network — and it must stay that way, because its whole point is
that a rehearsal cannot spend a HasData credit. These tests keep it from rotting:
if a stub stops matching the runner it is stubbing, the chain stops completing and
this fails.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _harness():
    spec = importlib.util.spec_from_file_location(
        "rehearsal",
        Path(__file__).resolve().parents[1] / "scripts" / "rehearse_pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_whole_chain_completes_against_synthetic_data(tmp_path, capsys):
    """Step 3 -> 7, every step, with only the external services stubbed."""
    harness = _harness()
    scratch = tmp_path / "scratch"

    code = harness.main(["--scratch", str(scratch)])

    assert code == 0
    out = capsys.readouterr().out
    assert "every step completed and produced its artefact" in out

    # Each step's handoff exists, which is the thing a rehearsal is for.
    produced = sorted(p.name for p in (scratch / "json").iterdir())
    assert produced == [
        "morning-anthem-step-2-trend-keywords.json",
        "morning-anthem-step-3-gold-products.json",
        "morning-anthem-step-4-keyword-bank.json",
        "morning-anthem-step-6-ranked-candidates.json",
    ]
    assert (scratch / "md" / "morning-anthem-step-6-ranked-candidates.md").is_file()


def test_the_chain_produces_and_then_cleans_real_packages(tmp_path, capsys):
    harness = _harness()
    scratch = tmp_path / "scratch"
    assert harness.main(["--scratch", str(scratch)]) == 0

    out = capsys.readouterr().out
    batch = scratch / "candidates"
    packages = sorted(batch.glob("product-*"))

    # The real exporter wrote the real contract, and Step 7 pruned the tree.
    assert packages, "the chain should leave packages behind"
    for package in packages:
        metadata = package / "metadata.json"
        assert metadata.is_file()
        assert len(list((package / "images").glob("image-*"))) >= 3
    assert "to delete" in out and "survived cleanup" in out


def test_the_rehearsal_never_touches_the_real_workspace(tmp_path):
    """It deletes directories in Step 7, so the scratch guard is load-bearing."""
    harness = _harness()
    from src.config import settings

    real = Path(settings.MORNING_ANTHEM_EXPORT_DIR).resolve()
    scratch = real / "rehearsal"
    with pytest.raises(SystemExit) as exit_info:
        harness.main(["--scratch", str(scratch)])

    assert exit_info.value.code == 2
    # It refused before creating anything — and the real tree is not on disk yet
    # in a fresh checkout, which is exactly when a runaway rehearsal would be
    # hardest to notice.
    assert not scratch.exists()


def test_the_stub_tables_agree_with_each_other(tmp_path):
    """The synthetic world is one table; a drifting stub shows up here first."""
    harness = _harness()

    keywords = {row[0] for row in harness.SYNTHETIC_POOL}
    assert set(harness.SYNTHETIC_LISTINGS) == keywords
    # Every listing the scrape returns has a pillar verdict waiting for it.
    for row in harness.SYNTHETIC_POOL:
        assert row[2] in harness.PILLAR_BY_TITLE
    # And every gold title's exact keyword has a ranking verdict, in the form the
    # stubbed extractor titles its listings.
    for row in harness.SYNTHETIC_POOL:
        title = f"{harness.exact_keyword(row[2])} (CJ listing)"
        assert title in harness.RANKING_BY_TITLE
