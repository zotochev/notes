"""Building the working parts from configuration. The only place that knows every concrete class."""
from __future__ import annotations

import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import urllib3

from notificator.calendars.google_auth import GoogleAuth
from notificator.config import (
    Config, LocalSourceConfig, SeafileSourceConfig, SourceConfig, WebDavSourceConfig,
)
from notificator.sources.local import LocalSource
from notificator.sources.seafile import SeafileSource
from notificator.sources.webdav import WebDavSource
from notificator.sync.engine import SyncSettings
from notificator.sync.ports import Source

logger = logging.getLogger(__name__)


def build_source(cfg: SourceConfig) -> Source:
    if getattr(cfg, "verify_ssl", True) is False:
        # Say it once, clearly, instead of urllib3 saying it on every request.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning(
            "Проверка SSL-сертификата для %s отключена (verify_ssl: false): соединение не защищено от подмены", cfg.url
        )
    match cfg:
        case WebDavSourceConfig():
            return WebDavSource(
                cfg.url, cfg.username, lambda: cfg.password,
                watch_paths=cfg.watch_paths, verify_ssl=cfg.verify_ssl, depth_infinity=cfg.depth_infinity,
            )
        case SeafileSourceConfig():
            return SeafileSource(cfg.url, cfg.username, cfg.password, cfg.watch_paths, verify_ssl=cfg.verify_ssl)
        case LocalSourceConfig():
            return LocalSource(cfg.roots)


def sync_settings(config: Config) -> SyncSettings:
    return SyncSettings(
        default_calendar=config.default_calendar,
        default_tz=ZoneInfo(config.time_zone),
        extensions=frozenset(config.extensions),
        max_delete_ratio=config.max_delete_ratio,
        held_deletes_min=config.held_deletes_min,
        read_concurrency=config.read_concurrency,
    )


def google_auth(config: Config, data_dir: Path) -> GoogleAuth:
    return GoogleAuth(
        data_dir / config.google.credentials_file,
        data_dir / config.google.token_file,
        config.google.redirect_uri,
    )
