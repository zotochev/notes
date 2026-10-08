"""
Listing a big tree in small batches, so that no single request overloads the server.

A batch is one request for one level of one folder. Its cost is bounded by
the folder's direct children, however deep or large the subtree is; requests
for a whole subtree are never made.

To avoid walking everything every time, a server may give each folder a
marker that changes whenever anything under the folder changes. A folder
whose marker is the one seen last time is not asked again: its level is taken
from memory, and so are its subfolders while their markers match too. The
decision uses only what the server has just said about the folder, never a
guess about its size, so folders that appear, vanish, grow or shrink are
handled the same way: the marker differs or is unknown, and the level is read.

The memory is only a shortcut. It is empty after a restart and is dropped
once a day, and then everything is read again, one level at a time.

How many requests run at once follows how fast the server answers: slow
answers lower the number and add pauses, fast ones raise it again.

A listing is complete or it raises: a level that cannot be listed fails the
whole listing, because a partial one would look like deleted files.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from notificator.core.model import RemoteFile

logger = logging.getLogger(__name__)

# An answer slower than this means the server is struggling: fewer requests at once, then pauses.
_SLOW_SEC = 5.0
# This many answers in a row faster than _FAST_SEC allow one more request at once.
_FAST_SEC = 1.5
_FAST_STREAK = 10
_MAX_PAUSE_SEC = 30.0
# How often a long listing reports how far it has got.
_LOG_INTERVAL_SEC = 5
_FULL_WALK_EVERY_SEC = 24 * 3600


@dataclass(frozen=True, slots=True)
class Folder:
    path: str
    # Changes whenever anything under the folder changes. None when the server gives no such promise.
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class Level:
    """What one folder holds directly."""
    folders: list[Folder]
    files: list[RemoteFile]


@dataclass(slots=True)
class _Known:
    """A level as it was when its folder had this marker."""
    marker: str
    level: Level


@dataclass(slots=True)
class _Group:
    """Progress of one folder directly under a watched root, or of the root's own level."""
    folder: str
    # Requests waiting or running.
    pending: int = 0
    started: bool = False
    read: int = 0
    unchanged: int = 0
    files: int = 0

    @property
    def state(self) -> str:
        if self.pending:
            return "running" if self.started else "queued"
        return "done" if self.read else "unchanged"


class TreeWalker:
    def __init__(
        self,
        list_level: Callable[[str], Level],
        max_concurrency: int = 4,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`list_level` returns one level of a folder or raises SourceError."""
        self._list_level = list_level
        self._max_concurrency = max_concurrency
        self._clock = clock
        self._sleep = sleep
        # Requests allowed at once right now: starts careful and follows the server's speed.
        self.concurrency = 1
        self._fast_streak = 0
        self._pause = 0.0
        self._known: dict[str, _Known] = {}
        self._known_since = clock()
        # Guards what snapshot() reads while a listing runs in another thread.
        self._lock = threading.Lock()
        self._groups: list[_Group] = []
        self._current: list[str] = []
        self._active = False

    def run(self, roots: list[str]) -> list[RemoteFile]:
        """List everything under the roots. Raises SourceError unless the listing is complete."""
        if self._clock() - self._known_since >= _FULL_WALK_EVERY_SEC:
            self.forget()
        roots = list(dict.fromkeys(roots))
        files: dict[str, RemoteFile] = {}
        seen: set[str] = set()
        queue: deque[tuple[Folder, _Group]] = deque()
        running: dict[Future, tuple[Folder, _Group, float]] = {}
        groups: list[_Group] = []
        with self._lock:
            self._groups = groups
            self._active = True
        last_log = self._clock()

        def new_group(folder: str) -> _Group:
            group = _Group(folder)
            with self._lock:
                groups.append(group)
            return group

        def take(level: Level, group: _Group, is_root: bool) -> list[tuple[Folder, _Group]]:
            """Count a level's files and return its subfolders with the group each one reports to."""
            group.files += len(level.files)
            files.update((f.path, f) for f in level.files)
            # Each folder directly under a root is shown on its own.
            return [(sub, new_group(sub.path) if is_root else group) for sub in level.folders]

        def visit(todo: list[tuple[Folder, _Group]]) -> None:
            """Take subtrees from memory while their markers still match, queue requests for the rest."""
            while todo:
                folder, group = todo.pop()
                seen.add(folder.path)
                known = self._known.get(folder.path)
                if known is None or folder.marker is None or known.marker != folder.marker:
                    group.pending += 1
                    queue.append((folder, group))
                else:
                    group.unchanged += 1
                    todo.extend(take(known.level, group, is_root=False))

        pool = ThreadPoolExecutor(max_workers=self._max_concurrency)
        try:
            # A root has no marker of its own: its level is always read.
            visit([(Folder(root), new_group(root)) for root in roots])
            while queue or running:
                while queue and len(running) < self.concurrency:
                    folder, group = queue.popleft()
                    group.started = True
                    running[pool.submit(self._list_level, folder.path)] = (folder, group, self._clock())
                with self._lock:
                    self._current = [folder.path for folder, _, _ in running.values()]
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    folder, group, started = running.pop(future)
                    level = future.result()
                    self._pace(self._clock() - started)
                    if folder.marker is not None:
                        self._known[folder.path] = _Known(folder.marker, level)
                    group.pending -= 1
                    group.read += 1
                    visit(take(level, group, is_root=folder.marker is None and folder.path in roots))
                if self._pause and queue:
                    self._sleep(self._pause)
                if self._clock() - last_log >= _LOG_INTERVAL_SEC:
                    last_log = self._clock()
                    logger.info(
                        "Листинг: папок прочитано %d, без изменений %d, в очереди %d, файлов %d, "
                        "запросов одновременно %d",
                        sum(g.read for g in groups), sum(g.unchanged for g in groups),
                        len(queue) + len(running), len(files), self.concurrency,
                    )
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._current = []
                self._active = False
        # A folder that is gone must not come back from memory when a folder of the same name appears.
        for path in self._known.keys() - seen:
            del self._known[path]
        return list(files.values())

    def forget(self) -> None:
        """Drop everything remembered, so that the next listing reads every level."""
        self._known.clear()
        self._known_since = self._clock()

    def snapshot(self, max_groups: int = 500) -> dict[str, Any]:
        """Progress of the running listing, or the outcome of the last one, for display."""
        with self._lock:
            groups = list(self._groups)
            current = list(self._current)
            active = self._active
        order = {"running": 0, "queued": 1, "done": 2, "unchanged": 3}
        return {
            "active": active,
            "concurrency": self.concurrency,
            "maxConcurrency": self._max_concurrency,
            "pauseSec": round(self._pause, 1),
            "read": sum(g.read for g in groups),
            "unchanged": sum(g.unchanged for g in groups),
            "waiting": sum(g.pending for g in groups),
            "files": sum(g.files for g in groups),
            "current": current,
            "groups": [
                {"folder": g.folder, "state": g.state, "read": g.read, "unchanged": g.unchanged,
                 "waiting": g.pending, "files": g.files}
                for g in sorted(groups, key=lambda g: (order[g.state], g.folder))[:max_groups]
            ],
        }

    def _pace(self, seconds: float) -> None:
        """Adjust how hard the server is pushed to how fast it has just answered."""
        if seconds > _SLOW_SEC:
            self._fast_streak = 0
            if self.concurrency > 1:
                self.concurrency = max(1, self.concurrency // 2)
            else:
                # Already one at a time: rest for as long as the answer took.
                self._pause = min(seconds, _MAX_PAUSE_SEC)
        elif seconds < _FAST_SEC:
            self._fast_streak += 1
            if self._fast_streak >= _FAST_STREAK:
                self._fast_streak = 0
                if self._pause:
                    self._pause = 0.0
                else:
                    self.concurrency = min(self._max_concurrency, self.concurrency + 1)
        else:
            self._fast_streak = 0
