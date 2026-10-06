"""Core polling implementation for file_watcher_poll.

The watcher fingerprints every tracked path with a cheap stat-based signature
and reports differences as events. On the first poll every existing path is
treated as *created* so callers receive a full snapshot without a priming
step; a path present on the first poll that vanishes later is reported as
*deleted*.
"""

from __future__ import annotations

import enum
import os
import stat as stat_mod
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional


class EventKind(enum.Enum):
    """The kind of change observed for a single path.

    We use an explicit enum rather than strings so that callers can pattern
    match exhaustively; typos surface as ``NameError`` at import time instead
    of as silently failing ``if`` branches at runtime.
    """

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileSignature:
    """A minimal, cheap fingerprint of a path.

    We deliberately track only the inode, the size, and the mtime returned by
    the injected clock. ``st_ino`` lets us notice a replace-by-rename (inode
    changes even if mtime and size happen to coincide); ``st_size`` catches
    truncations that might share an mtime second; ``st_mtime`` catches
    in-place edits. We do *not* track ``st_ctime`` because its meaning varies
    by platform and adds noise without adding signal for the common case.
    """

    inode: int
    size: int
    mtime: int
    is_dir: bool

    @classmethod
    def from_stat(cls, st: os.stat_result) -> "FileSignature":
        return cls(
            inode=int(st.st_ino),
            size=int(st.st_size),
            mtime=int(st.st_mtime),
            is_dir=bool(stat_mod.S_ISDIR(st.st_mode)),
        )


@dataclass(frozen=True)
class FileEvent:
    """A single observed change.

    ``old_sig`` is ``None`` for creates (nothing existed before); ``new_sig``
    is ``None`` for deletions (nothing exists now). Both are populated for
    modifications. This keeps the data model honest: a missing signature is
    represented explicitly rather than as a zero-value placeholder.
    """

    kind: EventKind
    path: str
    old_sig: Optional[FileSignature]
    new_sig: Optional[FileSignature]


StatFn = Callable[[str], os.stat_result]


def _default_stat(path: str) -> os.stat_result:
    # Follow symlinks on purpose: callers asking to watch a path usually care
    # about the *target*, not the link itself. A broken link is surfaced as
    # deleted on the next poll.
    return os.stat(path, follow_symlinks=True)


@dataclass
class Watcher:
    """Polls a set of paths and yields change events.

    The watcher is stateful: it remembers the last signature seen for each
    path so that consecutive ``poll()`` calls report only the deltas. Paths
    are added via ``add``; the first ``poll`` after a path is added reports a
    ``CREATED`` event for it (unless it does not exist, in which case nothing
    is reported until it appears).

    A custom ``stat_fn`` and ``clock`` may be injected. We use ``clock`` for
    the mtime field rather than reading ``st.st_mtime`` directly so that tests
    can drive event ordering deterministically — filesystem mtime resolution
    is unreliable and the test suite must not depend on it.
    """

    stat_fn: StatFn = field(default=_default_stat)
    clock: Callable[[], int] = field(default=lambda: 0)
    _signatures: Dict[str, FileSignature] = field(default_factory=dict, init=False, repr=False)
    _known: set = field(default_factory=set, init=False, repr=False)

    def add(self, path: str) -> None:
        """Begin tracking ``path``.

        Idempotent: adding a path already tracked is a no-op. The path need
        not exist yet; once it appears it will be reported as created.
        """
        self._known.add(path)

    def remove(self, path: str) -> None:
        """Stop tracking ``path`` and drop its stored signature."""
        self._known.discard(path)
        self._signatures.pop(path, None)

    def paths(self) -> List[str]:
        """Return a sorted snapshot of the tracked paths."""
        return sorted(self._known)

    def poll(self) -> List[FileEvent]:
        """Take one poll and return the events observed in order.

        Events are ordered by path (deterministic) so that tests and callers
        can rely on a stable sequence regardless of dict iteration order.
        """
        events: List[FileEvent] = []
        now = self.clock()
        for path in sorted(self._known):
            try:
                st = self.stat_fn(path)
            except OSError:
                # Path is missing (never existed, was deleted, or permissions
                # changed so we can no longer stat it). If we previously had a
                # signature, report deletion and drop it; otherwise stay quiet
                # so the first poll of a not-yet-existing path emits nothing.
                old = self._signatures.get(path)
                if old is not None:
                    events.append(
                        FileEvent(
                            kind=EventKind.DELETED,
                            path=path,
                            old_sig=old,
                            new_sig=None,
                        )
                    )
                    del self._signatures[path]
                continue

            new_sig = FileSignature(
                inode=int(st.st_ino),
                size=int(st.st_size),
                mtime=now,
                is_dir=bool(stat_mod.S_ISDIR(st.st_mode)),
            )
            old = self._signatures.get(path)
            if old is None:
                events.append(
                    FileEvent(
                        kind=EventKind.CREATED,
                        path=path,
                        old_sig=None,
                        new_sig=new_sig,
                    )
                )
            elif old != new_sig:
                events.append(
                    FileEvent(
                        kind=EventKind.MODIFIED,
                        path=path,
                        old_sig=old,
                        new_sig=new_sig,
                    )
                )
            self._signatures[path] = new_sig
        return events
