import json

import pytest

from notificator.config import ConfigError, load_config
from notificator.sources.local import LocalSource
from notificator.sources.seafile import SeafileSource
from notificator.sources.webdav import WebDavSource
from notificator.wiring import build_source

VALID = {
    "active_source": "cloud",
    "sources": {
        "cloud": {"type": "webdav", "url": "https://c.example/dav", "username": "u", "password": "p"},
        "sea": {"type": "seafile", "url": "https://s.example", "username": "u", "password": "p", "watch_paths": ["/lib"]},
        "disk": {"type": "local", "roots": ["."]},
    },
}


def write(tmp_path, config: dict):
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def test_minimal_config_gets_defaults(tmp_path):
    config = load_config(write(tmp_path, VALID))

    assert config.default_calendar == "primary"
    assert config.extensions == [".csv", ".md", ".txt"]
    assert config.sources["cloud"].watch_paths == ["/"]


def test_each_source_type_builds_its_source(tmp_path):
    config = load_config(write(tmp_path, VALID))

    built = {name: type(build_source(cfg)) for name, cfg in config.sources.items()}

    assert built == {"cloud": WebDavSource, "sea": SeafileSource, "disk": LocalSource}


def test_extensions_are_normalized(tmp_path):
    config = load_config(write(tmp_path, {**VALID, "extensions": ["MD", ".Txt", " ", ".md"]}))

    assert config.extensions == [".md", ".txt"]


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"active_source": "nope"}, "active_source"),
        ({"time_zone": "Mars/Olympus"}, "часовой пояс"),
        ({"scan_intervall_sec": 10}, "scan_intervall_sec"),
        ({"sources": {"cloud": {"type": "ftp"}}}, "sources.cloud"),
        ({"sources": {"cloud": {"type": "webdav", "url": "x", "username": "u"}}}, "password"),
        ({"sources": {"cloud": {"type": "seafile", "url": "x", "username": "u", "password": "p", "watch_paths": []}}}, "watch_paths"),
    ],
)
def test_invalid_config_is_rejected_with_the_field_name(tmp_path, change, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(write(tmp_path, {**VALID, **change}))


def test_missing_or_broken_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="нет файла"):
        load_config(tmp_path)

    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="не удалось прочитать"):
        load_config(tmp_path)
