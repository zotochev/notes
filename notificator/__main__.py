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
    args = parser.parse_args(argv)

    try:
        config = load_config(args.data_dir)
        if args.command == "plan":
            return _plan(config, args.data_dir, args.source)
        if args.command == "run":
            return _run(config, args.data_dir)
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
        print(f"  {names[call.action]:9} {when:26} {body.get('summary', call.event_id)!r} {' '.join(where)} -> {call.calendar_id}")
    if not result.calls:
        print("  Изменений нет.")
    if result.deletes_need_approval:
        print(f"Удалений слишком много ({report.deleted}): настоящий цикл отложит их до подтверждения.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
