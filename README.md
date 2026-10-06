# file_watcher_poll

Polls a set of filesystem paths for creation, modification, and deletion events using cheap `stat`-based signatures. Standard library only, no threads, no callbacks — call `poll()` and read the returned list.

## Usage

```python
from file_watcher_poll import Watcher, EventKind

w = Watcher()
w.add("/etc/hosts")
w.add("/var/log/system.log")

for event in w.poll():
    print(event.kind.value, event.path)
```

`poll()` returns a list of `FileEvent` objects. Each carries `kind` (an `EventKind` enum: `CREATED`, `MODIFIED`, `DELETED`), `path`, `old_sig`, and `new_sig`. Signatures are `FileSignature(inode, size, mtime, is_dir)` and are `None` on the side of the event that has no file (old for creates, new for deletions).

## Why this exists

`inotify` and `FSEvents` are fast but fiddly: platform-specific, resource-limited, and awkward to test. For daemons that already wake on a schedule, a polling watcher is simpler to reason about and trivially portable. The trade-off is latency proportional to your poll interval and one `stat` per tracked path per poll — acceptable for dozens or hundreds of paths, not for millions.

The signature is `inode + size + mtime + is_dir`. `inode` catches replace-by-rename even when size and mtime happen to match; `size` catches truncations within the same mtime second; `mtime` catches in-place edits. `ctime` is deliberately omitted because its meaning varies by platform and adds noise.

## The awkward edge

The first `poll()` after adding a path reports `CREATED` if the path exists, and reports *nothing* if it does not — there is no separate "priming" call. This means a freshly constructed watcher with N existing paths yields N `CREATED` events on the first poll. If you only care about deltas from now, drop the first `poll()`'s output. A path that becomes un-stattable for any reason (deletion, revoked permissions, broken symlink) is reported as `DELETED`; if you need to distinguish "gone" from "unreadable", this library is not for you.

`mtime` is driven by an injected `clock` function rather than `st_mtime` directly, so tests are deterministic and so that you can decouple observed "time" from filesystem mtime resolution if you wish. The default clock returns `0`; supply your own monotonic source in production.
