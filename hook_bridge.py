#!/usr/bin/env python3
"""Ultra-safe, non-blocking lifecycle hook entrypoint for Antigravity / Codex / Claude Code.

Guarantees:
- Never blocks the agent loop (<50ms): spawns `live_memory.py` detached in background.
- Never writes tracebacks or diagnostics to stdout/stderr.
- Always emits valid JSON `{}` to stdout and exits 0 so host clients never stall or error.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_STDIN_BYTES = 262_144


def _log_error(data_dir: str, msg: str) -> None:
    try:
        live_dir = os.path.join(data_dir, "live")
        os.makedirs(live_dir, exist_ok=True)
        with open(os.path.join(live_dir, "hook_errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def handle_hook(
    client: str,
    event: str,
    raw_stdin: str,
    *,
    data_dir: str | None = None,
    spawn: bool = True,
) -> dict:
    target_data_dir = data_dir or os.environ.get("DAILY_MEMORY_DATA_DIR") or os.path.join(BASE_DIR, "data")
    try:
        payload = json.loads(raw_stdin) if raw_stdin and raw_stdin.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    # Guard against recursive Stop loops if a client sets continuation flags
    if event.lower() != "stop":
        return {}

    if spawn:
        try:
            live_dir = os.path.join(target_data_dir, "live")
            os.makedirs(live_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="hook_", suffix=".json", dir=live_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            worker = os.path.join(BASE_DIR, "live_memory.py")
            subprocess.Popen(
                [
                    sys.executable,
                    worker,
                    "--data-dir",
                    target_data_dir,
                    "--client",
                    client,
                    "--payload-file",
                    tmp_path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as exc:
            _log_error(target_data_dir, f"{client}/{event}: {exc!r}")

    return {}


def main() -> None:
    data_dir = os.environ.get("DAILY_MEMORY_DATA_DIR") or os.path.join(BASE_DIR, "data")
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--client", default="antigravity")
        parser.add_argument("--event", default="Stop")
        parser.add_argument("--data-dir", default=None)
        args, _ = parser.parse_known_args()
        if args.data_dir:
            data_dir = args.data_dir
        raw = ""
        try:
            if not sys.stdin.isatty():
                raw = sys.stdin.read(MAX_STDIN_BYTES)
        except Exception:
            raw = ""
        resp = handle_hook(args.client, args.event, raw, data_dir=data_dir, spawn=True)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    except BaseException as exc:
        _log_error(data_dir, f"fatal: {exc!r}")
        try:
            sys.stdout.write("{}\n")
            sys.stdout.flush()
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
