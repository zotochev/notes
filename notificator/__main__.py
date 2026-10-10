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
    move = commands.add_parser(
        "move-source", help="перенести связи событий на другой источник с теми же файлами, не пересоздавая события"
    )
    move.add_argument("old", help="имя источника, чьи события переносятся")
    move.add_argument("new", help="имя источника из config.json, который их получит")
    move.add_argument(
        "--path", action="append", default=[], metavar="СТАРЫЙ=НОВЫЙ",
        help="замена начала пути файла, например /Notes=/mylib/Notes; можно указать несколько раз",
    )
    move.add_argument("--apply", action="store_true", help="выполнить перенос, а не только показать")
    move.add_argument(
        "--no-check", action="store_true", help="не читать новый источник, чтобы показать первый цикл после переноса"
    )
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
        if args.command == "move-source":
            return _move_source(
                config, args.data_dir, args.old, args.new, args.path, args.apply, not args.no_check
            )
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


def _move_source(
    config, data_dir: Path, old: str, new: str, rules: list[str], apply: bool, check: bool = True
) -> int:
    from notificator.config import STATE_FILE
    from notificator.migrate import move_source
    from notificator.store import Store

    if new not in config.sources:
        raise ConfigError(f"источник {new!r} не описан в sources (есть: {sorted(config.sources)})")
    if old == new or not rules or not all("=" in rule for rule in rules):
        print("Ошибка: нужны два разных источника и хотя бы одно правило --path СТАРЫЙ=НОВЫЙ", file=sys.stderr)
        return 1
    prefixes = [tuple(rule.split("=", 1)) for rule in rules]
    with Store(data_dir / STATE_FILE) as store:
        result = move_source(store, old, new, prefixes, apply=False)
    print(f"{'Переносится' if apply else 'Будет перенесено'} событий: {result.moved} ({old} -> {new}). "
          f"Не переносится: {len(result.skipped)}")
    for before, after in result.examples:
        print(f"  {before} -> {after}")
    for reason in result.skipped:
        print(f"  {reason}")
    if check and not _first_cycle_after_move(config, data_dir, old, new, prefixes):
        print("Источник не удалось проверить, перенос не выполнен. Без проверки: --no-check.", file=sys.stderr)
        return 1
    if not apply:
        print("Ничего не изменено. Чтобы выполнить перенос, повторите команду с --apply.")
        return 0
    with Store(data_dir / STATE_FILE) as store:
        backup = data_dir / f"{STATE_FILE}.before-move"
        store.backup_to(backup)
        move_source(store, old, new, prefixes, apply=True)
    print(f"Перенос выполнен. Копия состояния до переноса: {backup}")
    print(f"Теперь сделайте {new!r} активным источником и запустите сервис.")
    return 0


def _first_cycle_after_move(config, data_dir: Path, old: str, new: str, prefixes: list[tuple[str, str]]) -> bool:
    """Show what the first cycle of the new source would do after the move. False when it could not be worked out.

    The files are read from the new source for real; the move and the cycle happen in a throwaway copy of the state.
    """
    from notificator.migrate import move_source

    # Calendar event id -> what the event is, for the calls that carry no body.
    known: dict[str, str] = {}

    def move_in_copy(store) -> None:
        move_source(store, old, new, prefixes, apply=True)
        for source in store.event_counts():
            for e in store.events(source):
                known[e["gcal_event_id"]] = f"{e['summary']!r} uid={e['uid']} {source}:{e['path']}"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    preview = preview_cycle(config, data_dir, new, before=move_in_copy)
    if preview.report.error:
        print(f"Проверка по источнику {new} не удалась: {preview.report.error}", file=sys.stderr)
        return False
    actions = [call.action for call in preview.calls]
    kept = len(known) - actions.count("delete")
    print(f"Проверка по источнику {new} (первый цикл после переноса): файлов {preview.report.listed}, "
          f"не прочитано {preview.report.read_failed}.")
    print(f"  события сохранятся: {kept} (из них обновить описание: {actions.count('update')})")
    print(f"  будут удалены из календаря: {actions.count('delete')}")
    print(f"  будут созданы заново: {actions.count('insert')}")
    for call in preview.calls:
        if call.action == "delete":
            print(f"    удалить {known.get(call.event_id, call.event_id)}")
        elif call.action == "insert":
            print(f"    создать {_what(call)}")
    if preview.deletes_need_approval:
        print("  Удалений много: настоящий цикл отложит их до подтверждения в админке.")
    if "delete" in actions and "insert" in actions:
        print("  Есть и «удалить», и «создать»: возможно, правило --path даёт не те пути.")
    return True


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
    # A first pass over a large cloud takes minutes: show what is going on.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    result = preview_cycle(config, data_dir, name)
    report = result.report
    print(f"Источник: {name}. Файлов: {report.listed}, прочитано: {report.read}, не прочитано: {report.read_failed}")
    if report.error:
        print(f"Цикл остановлен, ничего не было бы изменено: {report.error}")
        return 1
    names = {"insert": "создать", "update": "обновить", "delete": "удалить"}
    for call in result.calls:
        print(f"  {names[call.action]:9} {_when(call):26} {_what(call)} -> {call.calendar_id}")
    if not result.calls:
        print("  Изменений нет.")
    if result.deletes_need_approval:
        print(f"Удалений слишком много ({report.deleted}): настоящий цикл отложит их до подтверждения.")
    return 0


def _when(call) -> str:
    """The start of a planned event or the due date of a planned task."""
    body = call.body or {}
    if call.kind == "task":
        return (body.get("due") or "")[:10]
    return body.get("start", {}).get("dateTime", "")


def _what(call) -> str:
    """A planned call's event or task in words: its title and the file it comes from."""
    body = call.body
    is_task = call.kind == "task"
    if not body:
        return f"{'задача' if is_task else 'событие'} {call.event_id}"
    text = body.get("notes") if is_task else body.get("description")
    where = " ".join((text or "\n").splitlines()[1:2])
    return f"{'задача ' if is_task else ''}{(body.get('title') if is_task else body['summary'])!r} {where}"


if __name__ == "__main__":
    sys.exit(main())
