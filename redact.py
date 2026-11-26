#!/usr/bin/env python3
"""Zero-dependency pre-persistence secret redaction engine for Agent Context Bus.

Redacts high-risk credentials (private keys, API keys, Bearer tokens, JWTs,
URL credentials, explicit password/secret assignments) before writing into
raw/live/note/decision/FTS stores or sending payloads to external LLM APIs.
Never mutates original client transcript files.
"""

from __future__ import annotations

import re
from typing import Any

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])("
    r"sk-ant-[A-Za-z0-9_-]{16,}"
    r"|sk-(?:proj-|svcacct-|live-|test-)?[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-zA-Z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ASIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xox[baprs]-[0-9A-Za-z-]{10,}"
    r")(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(
    r"(?i)(\bAuthorization\s*:\s*Bearer\s+|(?<![A-Za-z0-9_])Bearer\s+)([A-Za-z0-9._~+/=-]{16,})"
)
_COOKIE_RE = re.compile(
    r"(?im)^(\s*Cookie\s*:\s*)([^\r\n]{8,})$"
)
_URL_CRED_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]{1,64}:)([^/\s@]{3,})(@)"
)
_KV_SECRET_RE = re.compile(
    r"""(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|write[_-]?token|read[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd|pwd|private[_-]?key)\b\s*[:=]\s*["']?)([^\s"'`,;}\]\\]{8,})(["']?)"""
)

_SAFE_PLACEHOLDER_PREFIXES = (
    "$",
    "<",
    "[redacted",
    "your_",
    "your-",
    "example",
    "placeholder",
    "changeme",
    "change_me",
    "replace_",
    "dummy",
    "sample",
    "test_only",
    "****",
    "xxxx",
    "/",
    "~/",
    "./",
    "../",
    "http://",
    "https://",
)

_SAFE_EXACT_VALUES = {
    "none",
    "null",
    "true",
    "false",
    "undefined",
    "required",
    "optional",
    "configured",
    "redacted",
}


def _looks_like_placeholder(val: str) -> bool:
    low = val.strip().lower()
    if not low or low in _SAFE_EXACT_VALUES:
        return True
    if any(low.startswith(p) for p in _SAFE_PLACEHOLDER_PREFIXES):
        return True
    if set(low) <= {"*", "x", ".", "-", "_"}:
        return True
    # Avoid redacting pure python/config dotted identifiers like settings.api_key
    if re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+", val):
        return True
    return False


def _replace_kv(match: re.Match) -> str:
    prefix, val, suffix = match.group(1), match.group(2), match.group(3)
    if _looks_like_placeholder(val):
        return match.group(0)
    return f"{prefix}[REDACTED:SECRET]{suffix}"


def _replace_url_cred(match: re.Match) -> str:
    prefix, pwd, at_sign = match.group(1), match.group(2), match.group(3)
    if _looks_like_placeholder(pwd):
        return match.group(0)
    return f"{prefix}[REDACTED:PASS]{at_sign}"


def _replace_bearer(match: re.Match) -> str:
    prefix, tok = match.group(1), match.group(2)
    if _looks_like_placeholder(tok):
        return match.group(0)
    return f"{prefix}[REDACTED:BEARER]"


def is_redaction_enabled(cfg: dict[str, Any] | None = None) -> bool:
    if not isinstance(cfg, dict):
        return True
    rcfg = cfg.get("redact")
    if isinstance(rcfg, dict) and rcfg.get("disabled") is True:
        return False
    return True


def compile_extra_patterns(cfg: dict[str, Any] | None = None) -> list[re.Pattern]:
    if not isinstance(cfg, dict):
        return []
    rcfg = cfg.get("redact")
    if not isinstance(rcfg, dict):
        return []
    raw_list = rcfg.get("extra_patterns")
    if not isinstance(raw_list, list):
        return []
    out: list[re.Pattern] = []
    for item in raw_list:
        if isinstance(item, str) and item.strip():
            try:
                out.append(re.compile(item))
            except re.error:
                continue
    return out


def redact_text(
    text: str,
    *,
    extra_patterns: list[re.Pattern] | None = None,
    enabled: bool = True,
) -> str:
    if not enabled or not isinstance(text, str) or not text:
        return text
    s = _PRIVATE_KEY_RE.sub("[REDACTED:PRIVATE_KEY]", text)
    s = _JWT_RE.sub("[REDACTED:JWT]", s)
    s = _API_KEY_RE.sub("[REDACTED:API_KEY]", s)
    s = _BEARER_RE.sub(_replace_bearer, s)
    s = _COOKIE_RE.sub(r"\1[REDACTED:COOKIE]", s)
    s = _URL_CRED_RE.sub(_replace_url_cred, s)
    s = _KV_SECRET_RE.sub(_replace_kv, s)
    if extra_patterns:
        for pat in extra_patterns:
            s = pat.sub("[REDACTED:CUSTOM]", s)
    return s


def redact_obj(
    obj: Any,
    *,
    extra_patterns: list[re.Pattern] | None = None,
    enabled: bool = True,
) -> Any:
    if not enabled:
        return obj
    if isinstance(obj, str):
        return redact_text(obj, extra_patterns=extra_patterns, enabled=True)
    if isinstance(obj, list):
        return [redact_obj(x, extra_patterns=extra_patterns, enabled=True) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x, extra_patterns=extra_patterns, enabled=True) for x in obj)
    if isinstance(obj, dict):
        return {
            k: redact_obj(v, extra_patterns=extra_patterns, enabled=True)
            for k, v in obj.items()
        }
    return obj


def redact_with_cfg(obj: Any, cfg: dict[str, Any] | None = None) -> Any:
    if not is_redaction_enabled(cfg):
        return obj
    extra = compile_extra_patterns(cfg)
    return redact_obj(obj, extra_patterns=extra, enabled=True)
