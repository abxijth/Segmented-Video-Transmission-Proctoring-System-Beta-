"""Per-session locks shared across the server.

Both the upload handler and the background merge worker mutate the same
on-disk session (chunks + recording.mp4 + metadata.json). They serialize on a
per-(exam, student) lock so a merge never races a concurrent upload, while
different students still proceed fully in parallel.

Kept in its own module so `main.py` and `worker.py` share one lock registry
without importing each other.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_locks: defaultdict[tuple[str, str], threading.Lock] = defaultdict(
    threading.Lock)
_registry_guard = threading.Lock()


def session_lock(exam_id: str, student_id: str) -> threading.Lock:
    # Guard the defaultdict itself so two threads creating the first lock for a
    # new session can't each make a different Lock object.
    with _registry_guard:
        return _locks[(exam_id, student_id)]
