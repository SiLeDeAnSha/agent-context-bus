"""Antigravity client adapter (delegates to collect/live_memory/search_sessions while providing a clean plugin seam)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class AntigravityAdapter:
    name = "antigravity"
    display_name = "Antigravity"

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

        return collect.collect_antigravity(
            start_dt,
            end_dt,
            tz,
            workday,
            cfg,
            allow_missing_metadata=bool(kwargs.get("allow_missing_metadata", False)),
        )

    def scan_live(
        self,
        start: datetime,
        end: datetime,
        tz: ZoneInfo,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        import live_memory

        return live_memory.scan_antigravity_today(
            start,
            end,
            tz,
            db_path=kwargs.get("db_path"),
            brain_dir=kwargs.get("brain_dir"),
        )

    def extract_hook(
        self,
        payload: dict[str, Any],
        now: datetime,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        import live_memory

        return live_memory._extract_hook_session(
            "antigravity",
            payload,
            now,
            ag_db_path=kwargs.get("ag_db_path"),
        )

    def iter_session_files(self, ids: set[str]) -> Iterator[tuple[str, Any]]:
        import search_sessions

        yield from search_sessions.session_files("antigravity", ids)
