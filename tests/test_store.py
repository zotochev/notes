import threading

from notificator.core.model import EventKey
from notificator.store import Store


def test_readers_do_not_block_a_writer_in_another_thread(tmp_path):
    path = tmp_path / "state.db"
    Store(path).close()
    errors: list[Exception] = []
    stop = threading.Event()

    def write() -> None:
        try:
            with Store(path) as store:
                for i in range(300):
                    store.log(EventKey("s", "/a.md", "u1"), "created", str(i))
        except Exception as e:
            errors.append(e)
        finally:
            stop.set()

    writer = threading.Thread(target=write)
    writer.start()
    reads = 0
    while not stop.is_set():
        with Store(path) as store:
            store.journal(5)
        reads += 1
    writer.join()

    assert errors == []
    assert reads > 0
    with Store(path) as store:
        assert len(store.journal(1000)) == 300


def test_many_threads_can_open_a_new_database_at_once(tmp_path):
    path = tmp_path / "state.db"
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def open_it() -> None:
        start.wait()
        try:
            with Store(path) as store:
                store.journal(1)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_existing_database_is_opened_without_recreating_it(tmp_path):
    path = tmp_path / "state.db"
    with Store(path) as store:
        store.set_file_version("s", "/a.md", "v1")

    with Store(path) as store:
        assert store.file_versions("s") == {"/a.md": "v1"}
