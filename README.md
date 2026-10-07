# notificator

Читает заметки из облака (ownCloud по WebDAV, Seafile по REST API) или из локальной папки,
находит в них события и поддерживает их в Google Calendar: создаёт, обновляет и удаляет.

Событие в заметке — это блок в любом месте текстового файла:

```xml
<event>
  <uid>abc123</uid>
  <summary>Встреча</summary>
  <start>2026-11-11 08:00</start>
  <end>2026-11-11 09:00</end>
</event>
```

Необязательные поля: `description`, `time_zone`, `location`, `recurrence`, `calendar_id`,
`attendees` (вложенные теги с email). Строки, начинающиеся с `#`, игнорируются.
В файлах `.csv` события задаются строками таблицы с колонками `uid`, `summary`, `start` и теми же
необязательными; участники в `attendees` разделяются `;`.

## Требования

- Python 3.11 или новее.
- Файл клиента OAuth из Google Cloud Console (тип «Web application»).

## Установка

```bash
git clone git@github.com:zotochev/notes.git /opt/notificator
cd /opt/notificator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir data
```

## Каталог data/

Всё, что относится к конкретной установке, лежит в `data/` и не попадает в git.

| Файл | Что это | Откуда берётся |
|---|---|---|
| `config.json` | Настройки | Пишете сами, образец ниже |
| `google_credentials.json` | Клиент OAuth | Скачивается из Google Cloud Console |
| `google_token.json` | Токен доступа к календарю | Создаётся при входе в Google через админку |
| `state.db` | Что уже синхронизировано | Создаётся сервисом |

### config.json

```json
{
  "active_source": "seafile",
  "sources": {
    "seafile": {
      "type": "seafile",
      "url": "https://cloud.example.com:8443",
      "username": "me@example.com",
      "password": "…",
      "watch_paths": ["/mylib"]
    },
    "owncloud": {
      "type": "webdav",
      "url": "https://cloud.example.com/dav/spaces/<id>",
      "username": "me",
      "password": "…",
      "watch_paths": ["/"]
    }
  },
  "default_calendar": "me@gmail.com",
  "time_zone": "Europe/Ulyanovsk",
  "extensions": [".md", ".txt", ".csv"],
  "google": {"redirect_uri": "https://notificator.example.com/google/oauth/callback"}
}
```

- `active_source` — источник, из которого читаются события. Остальные описаны про запас.
- У Seafile каждый путь в `watch_paths` начинается с имени библиотеки: `/mylib` или `/mylib/notes`.
- Третий тип источника — локальная папка: `{"type": "local", "roots": ["/home/me/notes"]}`.
- `default_calendar` — календарь для событий без `calendar_id`.
- `google.redirect_uri` нужен только для входа в Google через админку и должен совпадать с адресом,
  разрешённым в Google Cloud Console.
- Остальные ключи и значения по умолчанию описаны в `notificator/config.py`. Неизвестный ключ —
  ошибка при запуске, а не тихо проигнорированная опечатка.

Настройки читаются при запуске: после правки `config.json` перезапустите сервис.

## Команды

Выполняются из каталога проекта.

```bash
.venv/bin/python -m notificator plan                # что сделал бы цикл; ничего не меняет
.venv/bin/python -m notificator plan --source NAME  # то же для другого источника из config.json
.venv/bin/python -m notificator run                 # синхронизация и админка
.venv/bin/python -m notificator cleanup             # события notificator в календарях, которых нет в состоянии
.venv/bin/python -m notificator cleanup --delete    # удалить их
```

`cleanup` узнаёт события notificator по заголовку в описании (`uid: …`, `file: …`, `---`) и
показывает те, которых нет в `state.db`: остатки старой версии, дубликаты. Чужие события
календаря он не трогает. Без `--delete` ничего не удаляется.

Каталог данных по умолчанию — `data/`; другой задаётся так:
`python -m notificator --data-dir /путь run`.

## Админка

`run` поднимает страницу на `http://127.0.0.1:40000` (`host` и `port` в `config.json`).
Входа по паролю у неё нет: наружу её нужно выставлять только через reverse proxy с
аутентификацией.

На странице: состояние синхронизации, список «Требует внимания» (ошибки в файлах, отказы
календаря, файлы, которые не удалось прочитать, удаления в ожидании подтверждения), события,
журнал действий. Клик по названию события показывает, как оно выглядит в Google Calendar.

## Когда события удаляются

Событие удаляется из календаря, только если это подтверждено:

- файл прочитан, разобран без неясностей, и события с таким `uid` в нём больше нет;
- источник отдал полный список файлов, и файла в нём нет;
- активным стал другой источник, и он успешно ответил.

Сбой облака, нечитаемый файл или опечатка в блоке `<event>` удалением не считаются: событие
остаётся в календаре, проблема появляется в «Требует внимания».

Если цикл хочет удалить сразу много (по умолчанию от 5 событий и больше 20% отслеживаемых),
удаление откладывается до подтверждения кнопкой в админке. Пороги — `held_deletes_min` и
`max_delete_ratio` в `config.json`.

## Запуск как сервис (systemd)

```bash
sudo cp notificator.service /etc/systemd/system/
sudo nano /etc/systemd/system/notificator.service   # User, WorkingDirectory, ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now notificator
journalctl -u notificator -f
```

## Переезд со старой версии

Состояние старой версии не переносится: новая заново прочитает файлы и создаст события.

1. Остановите старый сервис.
2. Установите новую версию (раздел «Установка»).
3. Перенесите файлы:

   | Из старого проекта | В новый |
   |---|---|
   | `notificator/credentials.json` | `data/google_credentials.json` |
   | `notificator/token.json` | `data/google_token.json` |
   | `config_override.json` | не копируется: по нему пишется `data/config.json` (образец выше) |
   | `state.json`, `events.log`, `config_dumps/`, `webdav_token.json` | не нужны |

   Соответствие настроек: `WEBDAV_URL`, `WEBDAV_USERNAME`, `WEBDAV_PASSWORD`, `WEBDAV_WATCH_PATHS` →
   поля источника `url`, `username`, `password`, `watch_paths`; `GOOGLE_CALENDAR_ID` →
   `default_calendar`; `EXTENSIONS` → `extensions`; `GOOGLE_REDIRECT_URI` → `google.redirect_uri`;
   `TIMEZONE_DEFAULT` → `time_zone`; `RESCAN_INTERVAL_SEC` → `scan_interval_sec`.

   Для Seafile `url` — адрес сервера без `/seafdav`, а в `watch_paths` первым идёт имя библиотеки.

4. Посмотрите, что сделает первый цикл: `.venv/bin/python -m notificator plan`.
5. Запустите сервис и дождитесь первого цикла без ошибок. Новая версия создаст свои события;
   созданные старой версией пока остаются, поэтому события временно видны дважды.
6. Уберите события старой версии: `.venv/bin/python -m notificator cleanup` покажет их,
   `cleanup --delete` удалит. Старый `state.json` для этого не нужен.

Порядок шагов 5 и 6 важен: `cleanup` считает лишним всё, чего нет в новом состоянии. Если
запустить его до первого цикла, он удалит и те события, которые новая версия ещё не успела
создать заново, — они появятся снова, но только при следующем цикле.

## Смена источника

Поменяйте `active_source` в `config.json` и перезапустите сервис. События прежнего источника
удаляются из календаря (большое удаление — после подтверждения в админке), события нового
создаются. Удаление начинается только после того, как новый источник успешно ответил.

## Тесты

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
