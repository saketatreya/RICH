"""Durable filesystem writes shared by the coding and run-engine paths."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    """Flush a directory so a rename or unlink inside it survives a crash.

    fsync on a file makes its bytes durable; the name that points at them
    lives in the parent directory, and until that is flushed too a power
    loss can leave a complete file that nothing refers to. Two modules had
    grown identical copies of this; a durability rule written twice is a
    rule that can be fixed once.
    """

    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
