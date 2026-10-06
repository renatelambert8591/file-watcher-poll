"""File Watcher Poll: poll filesystem paths for change events.

Public API:
    Watcher        -- the poller itself
    FileEvent      -- a record of a single change (type, path, old_sig, new_sig)
    FileSignature  -- the stat-based fingerprint captured per path
    EventKind      -- enum of event types
"""

from .core import (
    EventKind,
    FileEvent,
    FileSignature,
    Watcher,
)

__all__ = [
    "EventKind",
    "FileEvent",
    "FileSignature",
    "Watcher",
]
