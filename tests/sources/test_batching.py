from hashlib import sha1

import pytest

from notificator.core.model import RemoteFile
from notificator.sources.batching import Folder, Level, TreeWalker
from notificator.sync.ports import SourceError


class Tree:
    """A file tree served one level at a time, with a clock that requests advance."""

    def __init__(self, files: dict[str, str] | list[str], markers: bool = True) -> None:
        self.files = dict(files) if isinstance(files, dict) else dict.fromkeys(files, "v1")
        self.markers = markers
        self.requests: list[str] = []
        self.now = 0.0
        self.seconds_per_request = 0.0
        self.slept: list[float] = []
        self.fail: set[str] = set()

    def walker(self, **kwargs) -> TreeWalker:
        return TreeWalker(self.level, clock=lambda: self.now, sleep=self.slept.append, **kwargs)

    def level(self, folder: str) -> Level:
        self.requests.append(folder)
        self.now += self.seconds_per_request
        if folder in self.fail:
            raise SourceError(f"cannot list {folder}")
        base = folder.rstrip("/") + "/"
        inside = {p[len(base):]: version for p, version in self.files.items() if p.startswith(base)}
        folders = sorted({base + rest.split("/")[0] for rest in inside if "/" in rest})
        return Level(
            folders=[Folder(f, self.marker(f) if self.markers else None) for f in folders],
            files=[RemoteFile(base + rest, version) for rest, version in inside.items() if "/" not in rest],
        )

    def marker(self, folder: str) -> str:
        """A hash of everything under the folder."""
        inside = sorted((p, v) for p, v in self.files.items() if p.startswith(folder + "/"))
        return sha1(repr(inside).encode()).hexdigest()

    def asked(self) -> list[str]:
        asked, self.requests = sorted(self.requests), []
        return asked


def listed(files: list[RemoteFile]) -> dict[str, str]:
    return {f.path: f.version for f in files}


FILES = ["/a.md", "/notes/b.md", "/notes/deep/c.md", "/photos/1.jpg", "/photos/2020/2.jpg"]


def test_every_level_is_read_one_folder_per_request():
    tree = Tree(FILES)

    files = tree.walker().run(["/"])

    assert listed(files) == tree.files
    assert tree.asked() == ["/", "/notes", "/notes/deep", "/photos", "/photos/2020"]


def test_unchanged_tree_costs_one_request_per_root():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])
    tree.asked()

    assert listed(walker.run(["/"])) == tree.files
    assert tree.asked() == ["/"]


def test_only_the_path_to_a_change_is_read_again():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])
    tree.files["/notes/deep/c.md"] = "v2"
    tree.asked()

    assert listed(walker.run(["/"])) == tree.files
    assert tree.asked() == ["/", "/notes", "/notes/deep"]


def test_folders_that_appear_vanish_grow_and_shrink_are_followed():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])

    del tree.files["/notes/b.md"], tree.files["/notes/deep/c.md"]
    tree.files.update({f"/photos/2020/{i}.jpg": "v1" for i in range(50)})
    tree.files["/new/deep/x.md"] = "v1"
    assert listed(walker.run(["/"])) == tree.files

    del tree.files["/new/deep/x.md"]
    for i in range(50):
        del tree.files[f"/photos/2020/{i}.jpg"]
    tree.files["/notes/b.md"] = "v1"
    assert listed(walker.run(["/"])) == tree.files


def test_folder_that_comes_back_under_the_same_name_is_read_afresh():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])
    removed = {p: tree.files.pop(p) for p in ["/notes/b.md", "/notes/deep/c.md"]}
    walker.run(["/"])
    tree.files.update(removed)
    tree.asked()

    assert listed(walker.run(["/"])) == tree.files
    assert tree.asked() == ["/", "/notes", "/notes/deep"]


def test_without_markers_every_level_is_read_every_time():
    tree = Tree(FILES, markers=False)
    walker = tree.walker()
    walker.run(["/"])
    tree.asked()

    assert listed(walker.run(["/"])) == tree.files
    assert len(tree.asked()) == 5


def test_everything_is_read_again_once_a_day():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])
    tree.now += 24 * 3600
    tree.asked()

    walker.run(["/"])
    assert len(tree.asked()) == 5

    walker.run(["/"])
    assert tree.asked() == ["/"]


def test_level_that_cannot_be_listed_fails_the_whole_listing_and_is_asked_again_next_time():
    tree = Tree(FILES)
    walker = tree.walker()
    tree.fail = {"/notes/deep"}

    with pytest.raises(SourceError, match="/notes/deep"):
        walker.run(["/"])
    assert not walker.snapshot()["active"]

    tree.fail = set()
    tree.asked()
    assert listed(walker.run(["/"])) == tree.files
    # What was read before the failure is not asked again.
    assert tree.asked() == ["/", "/notes/deep"]


def test_slow_answers_lower_the_load_and_fast_ones_raise_it_again():
    tree = Tree([f"/d{i}/f.md" for i in range(60)], markers=False)
    walker = tree.walker(max_concurrency=4)

    tree.seconds_per_request = 0.1
    walker.run(["/"])
    assert walker.concurrency == 4

    tree.seconds_per_request = 6.0
    walker.run(["/"])
    assert walker.concurrency == 1
    # The pause is as long as the slow answer took.
    assert tree.slept and tree.slept[-1] == 6.0

    tree.slept.clear()
    tree.seconds_per_request = 0.1
    walker.run(["/"])
    assert walker.concurrency > 1
    assert walker.snapshot()["pauseSec"] == 0


def test_listing_starts_with_one_request_at_a_time():
    assert Tree(FILES).walker().concurrency == 1


def test_snapshot_shows_each_top_folder_and_what_was_read_or_left_alone():
    tree = Tree(FILES)
    walker = tree.walker()
    walker.run(["/"])
    tree.files["/notes/deep/c.md"] = "v2"

    walker.run(["/"])
    snapshot = walker.snapshot()

    assert (snapshot["read"], snapshot["unchanged"], snapshot["waiting"], snapshot["files"]) == (3, 2, 0, 5)
    assert snapshot["groups"] == [
        {"folder": "/", "state": "done", "read": 1, "unchanged": 0, "waiting": 0, "files": 1},
        {"folder": "/notes", "state": "done", "read": 2, "unchanged": 0, "waiting": 0, "files": 2},
        {"folder": "/photos", "state": "unchanged", "read": 0, "unchanged": 2, "waiting": 0, "files": 2},
    ]


def test_several_roots_and_several_requests_at_once_give_the_same_listing():
    tree = Tree(FILES)
    walker = tree.walker(max_concurrency=4)
    walker.concurrency = 4

    assert listed(walker.run(["/notes", "/photos", "/notes"])) == {
        p: v for p, v in tree.files.items() if p != "/a.md"
    }
