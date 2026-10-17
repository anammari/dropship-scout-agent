"""Write a deliverable so a crash can never leave a half-written file.

Every runner in this repo writes JSON deliverables that the *next* step reads as
its input. A plain `Path.write_text` truncates the destination first, so a crash
mid-write — or a full disk — leaves a truncated file sitting exactly where the
next stage looks for a complete one. Worse, it destroys the *previous* good
deliverable while doing it.

`write_text_atomic` removes both risks: the content is written to a temporary
sibling, flushed to disk, and then moved into place with `os.replace`, which is
atomic on POSIX and on Windows. A reader therefore sees either the old file or
the new one, never a partial one, and a crash before the replace leaves the
previous deliverable untouched.

The temporary file is created in the *same directory* as the destination, so the
final move stays within one filesystem (a cross-device rename is not atomic) and
is cleaned up if anything goes wrong.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp file + `os.replace`).

    The destination's parent directory is created if it does not exist. On any
    failure the temporary file is removed and the destination keeps whatever it
    held before — a failed run leaves no partial artefact behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            # Durability before the rename: without this a power loss can leave
            # the *new* name pointing at unwritten blocks, which is worse than
            # the old file it replaced.
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
