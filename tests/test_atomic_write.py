"""Offline tests for the atomic deliverable writer.

Every runner writes JSON that the *next* stage reads as its input, so a crash
mid-write must never leave a truncated file where a complete one is expected —
nor destroy the previous good deliverable while failing. These tests pin both
properties without touching the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.pipeline.atomic_write import write_text_atomic


def test_writes_the_content(tmp_path):
    target = tmp_path / "deliverable.json"
    write_text_atomic(target, '{"ok": true}\n')
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'


def test_creates_the_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "deliverable.json"
    write_text_atomic(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_leaves_no_temporary_sibling_behind(tmp_path):
    target = tmp_path / "deliverable.json"
    write_text_atomic(target, "first")
    write_text_atomic(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    # Only the deliverable is in the directory — a stray `.tmp` would be a
    # half-written file for a later reader to trip over.
    assert [p.name for p in tmp_path.iterdir()] == ["deliverable.json"]


def test_replaces_an_existing_file_wholesale(tmp_path):
    target = tmp_path / "deliverable.json"
    target.write_text("a much longer previous deliverable", encoding="utf-8")
    write_text_atomic(target, "short\n")
    # Truncation, not an in-place overwrite: the tail of the old content is gone
    # rather than showing through.
    assert target.read_text(encoding="utf-8") == "short\n"


def test_a_failed_write_keeps_the_previous_deliverable_and_cleans_up(
    tmp_path, monkeypatch
):
    """The property that matters: a failed run leaves the old file intact.

    `os.replace` is stubbed to fail, standing in for whatever goes wrong between
    writing the temp file and swapping it in (a full disk, a permissions change, a
    killed process).
    """
    target = tmp_path / "deliverable.json"
    target.write_text("previous good deliverable", encoding="utf-8")

    def _explode(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(
        "src.pipeline.atomic_write.os.replace", _explode
    )
    with pytest.raises(OSError, match="no space left"):
        write_text_atomic(target, "the new deliverable")

    # The old deliverable is untouched...
    assert target.read_text(encoding="utf-8") == "previous good deliverable"
    # ...and the abandoned temp file was cleaned up.
    assert [p.name for p in tmp_path.iterdir()] == ["deliverable.json"]


def test_the_temp_file_is_created_beside_the_destination(tmp_path, monkeypatch):
    """A temp file in another directory would make the final move non-atomic."""
    seen = {}
    real_mkstemp = __import__("tempfile").mkstemp

    def _record(**kwargs):
        seen.update(kwargs)
        return real_mkstemp(**kwargs)

    monkeypatch.setattr("src.pipeline.atomic_write.tempfile.mkstemp", _record)
    target = tmp_path / "sub" / "deliverable.json"
    write_text_atomic(target, "x")
    assert Path(seen["dir"]) == tmp_path / "sub"


def test_the_content_is_flushed_and_fsynced_before_the_rename(tmp_path, monkeypatch):
    """Durability before the rename: otherwise a crash can leave the new name
    pointing at unwritten blocks, which is worse than the file it replaced."""
    synced = []
    real_fsync = os.fsync

    def _record(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("src.pipeline.atomic_write.os.fsync", _record)
    write_text_atomic(tmp_path / "deliverable.json", "x")
    assert len(synced) == 1
