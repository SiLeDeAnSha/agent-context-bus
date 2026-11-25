#!/usr/bin/env python3
"""Multi-endpoint HTTPS/LAN/Tailscale/IPv6/IPv4 client with automatic failover and local cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import multi_machine

DEFAULT_REMOTE_CONFIG_PATH = os.path.expanduser("~/.daily-memory/remote.json")
DEFAULT_CACHE_DIR = os.path.expanduser("~/.daily-memory/cache")
STICKY_ENDPOINT_TTL_SECONDS = 180


class RemoteAuthError(RuntimeError):
    """Raised when the remote server rejects the Bearer token (401/403)."""


class RemoteUnavailableError(RuntimeError):
    """Raised when none of the configured endpoints can be reached."""


@dataclass
class Endpoint:
    name: str
    url: str
    timeout: float = 5.0


@dataclass
class RemoteConfig:
    endpoints: list[Endpoint]
    read_token: str
    write_token: str
    machine_id: str
    cache_dir: str

    @property
    def configured(self) -> bool:
        return bool(self.endpoints)


def _parse_endpoint_entry(entry: Any, idx: int) -> Endpoint | None:
    if isinstance(entry, str):
        u = entry.strip().rstrip("/")
        if not u:
            return None
        return Endpoint(name=f"endpoint-{idx + 1}", url=u, timeout=5.0)
    if isinstance(entry, dict):
        u = str(entry.get("url") or "").strip().rstrip("/")
        if not u:
            return None
        name = str(entry.get("name") or f"endpoint-{idx + 1}").strip()
        timeout = float(entry.get("timeout") or 5.0)
        return Endpoint(name=name, url=u, timeout=max(0.5, timeout))
    return None


def load_remote_config(
    remote_config_path: str | None = None,
    project_cfg: dict[str, Any] | None = None,
) -> RemoteConfig:
    """Resolve remote endpoints and tokens from env vars, ~/.daily-memory/remote.json, and config.json."""
    cfg_path = os.path.expanduser(
        remote_config_path or os.environ.get("DAILY_MEMORY_REMOTE_CONFIG") or DEFAULT_REMOTE_CONFIG_PATH
    )
    file_data: dict[str, Any] = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                file_data = loaded
        except Exception:
            file_data = {}

    proj_remote = project_cfg.get("remote") if isinstance(project_cfg, dict) and isinstance(project_cfg.get("remote"), dict) else {}

    endpoints: list[Endpoint] = []
    env_urls = (os.environ.get("DAILY_MEMORY_URLS") or os.environ.get("DAILY_MEMORY_URL") or "").strip()
    if env_urls:
        for i, raw in enumerate(env_urls.split(",")):
            ep = _parse_endpoint_entry(raw, i)
            if ep:
                endpoints.append(ep)
    else:
        raw_eps = file_data.get("endpoints") or proj_remote.get("endpoints") or []
        if isinstance(raw_eps, list):
            for i, entry in enumerate(raw_eps):
                ep = _parse_endpoint_entry(entry, i)
                if ep:
                    endpoints.append(ep)
        single = file_data.get("url") or proj_remote.get("url")
        if not endpoints and single:
            ep = _parse_endpoint_entry(single, 0)
            if ep:
                endpoints.append(ep)

    read_token = (
        os.environ.get("DAILY_MEMORY_READ_TOKEN")
        or os.environ.get("DAILY_MEMORY_TOKEN")
        or str(file_data.get("read_token") or "")
        or str(file_data.get("token") or "")
        or str(proj_remote.get("read_token") or "")
        or str(proj_remote.get("token") or "")
        or str(file_data.get("write_token") or "")
        or str(proj_remote.get("write_token") or "")
    ).strip()

    write_token = (
        os.environ.get("DAILY_MEMORY_WRITE_TOKEN")
        or os.environ.get("DAILY_MEMORY_TOKEN")
        or str(file_data.get("write_token") or "")
        or str(file_data.get("token") or "")
        or str(proj_remote.get("write_token") or "")
        or str(proj_remote.get("token") or "")
    ).strip()

    machine_id = multi_machine.normalize_machine_id(
        os.environ.get("DAILY_MEMORY_MACHINE_ID")
        or str(file_data.get("machine_id") or "")
        or str(proj_remote.get("machine_id") or "")
        or str((project_cfg or {}).get("machine_id") or "")
    )

    cache_dir = os.path.expanduser(
        os.environ.get("DAILY_MEMORY_CACHE_DIR")
        or str(file_data.get("cache_dir") or "")
        or str(proj_remote.get("cache_dir") or "")
        or DEFAULT_CACHE_DIR
    )

    return RemoteConfig(
        endpoints=endpoints,
        read_token=read_token,
        write_token=write_token,
        machine_id=machine_id,
        cache_dir=cache_dir,
    )


def _sticky_state_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, ".last_endpoint.json")


def _order_endpoints_with_sticky(rc: RemoteConfig) -> list[Endpoint]:
    if len(rc.endpoints) <= 1:
        return list(rc.endpoints)
    state_path = _sticky_state_path(rc.cache_dir)
    if not os.path.exists(state_path):
        return list(rc.endpoints)
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        ts = float(st.get("ts") or 0.0)
        last_url = str(st.get("url") or "")
        if time.time() - ts <= STICKY_ENDPOINT_TTL_SECONDS and last_url:
            primary = [ep for ep in rc.endpoints if ep.url == last_url]
            rest = [ep for ep in rc.endpoints if ep.url != last_url]
            if primary:
                return primary + rest
    except Exception:
        pass
    return list(rc.endpoints)


def _record_sticky_endpoint(cache_dir: str, ep: Endpoint) -> None:
    try:
        multi_machine.atomic_write_json(
            _sticky_state_path(cache_dir),
            {"name": ep.name, "url": ep.url, "ts": time.time()},
        )
    except Exception:
        pass


def _do_request(
    rc: RemoteConfig,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    write: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str], Endpoint]:
    if not rc.endpoints:
        raise RemoteUnavailableError("No remote endpoints configured.")

    token = rc.write_token if write else rc.read_token
    query_str = ""
    if params:
        clean = {k: str(v) for k, v in params.items() if v is not None and v != ""}
        if clean:
            query_str = "?" + urlparse.urlencode(clean)

    body_bytes: bytes | None = None
    headers = {"User-Agent": "daily-memory-client/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if extra_headers:
        headers.update(extra_headers)

    errors: list[str] = []
    for ep in _order_endpoints_with_sticky(rc):
        full_url = f"{ep.url}{path}{query_str}"
        req = urlrequest.Request(full_url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=ep.timeout) as resp:
                status = int(resp.getcode() or 200)
                raw = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                _record_sticky_endpoint(rc.cache_dir, ep)
                return status, raw, resp_headers, ep
        except urlerror.HTTPError as exc:
            status = int(exc.code)
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:240]
            except Exception:
                pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            if status in (401, 403):
                raise RemoteAuthError(f"Authentication failed on {ep.name} ({ep.url}): HTTP {status} {detail}") from exc
            if 400 <= status < 500 and status != 408 and status != 429:
                raise RuntimeError(f"Request rejected by {ep.name} ({ep.url}): HTTP {status} {detail}") from exc
            errors.append(f"{ep.name}({ep.url}): HTTP {status}")
        except Exception as exc:
            errors.append(f"{ep.name}({ep.url}): {exc}")

    raise RemoteUnavailableError("All remote endpoints failed: " + "; ".join(errors))


def request_json(
    rc: RemoteConfig,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    write: bool = False,
) -> tuple[Any, Endpoint]:
    _, raw, _, ep = _do_request(rc, method, path, params=params, json_body=json_body, write=write)
    return json.loads(raw.decode("utf-8")), ep


def request_text(
    rc: RemoteConfig,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    write: bool = False,
) -> tuple[str, Endpoint]:
    _, raw, _, ep = _do_request(rc, method, path, params=params, write=write)
    return raw.decode("utf-8"), ep


def push_raw(rc: RemoteConfig, raw_payload: dict[str, Any], machine_id: str | None = None) -> tuple[dict[str, Any], Endpoint]:
    mid = multi_machine.normalize_machine_id(machine_id or rc.machine_id)
    data, ep = request_json(
        rc,
        "POST",
        "/api/v1/ingest/raw",
        json_body={"machine_id": mid, "payload": raw_payload},
        write=True,
    )
    return data, ep


def push_summary(
    rc: RemoteConfig,
    summary_payload: dict[str, Any] | list[dict[str, Any]],
    machine_id: str | None = None,
) -> tuple[dict[str, Any], Endpoint]:
    mid = multi_machine.normalize_machine_id(machine_id or rc.machine_id)
    data, ep = request_json(
        rc,
        "POST",
        "/api/v1/ingest/summary",
        json_body={"machine_id": mid, "payload": summary_payload},
        write=True,
    )
    return data, ep


def sync_local_cache(rc: RemoteConfig) -> dict[str, Any]:
    """Download MEMORY_INDEX.md + summary_all_history.json into local offline cache."""
    bundle, ep = request_json(rc, "GET", "/api/v1/bundle", write=False)
    os.makedirs(os.path.join(rc.cache_dir, "data"), exist_ok=True)
    index_md = str(bundle.get("memory_index_md") or "")
    days = bundle.get("days") or []
    index_path = os.path.join(rc.cache_dir, "MEMORY_INDEX.md")
    master_path = os.path.join(rc.cache_dir, "data", "summary_all_history.json")
    meta_path = os.path.join(rc.cache_dir, "cache_meta.json")
    if index_md:
        multi_machine.atomic_write_text(index_path, index_md)
    multi_machine.atomic_write_json(master_path, {"days": days})
    meta = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_name": ep.name,
        "endpoint_url": ep.url,
        "total_days": len(days),
    }
    multi_machine.atomic_write_json(meta_path, meta)
    return {**meta, "index_path": index_path, "master_path": master_path}


def probe_endpoints(rc: RemoteConfig) -> list[dict[str, Any]]:
    """Probe all configured endpoints in priority order and report reachability and latency."""
    results: list[dict[str, Any]] = []
    for ep in rc.endpoints:
        t0 = time.monotonic()
        url = f"{ep.url}/healthz"
        req = urlrequest.Request(url, headers={"User-Agent": "daily-memory-probe/1.0"}, method="GET")
        try:
            with urlrequest.urlopen(req, timeout=ep.timeout) as resp:
                ms = round((time.monotonic() - t0) * 1000.0, 1)
                body = resp.read().decode("utf-8", errors="replace")
                results.append(
                    {
                        "name": ep.name,
                        "url": ep.url,
                        "ok": 200 <= resp.getcode() < 300,
                        "status": int(resp.getcode() or 200),
                        "latency_ms": ms,
                        "detail": body[:120],
                    }
                )
        except Exception as exc:
            ms = round((time.monotonic() - t0) * 1000.0, 1)
            results.append(
                {
                    "name": ep.name,
                    "url": ep.url,
                    "ok": False,
                    "status": 0,
                    "latency_ms": ms,
                    "detail": str(exc)[:160],
                }
            )
    return results
