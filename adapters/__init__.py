"""Pluggable client adapters for Agent Context Bus.

Each adapter encapsulates one AI client's local storage discovery, daily trace
collection, 0-token real-time WAL scanning, hook extraction, and raw session
iteration so new hosts (e.g., Cursor, opencode) can be added without modifying
core orchestration code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Protocol
from zoneinfo import ZoneInfo


class ClientAdapter(Protocol):
    name: str
    display_name: str

    def collect_day(
        self,
        start_dt: datetime,
        end_dt: datetime,
        tz: ZoneInfo,
        workday: str,
        cfg: dict[str, Any],
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[datetime]]: ...

    def scan_live(
        self,
        start: datetime,
        end: datetime,
        tz: ZoneInfo,
        **kwargs: Any,
    ) -> list[dict[str, Any]]: ...

    def extract_hook(
        self,
        payload: dict[str, Any],
        now: datetime,
        **kwargs: Any,
    ) -> dict[str, Any] | None: ...

    def iter_session_files(self, ids: set[str]) -> Iterator[tuple[str, Any]]: ...


from adapters.antigravity import AntigravityAdapter
from adapters.claude import ClaudeAdapter
from adapters.codex import CodexAdapter
from adapters.workbuddy import WorkBuddyAdapter

ADAPTERS: dict[str, ClientAdapter] = {
    "antigravity": AntigravityAdapter(),
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
    "workbuddy": WorkBuddyAdapter(),
}


def get_adapter(name: str) -> ClientAdapter | None:
    return ADAPTERS.get(str(name or "").strip().lower())


def list_adapters() -> list[str]:
    return list(ADAPTERS.keys())
