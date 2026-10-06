"""Tests for file_watcher_poll.core.

We never touch the real filesystem or wall clock. A fake stat function lets us
script exactly what each path returns per poll, and an integer clock makes
mtime ordering fully deterministic.
"""

import os
import unittest
from dataclasses import replace

from file_watcher_poll.core import (
    EventKind,
    FileEvent,
    FileSignature,
    Watcher,
)


def make_stat(*, inode=1, size=0, mtime=0, mode=0o100644):
    """Build an ``os.stat_result``-like object suitable for the watcher.

    ``os.stat_result`` accepts exactly the fields it documents plus an unused
    pair at the end; the real object works fine for our purposes since the
    watcher only reads ``st_ino``, ``st_size``, ``st_mtime`` and ``st_mode``.
    """
    return os.stat_result((mode, inode, 0, 1, 0, 0, size, mtime, 0, 0))


class FakeFS:
    """A tiny in-memory filesystem for deterministic stat responses.

    Each path maps to an ``os.stat_result``. Deleting a key simulates an
    absent path; the watcher will catch ``KeyError`` via ``OSError``.
    """

    def __init__(self, initial=None):
        self._entries = dict(initial or {})

    def stat(self, path):
        if path not in self._entries:
            raise FileNotFoundError(path)
        return self._entries[path]

    def set(self, path, **kwargs):
        prev = self._entries.get(path)
        defaults = {}
        if prev is not None:
            defaults = {
                "inode": prev.st_ino,
                "size": prev.st_size,
                "mtime": prev.st_mtime,
                "mode": prev.st_mode,
            }
        defaults.update(kwargs)
        self._entries[path] = make_stat(**defaults)

    def remove(self, path):
        self._entries.pop(path, None)


class Clock:
    """Integer clock so tests never compare floats or sleep."""

    def __init__(self, start=0):
        self.t = start

    def __call__(self):
        return self.t

    def tick(self):
        self.t += 1
        return self.t


class FirstPollCreatedTests(unittest.TestCase):
    def test_existing_paths_reported_as_created_once(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10), "/b": make_stat(inode=2, size=20)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.add("/b")
        events = w.poll()
        kinds = [(e.kind, e.path) for e in events]
        self.assertEqual(
            kinds,
            [(EventKind.CREATED, "/a"), (EventKind.CREATED, "/b")],
        )
        self.assertEqual(w.poll(), [])

    def test_nonexistent_path_silent_then_created_on_appearance(self):
        fs = FakeFS()
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/ghost")
        self.assertEqual(w.poll(), [])
        fs.set("/ghost", inode=7, size=3)
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.CREATED)
        self.assertEqual(events[0].path, "/ghost")
        self.assertIsNone(events[0].old_sig)
        self.assertEqual(events[0].new_sig.inode, 7)


class ModificationTests(unittest.TestCase):
    def test_size_change_is_modification(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        fs.set("/a", size=11)
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.MODIFIED)
        self.assertEqual(events[0].old_sig.size, 10)
        self.assertEqual(events[0].new_sig.size, 11)

    def test_inode_change_is_modification_even_without_size_change(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        fs.set("/a", inode=2, size=10)
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.MODIFIED)

    def test_clock_drives_mtime_field(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        clock = Clock()
        w = Watcher(stat_fn=fs.stat, clock=clock)
        w.add("/a")
        w.poll()
        # With a stable clock the signature is stable across polls: no event.
        self.assertEqual(w.poll(), [])
        # Advancing the clock changes the mtime field of the signature, so the
        # next poll reports a modification driven purely by the clock.
        clock.tick()
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.MODIFIED)
        self.assertEqual(events[0].old_sig.mtime, 0)
        self.assertEqual(events[0].new_sig.mtime, 1)

    def test_clock_advance_with_change_reports_new_mtime(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        clock = Clock()
        w = Watcher(stat_fn=fs.stat, clock=clock)
        w.add("/a")
        w.poll()
        clock.tick()
        fs.set("/a", size=11)
        events = w.poll()
        self.assertEqual(events[0].new_sig.mtime, 1)
        self.assertEqual(events[0].old_sig.mtime, 0)


class DeletionTests(unittest.TestCase):
    def test_deletion_after_known_is_reported(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        fs.remove("/a")
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.DELETED)
        self.assertIsNone(events[0].new_sig)
        self.assertEqual(events[0].old_sig.inode, 1)

    def test_recreation_after_deletion_is_created(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        fs.remove("/a")
        w.poll()
        fs.set("/a", inode=2, size=20)
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.CREATED)

    def test_oserror_other_than_missing_treated_as_deletion(self):
        # A permission error means we can no longer fingerprint the path; the
        # watcher treats this as "no longer observable" and reports deletion.
        class PermissionFS:
            def __init__(self, inner):
                self.inner = inner
                self.locked = False

            def stat(self, path):
                if path in self.inner._entries:
                    if self.locked:
                        raise PermissionError(path)
                    return self.inner._entries[path]
                raise FileNotFoundError(path)

        fs = FakeFS({"/a": make_stat(inode=1, size=10)})
        pfs = PermissionFS(fs)
        w = Watcher(stat_fn=pfs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        pfs.locked = True
        events = w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.DELETED)


class OrderingAndStabilityTests(unittest.TestCase):
    def test_events_sorted_by_path(self):
        fs = FakeFS({
            "/zeta": make_stat(inode=1, size=1),
            "/alpha": make_stat(inode=2, size=2),
            "/mid": make_stat(inode=3, size=3),
        })
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        for p in ("/zeta", "/mid", "/alpha"):
            w.add(p)
        events = w.poll()
        self.assertEqual([e.path for e in events], ["/alpha", "/mid", "/zeta"])

    def test_add_idempotent(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=1)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.add("/a")
        self.assertEqual(w.paths(), ["/a"])
        events = w.poll()
        self.assertEqual(len(events), 1)

    def test_remove_drops_signature_silently(self):
        fs = FakeFS({"/a": make_stat(inode=1, size=1)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/a")
        w.poll()
        w.remove("/a")
        self.assertEqual(w.poll(), [])
        self.assertEqual(w.paths(), [])

    def test_remove_unknown_is_noop(self):
        w = Watcher()
        w.remove("/never-added")
        self.assertEqual(w.paths(), [])


class SignatureTests(unittest.TestCase):
    def test_directory_flag_propagates(self):
        fs = FakeFS({"/d": make_stat(inode=1, size=0, mode=0o040755)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/d")
        events = w.poll()
        self.assertTrue(events[0].new_sig.is_dir)

    def test_file_signature_not_dir(self):
        fs = FakeFS({"/f": make_stat(inode=1, size=0, mode=0o100644)})
        w = Watcher(stat_fn=fs.stat, clock=Clock())
        w.add("/f")
        events = w.poll()
        self.assertFalse(events[0].new_sig.is_dir)

    def test_file_event_is_frozen(self):
        ev = FileEvent(
            kind=EventKind.CREATED,
            path="/a",
            old_sig=None,
            new_sig=FileSignature(inode=1, size=1, mtime=0, is_dir=False),
        )
        with self.assertRaises(Exception):
            ev.path = "/b"  # type: ignore[misc]

    def test_file_signature_is_value_equal(self):
        a = FileSignature(inode=1, size=2, mtime=3, is_dir=False)
        b = FileSignature(inode=1, size=2, mtime=3, is_dir=False)
        self.assertEqual(a, b)
        self.assertNotEqual(a, replace(a, size=3))


if __name__ == "__main__":
    unittest.main()
