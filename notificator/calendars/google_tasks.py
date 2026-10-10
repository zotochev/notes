"""
Google Tasks backend.

One instance holds one API client, which is not thread-safe: create an
instance per sync cycle or per request, do not share it between threads.
"""
from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build

from notificator.calendars.google import execute
from notificator.sync.ports import CalendarError, CalendarUnavailable, EventNotFound


class GoogleTasks:
    def __init__(self, credentials: Any = None, http: Any = None, retries: int = 3) -> None:
        """Pass `credentials` for real use; `http` replaces the transport in tests."""
        self._service = build("tasks", "v1", credentials=credentials, http=http, cache_discovery=False)
        self._retries = retries

    def insert(self, tasklist: str, body: dict[str, Any]) -> str:
        """Create a task and return the id Google gave it."""
        try:
            return self._execute(self._service.tasks().insert(tasklist=tasklist, body=body))["id"]
        except EventNotFound as e:
            # A new task cannot be "not found": it is the list that is missing.
            raise _no_such_list(tasklist) from e

    def update(self, tasklist: str, task_id: str, body: dict[str, Any]) -> None:
        """Change what we set and leave the rest, such as "completed", as it is. Raises EventNotFound."""
        task = self._execute(self._service.tasks().patch(tasklist=tasklist, task=task_id, body=body))
        if task.get("deleted"):
            # Deleted in Google: the task is kept for a while as a hidden stub.
            raise EventNotFound(f"задача {task_id} удалена в Google")

    def delete(self, tasklist: str, task_id: str) -> None:
        try:
            self._execute(self._service.tasks().delete(tasklist=tasklist, task=task_id))
        except EventNotFound:
            pass

    def get(self, tasklist: str, task_id: str) -> dict[str, Any]:
        """Return the raw task resource. Raises EventNotFound."""
        return self._execute(self._service.tasks().get(tasklist=tasklist, task=task_id))

    def find(self, tasklist: str, marker: str) -> str | None:
        """The id of a task in the list whose notes begin with `marker`, completed ones included.

        A list that does not exist holds no tasks: the answer is None, not an error.
        """
        page_token = None
        while True:
            try:
                page = self._execute(self._service.tasks().list(
                    tasklist=tasklist, pageToken=page_token, maxResults=100, showCompleted=True, showHidden=True,
                ))
            except EventNotFound:
                return None
            for task in page.get("items", []):
                if (task.get("notes") or "").startswith(marker) and not task.get("deleted"):
                    return task["id"]
            page_token = page.get("nextPageToken")
            if not page_token:
                return None

    def tasklists(self) -> list[dict[str, Any]]:
        """The user's task lists as {id, title}."""
        lists: list[dict[str, Any]] = []
        page_token = None
        while True:
            page = self._execute(self._service.tasklists().list(pageToken=page_token, maxResults=100))
            lists += [{"id": t["id"], "title": t.get("title", t["id"])} for t in page.get("items", [])]
            page_token = page.get("nextPageToken")
            if not page_token:
                return lists

    def _execute(self, request: Any) -> Any:
        try:
            return execute(request, self._retries)
        except (EventNotFound, CalendarUnavailable):
            raise
        except CalendarError as e:
            if "HTTP 403" in str(e):
                # No permission is one task's problem, not a reason to stop the events as well.
                raise CalendarError(
                    "нет доступа к Google Tasks: включите Google Tasks API в проекте Google Cloud "
                    f"и войдите в Google заново через админку ({e})"
                ) from e
            raise


def _no_such_list(tasklist: str) -> CalendarError:
    return CalendarError(
        f"список задач {tasklist!r} не найден или недоступен этому аккаунту: "
        "в tasklist пишется идентификатор списка из раздела «Google» в админке, а не его название"
    )
