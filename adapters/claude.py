"""Claude Code client adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class ClaudeAdapter:
    name = "claude"
    display_name = "Claude Code"

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

        return collect.collect_claude(start_dt, end_dt, tz, workday, cfg)

    def scan_live(
        self,
        start: datetime,
        end: datetime,
        tz: ZoneInfo,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        import live_memory

        return live_memory.scan_claude_today(
            start,
            end,
            tz,
            projects_dir=kwargs.get("projects_dir"),
        )

    def extract_hook(
        self,
        payload: dict[str, Any],
        now: datetime,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        import live_memory

        return live_memory._extract_hook_session("claude", payload, now)

    def iter_session_files(self, ids: set[str]) -> Iterator[tuple[str, Any]]:
        import search_sessions

        yield from search_sessions.session_files("claude", ids)
