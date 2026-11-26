"""Codex CLI client adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class CodexAdapter:
    name = "codex"
    display_name = "Codex"

    def collect_day(
        self,
        start_dt: datetime,
        end_dt: datetime,
        tz: ZoneInfo,
        workday: str,
        cfg: dict[str, Any],
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], list[datetime]]:
        import collect

        return collect.collect_codex(
            start_dt,
            end_dt,
            tz,
            workday,
            cfg,
            paths=kwargs.get("paths"),
        )

    def scan_live(
        self,
        start: datetime,
        end: datetime,
        tz: ZoneInfo,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        import live_memory

        return live_memory.scan_codex_today(
            start,
            end,
            tz,
            codex_home=kwargs.get("codex_home"),
        )

    def extract_hook(
        self,
        payload: dict[str, Any],
        now: datetime,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        import live_memory

        return live_memory._extract_hook_session(
            "codex",
            payload,
            now,
            codex_home=kwargs.get("codex_home"),
        )

    def iter_session_files(self, ids: set[str]) -> Iterator[tuple[str, Any]]:
        import search_sessions

        yield from search_sessions.session_files("codex", ids)
