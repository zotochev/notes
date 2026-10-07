"""Command line: `python -m notificator <command>`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from notificator.config import ConfigError, load_config
from notificator.preview import preview_cycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notificator")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="каталог с config.json и состоянием")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="запустить синхронизацию и админку")
    plan = commands.add_parser("plan", help="показать, что сделал бы цикл синхронизации, ничего не меняя")
    plan.add_argument("--source", help="имя источника из config.json (по умолчанию активный)")
    old = commands.add_parser("import-old-state", help="перенести связи событий из state.json старой версии")
    old.add_argument("state_file", type=Path, help="путь к старому state.json")
    old.add_argument("--source", help="к какому источнику отнести события (по умолчанию активный)")
    cleanup = commands.add_parser(
        "cleanup", help="найти в календарях события notificator, которых нет в текущем состоянии"
    )
    cleanup.add_argument("--delete", action="store_true", help="удалить найденные события, а не только показать")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.data_dir)
        if args.command == "plan":
            return _plan(config, args.data_dir, args.source)
        if args.command == "run":
            return _run(config, args.data_dir)
        if args.command == "cleanup":
            return _cleanup(config, args.data_dir, args.delete)
        if args.command == "import-old-state":
            return _import_old_state(config, args.data_dir, args.state_file, args.source)
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2
    return 0


def _run(config, data_dir: Path) -> int:
    import uvicorn

    from notificator.service import SyncService
    from notificator.web.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = SyncService(config, data_dir)
    uvicorn.run(create_app(service, config, data_dir), host=config.host, port=config.port)
    return 0


def _import_old_state(config, data_dir: Path, state_file: Path, source_name: str | None) -> int:
    from zoneinfo import ZoneInfo

    from notificator.config import STATE_FILE
    from notificator.legacy import import_legacy_state
    from notificator.store import Store

    name = source_name or config.active_source
    if name not in config.sources:
        raise ConfigError(f"источник {name!r} не описан в sources (есть: {sorted(config.sources)})")
    try:
        with Store(data_dir / STATE_FILE) as store:
            result = import_legacy_state(
                state_file, store, name, config.default_calendar, ZoneInfo(config.time_zone)
            )
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print(f"Перенесено событий: {result.imported} (источник {name}). Не перенесено: {len(result.skipped)}")
    for reason in result.skipped:
        print(f"  {reason}")
    print("Теперь выполните `plan`, чтобы увидеть, что сделает первый цикл.")
    return 0


def _cleanup(config, data_dir: Path, delete: bool) -> int:
    from notificator.calendars.google import GoogleCalendar
    from notificator.cleanup import delete_untracked, find_untracked
    from notificator.config import STATE_FILE
    from notificator.store import Store
    from notificator.sync.ports import CalendarError
    from notificator.wiring import google_auth

    try:
        calendar = GoogleCalendar(google_auth(config, data_dir).credentials())
        with Store(data_dir / STATE_FILE) as store:
            found = find_untracked(calendar, store)
        print(f"Событий notificator, которых нет в текущем состоянии: {len(found)}")
        for e in found:
            print(f"  {e.start:26} {e.summary!r} uid={e.uid} file: {e.file} -> {e.calendar_name}")
        if found and not delete:
            print("Ничего не удалено. Чтобы удалить эти события, повторите команду с --delete.")
        elif found:
            delete_untracked(calendar, found)
            print(f"Удалено: {len(found)}")
    except CalendarError as e:
        print(f"Ошибка календаря: {e}", file=sys.stderr)
        return 1
    return 0


def _plan(config, data_dir: Path, source_name: str | None) -> int:
    name = source_name or config.active_source
    if name not in config.sources:
        raise ConfigError(f"источник {name!r} не описан в sources (есть: {sorted(config.sources)})")
    result = preview_cycle(config, data_dir, name)
    report = result.report
    print(f"Источник: {name}. Файлов: {report.listed}, прочитано: {report.read}, не прочитано: {report.read_failed}")
    if report.error:
        print(f"Цикл остановлен, ничего не было бы изменено: {report.error}")
        return 1
    names = {"insert": "создать", "update": "обновить", "delete": "удалить"}
    for call in result.calls:
        body = call.body or {}
        when = body.get("start", {}).get("dateTime", "")
        where = (body.get("description") or "\n").splitlines()[1:2]
        what = repr(body["summary"]) if body else f"событие {call.event_id}"
        print(f"  {names[call.action]:9} {when:26} {what} {' '.join(where)} -> {call.calendar_id}")
    if not result.calls:
        print("  Изменений нет.")
    if result.deletes_need_approval:
        print(f"Удалений слишком много ({report.deleted}): настоящий цикл отложит их до подтверждения.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
