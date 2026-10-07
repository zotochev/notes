"""
Configuration: one JSON file, `config.json` in the data directory.

Everything else the service keeps (state database, Google token) lives in the
same directory, and relative file names in the config are resolved against it.
Unknown keys are rejected so that a typo cannot silently do nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONFIG_FILE = "config.json"
STATE_FILE = "state.db"


class ConfigError(Exception):
    """The configuration file is missing or invalid; the message is for the user."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebDavSourceConfig(_Strict):
    type: Literal["webdav"]
    url: str
    username: str
    password: str
    watch_paths: list[str] = ["/"]
    verify_ssl: bool = True
    # Turn off for servers that reject PROPFIND Depth: infinity without saying so clearly.
    depth_infinity: bool = True


class SeafileSourceConfig(_Strict):
    type: Literal["seafile"]
    url: str
    username: str
    password: str
    # Each path starts with a library name: "/mylib" or "/mylib/notes".
    watch_paths: list[str] = Field(min_length=1)
    verify_ssl: bool = True


class LocalSourceConfig(_Strict):
    type: Literal["local"]
    roots: list[Path] = Field(min_length=1)


SourceConfig = Annotated[
    WebDavSourceConfig | SeafileSourceConfig | LocalSourceConfig, Field(discriminator="type")
]


class GoogleConfig(_Strict):
    credentials_file: str = "google_credentials.json"
    token_file: str = "google_token.json"
    redirect_uri: str | None = None


class Config(_Strict):
    active_source: str
    sources: dict[str, SourceConfig]
    default_calendar: str = "primary"
    time_zone: str = "Europe/Ulyanovsk"
    extensions: list[str] = [".csv", ".md", ".txt"]
    scan_interval_sec: int = Field(default=30, ge=5)
    # Deletions wait for approval when a cycle wants to delete at least
    # `held_deletes_min` events and more than `max_delete_ratio` of all tracked ones.
    max_delete_ratio: float = Field(default=0.2, ge=0, le=1)
    held_deletes_min: int = Field(default=5, ge=1)
    google: GoogleConfig = GoogleConfig()
    host: str = "127.0.0.1"
    port: int = 40000

    @field_validator("time_zone")
    @classmethod
    def _known_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError):
            raise ValueError(f"неизвестный часовой пояс {value!r}") from None
        return value

    @field_validator("extensions")
    @classmethod
    def _normalized_extensions(cls, value: list[str]) -> list[str]:
        cleaned = {("." + e.strip().lstrip(".")).lower() for e in value if e.strip().strip(".")}
        return sorted(cleaned)

    @model_validator(mode="after")
    def _active_source_exists(self) -> Config:
        if self.active_source not in self.sources:
            raise ValueError(
                f"active_source {self.active_source!r} не описан в sources (есть: {sorted(self.sources)})"
            )
        return self


def load_config(data_dir: Path) -> Config:
    path = data_dir / CONFIG_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"нет файла настроек {path}") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"не удалось прочитать {path}: {e}") from e
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'config'}: {err['msg']}" for err in e.errors()
        )
        raise ConfigError(f"ошибка в {path}: {problems}") from e
