#!/usr/bin/env python3
"""Safely migrate local Codex task metadata to the current model provider."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = 6
GUARD_STATE_FIELDS = {
    "last_provider",
    "last_guard_time",
    "last_guard_version",
    "last_manifest",
    "last_result",
}
REQUIRED_COLUMNS = {
    "id",
    "rollout_path",
    "source",
    "model_provider",
    "archived",
    "archived_at",
}
PROVIDER_RE = re.compile(
    r"^\s*model_provider\s*=\s*(?:\"((?:\\.|[^\"\\])*)\"|'([^']*)')"
)
SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TABLE_HEADER_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
RESERVED_PROVIDER_NAMES = {"openai", "oss", "ollama", "lmstudio"}

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


class RepairError(RuntimeError):
    pass


class ManagedConnection(sqlite3.Connection):
    """Close SQLite connections when their transaction context exits."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def resolve_codex_home(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def resolve_rollout_path(path_value: str, codex_home: Path | None = None) -> Path:
    """Resolve rollout paths stored by either old or current Codex versions."""
    path = Path(path_value).expanduser()
    if not path.is_absolute() and codex_home is not None:
        path = codex_home / path
    return path.resolve()


def parse_provider(config_path: Path, override: str | None) -> tuple[str, str]:
    if override:
        return override, "command_line"
    if config_path.is_file():
        in_table = False
        with config_path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if stripped.startswith("["):
                    in_table = True
                if in_table:
                    continue
                match = PROVIDER_RE.match(raw_line)
                if not match:
                    continue
                if match.group(1) is not None:
                    value = json.loads('"' + match.group(1) + '"')
                else:
                    value = match.group(2)
                if value:
                    return value, "config.toml"
    return "openai", "codex_default"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provider_header_prefix(provider: str) -> str:
    if not SAFE_PROVIDER_RE.fullmatch(provider):
        raise RepairError(f"Unsupported provider name for aliasing: {provider!r}")
    return f"model_providers.{provider}"


def table_name(line: str) -> str | None:
    match = TABLE_HEADER_RE.match(line.rstrip("\r\n"))
    return match.group(1).strip() if match else None


def provider_block_bounds(lines: list[str], provider: str) -> tuple[int, int] | None:
    prefix = provider_header_prefix(provider)
    start = None
    for index, line in enumerate(lines):
        name = table_name(line)
        if name == prefix:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    nested_prefix = prefix + "."
    for index in range(start + 1, len(lines)):
        name = table_name(lines[index])
        if name is not None and name != prefix and not name.startswith(nested_prefix):
            end = index
            break
    return start, end


def builtin_openai_provider_block() -> list[str]:
    return [
        "[model_providers.openai]\n",
        'name = "openai"\n',
        'base_url = "https://api.openai.com/v1"\n',
        'wire_api = "responses"\n',
        "requires_openai_auth = true\n",
    ]


def builtin_openai_provider_config() -> dict[str, Any]:
    return {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "wire_api": "responses",
        "requires_openai_auth": True,
    }


def clone_provider_block(
    block: list[str], source_provider: str, target_provider: str
) -> list[str]:
    source_prefix = provider_header_prefix(source_provider)
    target_prefix = provider_header_prefix(target_provider)
    result: list[str] = []
    in_main_table = False
    name_replaced = False
    for line in block:
        name = table_name(line)
        if name is not None:
            if name == source_prefix or name.startswith(source_prefix + "."):
                suffix = name[len(source_prefix) :]
                line = f"[{target_prefix}{suffix}]\n"
                in_main_table = suffix == ""
            else:
                in_main_table = False
        if in_main_table and re.match(r"^\s*name\s*=", line):
            line = f'name = {json.dumps(target_provider)}\n'
            name_replaced = True
        result.append(line)
    if not name_replaced:
        result.insert(1, f'name = {json.dumps(target_provider)}\n')
    while result and not result[-1].strip():
        result.pop()
    result.append("\n")
    return result


def build_provider_alias_config(
    config_path: Path, current_provider: str, legacy_providers: Iterable[str]
) -> tuple[str, str, list[str]]:
    if not config_path.is_file():
        raise RepairError(f"Cannot synchronize provider aliases without {config_path}")
    original = config_path.read_text(encoding="utf-8-sig")
    lines = original.splitlines(keepends=True)
    source_bounds = provider_block_bounds(lines, current_provider)
    if source_bounds is None:
        if current_provider != "openai":
            raise RepairError(
                f"Current provider table [model_providers.{current_provider}] was not found"
            )
        source_block = builtin_openai_provider_block()
    else:
        source_block = lines[source_bounds[0] : source_bounds[1]]

    aliases = sorted(
        {
            provider
            for provider in legacy_providers
            if provider and provider != current_provider
        }
    )
    for provider in aliases:
        provider_header_prefix(provider)

    updated_lines = list(lines)
    for provider in aliases:
        bounds = provider_block_bounds(updated_lines, provider)
        if bounds is not None:
            del updated_lines[bounds[0] : bounds[1]]

    if aliases:
        while updated_lines and not updated_lines[-1].strip():
            updated_lines.pop()
        updated_lines.append("\n")
        for provider in aliases:
            updated_lines.extend(
                clone_provider_block(source_block, current_provider, provider)
            )

    updated = "".join(updated_lines)
    if tomllib is not None:
        tomllib.loads(updated)
    return original, updated, aliases


def aliasable_legacy_providers(
    providers: Iterable[str], current_provider: str
) -> tuple[set[str], set[str]]:
    legacy = {provider for provider in providers if provider != current_provider}
    reserved = legacy & RESERVED_PROVIDER_NAMES
    return legacy - reserved, reserved


def provider_alias_status(
    config_path: Path, current_provider: str, aliases: Iterable[str]
) -> tuple[list[str], list[str]]:
    aliases = sorted(set(aliases))
    if not aliases:
        return [], []
    if tomllib is None or not config_path.is_file():
        return [], aliases
    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        providers = parsed.get("model_providers") or {}
        current = providers.get(current_provider)
        if current is None and current_provider == "openai":
            current = builtin_openai_provider_config()
        if not isinstance(current, dict):
            return [], aliases
        configured: list[str] = []
        needed: list[str] = []
        for alias in aliases:
            expected = dict(current)
            expected["name"] = alias
            if providers.get(alias) == expected:
                configured.append(alias)
            else:
                needed.append(alias)
        return configured, needed
    except (OSError, ValueError):
        return [], aliases


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def atomic_write_bytes(path: Path, value: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def database_rank(path: Path) -> tuple[int, float]:
    match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
    version = int(match.group(1)) if match else -1
    return version, path.stat().st_mtime


def has_threads_table(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute(
                "select 1 from sqlite_master where type='table' and name='threads'"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def resolve_database(codex_home: Path, value: str | None) -> Path:
    if value:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise RepairError(f"Database does not exist: {path}")
        return path
    candidates = sorted(
        codex_home.glob("state_*.sqlite"), key=database_rank, reverse=True
    )
    for candidate in candidates:
        if has_threads_table(candidate):
            return candidate.resolve()
    raise RepairError(f"No supported state_*.sqlite database found in {codex_home}")


def open_database(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=10,
            factory=ManagedConnection,
        )
    else:
        conn = sqlite3.connect(path, timeout=10, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 10000")
    return conn


def validate_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("pragma table_info(threads)")}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise RepairError(
            "Unsupported threads schema; missing columns: " + ", ".join(sorted(missing))
        )


def integrity_check(conn: sqlite3.Connection) -> None:
    result = conn.execute("pragma integrity_check").fetchone()
    if not result or result[0] != "ok":
        detail = result[0] if result else "no result"
        raise RepairError(f"SQLite integrity check failed: {detail}")


def is_internal_source(source: str) -> bool:
    value = (source or "").strip()
    if not value.startswith("{"):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "subagent" in parsed


def load_threads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select id, rollout_path, source, model_provider, archived, archived_at
        from threads
        order by id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def rollout_provider_values(path_value: str, codex_home: Path | None = None) -> list[str]:
    path = resolve_rollout_path(path_value, codex_home)
    if not path.is_file():
        return []
    values: list[str] = []
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "session_meta":
                    continue
                payload = event.get("payload")
                if isinstance(payload, dict):
                    value = payload.get("model_provider")
                    if isinstance(value, str) and value:
                        values.append(value)
    except OSError:
        return []
    return values


def rollout_first_provider(
    path_value: str, codex_home: Path | None = None, max_bytes: int = 128 * 1024
) -> str | None:
    """Read only the metadata prefix used by the guard fast path.

    Codex writes session_meta near the start of a rollout. If a file does not
    expose it in the bounded prefix, return None so the caller falls back to a
    complete, conservative analysis instead of allowing a false launch.
    """
    path = resolve_rollout_path(path_value, codex_home)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            remaining = max_bytes
            for raw_line in handle:
                remaining -= len(raw_line)
                if remaining < 0:
                    break
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "session_meta":
                    continue
                payload = event.get("payload")
                value = payload.get("model_provider") if isinstance(payload, dict) else None
                return value if isinstance(value, str) and value else None
    except OSError:
        return None
    return None


def quick_rollout_check(
    user_rows: Iterable[dict[str, Any]], provider: str, codex_home: Path
) -> tuple[int, int, bool]:
    """Return (missing files, mismatched files, metadata_unknown).

    This checks file existence and only the bounded metadata prefix. It never
    walks conversation bodies on the no-change path.
    """
    missing = mismatched = 0
    unknown = False
    for row in user_rows:
        path = resolve_rollout_path(row["rollout_path"], codex_home)
        if not path.is_file():
            missing += 1
            continue
        value = rollout_first_provider(row["rollout_path"], codex_home)
        if value is None:
            unknown = True
        elif value != provider:
            mismatched += 1
    return missing, mismatched, unknown


def split_line_ending(raw_line: bytes) -> tuple[bytes, bytes]:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2], b"\r\n"
    if raw_line.endswith(b"\n") or raw_line.endswith(b"\r"):
        return raw_line[:-1], raw_line[-1:]
    return raw_line, b""


def prepare_rollout_rewrite(path: Path, provider: str) -> tuple[bytes, bytes, int]:
    before = path.read_bytes()
    output: list[bytes] = []
    changed_events = 0
    for raw_line in before.splitlines(keepends=True):
        content, ending = split_line_ending(raw_line)
        try:
            event = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            output.append(raw_line)
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        old_provider = (
            payload.get("model_provider") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(event, dict)
            or event.get("type") != "session_meta"
            or not isinstance(old_provider, str)
            or not old_provider
            or old_provider == provider
        ):
            output.append(raw_line)
            continue

        original_event = copy.deepcopy(event)
        payload["model_provider"] = provider
        rewritten = json.dumps(
            event, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        verified = json.loads(rewritten)
        original_event["payload"]["model_provider"] = provider
        if verified != original_event:
            raise RepairError(f"Rollout rewrite validation failed: {path}")
        output.append(rewritten + ending)
        changed_events += 1

    after = b"".join(output)
    if changed_events == 0 or after == before:
        raise RepairError(f"Expected provider metadata changes were not found: {path}")
    return before, after, changed_events


def rollout_provider_analysis(
    user_rows: Iterable[dict[str, Any]], provider: str, codex_home: Path | None = None
) -> tuple[Counter[str], list[dict[str, Any]], int]:
    counts: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []
    missing_meta = 0
    for row in user_rows:
        values = rollout_provider_values(row["rollout_path"], codex_home)
        if not values:
            missing_meta += 1
            continue
        counts[values[0]] += 1
        legacy_values = sorted({value for value in values if value != provider})
        if legacy_values:
            enriched = dict(row)
            enriched["rollout_provider"] = legacy_values[0]
            enriched["rollout_providers"] = legacy_values
            enriched["rollout_provider_events"] = len(values)
            enriched["rollout_provider_mismatch_events"] = sum(
                value != provider for value in values
            )
            mismatches.append(enriched)
    return counts, mismatches, missing_meta


def writer_lock_ids(codex_home: Path) -> set[str]:
    lock_directory = codex_home / "thread-writer-locks"
    if not lock_directory.is_dir():
        return set()
    return {path.stem for path in lock_directory.glob("*.lock") if path.is_file()}


def guard_state_path(codex_home: Path) -> Path:
    return codex_home / "history-repair-state" / "guard-state.json"


def read_guard_state(codex_home: Path) -> dict[str, Any] | None:
    path = guard_state_path(codex_home)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != GUARD_STATE_FIELDS:
        return None
    if not (
        isinstance(value.get("last_provider"), str)
        and isinstance(value.get("last_guard_time"), str)
        and isinstance(value.get("last_guard_version"), int)
        and isinstance(value.get("last_manifest"), (str, type(None)))
        and isinstance(value.get("last_result"), dict)
    ):
        return None
    return value


def rollout_fingerprints(
    user_rows: Iterable[dict[str, Any]], codex_home: Path
) -> dict[str, tuple[int, int]] | None:
    fingerprints: dict[str, tuple[int, int]] = {}
    try:
        for row in user_rows:
            path = resolve_rollout_path(row["rollout_path"], codex_home)
            stat_result = path.stat()
            if not path.is_file():
                return None
            fingerprints[row["id"]] = (stat_result.st_size, stat_result.st_mtime_ns)
    except OSError:
        return None
    return fingerprints


def guard_state_matches(
    state: dict[str, Any] | None,
    provider: str,
    user_rows: Iterable[dict[str, Any]],
    codex_home: Path,
) -> bool:
    if (
        not state
        or state["last_provider"] != provider
        or state["last_guard_version"] != SCRIPT_VERSION
        or state["last_result"].get("status") != "launch"
    ):
        return False
    previous = state["last_result"].get("rollouts")
    current = rollout_fingerprints(user_rows, codex_home)
    if current is None or not isinstance(previous, dict):
        return False
    normalized = {
        str(thread_id): (int(values[0]), int(values[1]))
        for thread_id, values in previous.items()
        if isinstance(values, list)
        and len(values) == 2
        and all(isinstance(item, int) for item in values)
    }
    return len(normalized) == len(previous) and normalized == current


def write_guard_state(
    codex_home: Path,
    provider: str,
    result: str,
    manifest: str | None = None,
    user_rows: Iterable[dict[str, Any]] | None = None,
) -> bool:
    fingerprints = rollout_fingerprints(user_rows or [], codex_home)
    payload = {
        "last_provider": provider,
        "last_guard_time": utc_now(),
        "last_guard_version": SCRIPT_VERSION,
        "last_manifest": manifest,
        "last_result": {
            "status": result,
            "rollouts": {
                thread_id: [size, modified]
                for thread_id, (size, modified) in (fingerprints or {}).items()
            },
        },
    }
    try:
        path = guard_state_path(codex_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return True
    except OSError:
        return False


def fast_guard_report(
    rows: Iterable[dict[str, Any]],
    provider: str,
    codex_home: Path,
    database: Path,
    missing_rollouts: int,
) -> dict[str, Any]:
    rows = list(rows)
    user_rows = [row for row in rows if not is_internal_source(row["source"])]
    internal_rows = [row for row in rows if is_internal_source(row["source"])]
    providers = Counter(row["model_provider"] for row in user_rows)
    return {
        "codex_home": str(codex_home),
        "database": str(database),
        "current_provider": provider,
        "integrity": "ok",
        "total_records": len(rows),
        "user_tasks": len(user_rows),
        "visible_provider_tasks": len(user_rows),
        "hidden_provider_tasks": 0,
        "archived_user_tasks": sum(bool(row["archived"]) for row in user_rows),
        "internal_subagents": len(internal_rows),
        "missing_rollout_files": missing_rollouts,
        "writer_locked_user_tasks": 0,
        "hidden_writer_locked_tasks": 0,
        "repairable_hidden_provider_tasks": 0,
        "providers": dict(sorted(providers.items())),
        "rollout_providers": {},
        "runtime_provider_mismatch_tasks": 0,
        "repairable_runtime_provider_tasks": 0,
        "writer_locked_runtime_provider_tasks": 0,
        "unresolved_runtime_provider_tasks": 0,
        "provider_aliases_configured": [],
        "provider_aliases_needed": [],
        "provider_aliases_skipped_reserved": [],
        "missing_session_meta": 0,
    }


def analyze(
    rows: Iterable[dict[str, Any]], provider: str, codex_home: Path, database: Path
) -> dict[str, Any]:
    rows = list(rows)
    user_rows = [row for row in rows if not is_internal_source(row["source"])]
    internal_rows = [row for row in rows if is_internal_source(row["source"])]
    hidden_rows = [row for row in user_rows if row["model_provider"] != provider]
    archived_rows = [row for row in user_rows if bool(row["archived"])]
    missing_rollouts = [
        row
        for row in user_rows
        if not resolve_rollout_path(row["rollout_path"], codex_home).is_file()
    ]
    locked_ids = writer_lock_ids(codex_home)
    locked_user_rows = [row for row in user_rows if row["id"] in locked_ids]
    locked_hidden_rows = [row for row in hidden_rows if row["id"] in locked_ids]
    repairable_hidden_rows = [
        row for row in hidden_rows if row["id"] not in locked_ids
    ]
    providers = Counter(row["model_provider"] for row in user_rows)
    rollout_providers, rollout_mismatches, missing_session_meta = (
        rollout_provider_analysis(user_rows, provider, codex_home)
    )
    mismatch_provider_names = {
        legacy_provider
        for row in rollout_mismatches
        for legacy_provider in row["rollout_providers"]
    }
    aliasable_providers, reserved_providers = aliasable_legacy_providers(
        mismatch_provider_names, provider
    )
    configured_aliases, needed_aliases = provider_alias_status(
        codex_home / "config.toml", provider, aliasable_providers
    )
    unresolved_runtime_rows = [
        row
        for row in rollout_mismatches
        if set(row["rollout_providers"]) & set(needed_aliases)
    ]
    locked_rollout_mismatches = [
        row for row in rollout_mismatches if row["id"] in locked_ids
    ]
    return {
        "codex_home": str(codex_home),
        "database": str(database),
        "current_provider": provider,
        "total_records": len(rows),
        "user_tasks": len(user_rows),
        "visible_provider_tasks": len(user_rows) - len(hidden_rows),
        "hidden_provider_tasks": len(hidden_rows),
        "archived_user_tasks": len(archived_rows),
        "internal_subagents": len(internal_rows),
        "missing_rollout_files": len(missing_rollouts),
        "writer_locked_user_tasks": len(locked_user_rows),
        "hidden_writer_locked_tasks": len(locked_hidden_rows),
        "repairable_hidden_provider_tasks": len(repairable_hidden_rows),
        "providers": dict(sorted(providers.items())),
        "rollout_providers": dict(sorted(rollout_providers.items())),
        "runtime_provider_mismatch_tasks": len(rollout_mismatches),
        "repairable_runtime_provider_tasks": len(rollout_mismatches)
        - len(locked_rollout_mismatches),
        "writer_locked_runtime_provider_tasks": len(locked_rollout_mismatches),
        "unresolved_runtime_provider_tasks": len(unresolved_runtime_rows),
        "provider_aliases_configured": configured_aliases,
        "provider_aliases_needed": needed_aliases,
        "provider_aliases_skipped_reserved": sorted(reserved_providers),
        "missing_session_meta": missing_session_meta,
    }


def history_is_consistent(report: dict[str, Any]) -> bool:
    return (
        report["hidden_provider_tasks"] == 0
        and report["runtime_provider_mismatch_tasks"] == 0
        and report["missing_rollout_files"] == 0
    )


def scan_next_action(report: dict[str, Any]) -> str:
    if report["missing_rollout_files"]:
        return "inspect_missing_rollouts"
    if (
        report["repairable_hidden_provider_tasks"]
        or report["repairable_runtime_provider_tasks"]
    ):
        return "repair"
    if (
        report["hidden_writer_locked_tasks"]
        or report["writer_locked_runtime_provider_tasks"]
    ):
        return "restart_and_rerun"
    return "none" if history_is_consistent(report) else "repair"


def repair_next_action(report: dict[str, Any], changed: bool) -> str:
    if report["missing_rollout_files"]:
        return "inspect_missing_rollouts"
    if (
        report["hidden_writer_locked_tasks"]
        or report["writer_locked_runtime_provider_tasks"]
    ):
        return "restart_and_rerun"
    if not history_is_consistent(report):
        return "repair_again"
    return "restart_to_reload" if changed else "none"


def backup_database(source: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(destination)) as backup_conn:
        source.backup(backup_conn)
        integrity_check(backup_conn)


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def create_backup(
    conn: sqlite3.Connection,
    codex_home: Path,
    database: Path,
    config_path: Path,
    manifest: dict[str, Any],
) -> Path:
    root = codex_home / "history-repair-backups"
    directory = root / f"{timestamp_slug()}-{uuid.uuid4().hex[:8]}"
    backup_database(conn, directory / database.name)
    if config_path.is_file():
        shutil.copy2(config_path, directory / "config.toml")
    write_manifest(directory / "manifest.json", manifest)
    return directory


def backup_rollout_rewrites(
    backup_dir: Path,
    rows: Iterable[dict[str, Any]],
    provider: str,
    codex_home: Path,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    rollout_backup_dir = backup_dir / "rollouts"
    for row in rows:
        if row["id"] in writer_lock_ids(codex_home):
            raise RepairError(
                "A rollout became writer-locked after scanning; rerun the repair"
            )
        path = resolve_rollout_path(row["rollout_path"], codex_home)
        before, after, changed_events = prepare_rollout_rewrite(path, provider)
        rollout_backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = rollout_backup_dir / f"{row['id']}.jsonl"
        shutil.copy2(path, backup_path)
        if backup_path.read_bytes() != before:
            raise RepairError(f"Rollout backup verification failed: {path}")
        entries.append(
            {
                "id": row["id"],
                "path": str(path),
                "backup": str(backup_path.relative_to(backup_dir)),
                "before_sha256": sha256_bytes(before),
                "after_sha256": sha256_bytes(after),
                "changed_session_meta_events": changed_events,
            }
        )
    return entries


def backup_rollout_snapshots(
    backup_dir: Path,
    rows: Iterable[dict[str, Any]],
    codex_home: Path,
) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    locked_ids = writer_lock_ids(codex_home)
    rollout_backup_dir = backup_dir / "rollouts"
    rollout_backup_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        path = resolve_rollout_path(row["rollout_path"], codex_home)
        if not path.is_file():
            continue
        snapshot = None
        for _ in range(3):
            before = path.read_bytes()
            after = path.read_bytes()
            if before == after:
                snapshot = before
                break
        if snapshot is None:
            raise RepairError(f"Rollout changed repeatedly during snapshot: {path}")
        backup_path = rollout_backup_dir / f"{row['id']}.jsonl"
        atomic_write_bytes(backup_path, snapshot)
        shutil.copystat(path, backup_path)
        if backup_path.read_bytes() != snapshot:
            raise RepairError(f"Rollout snapshot verification failed: {path}")
        digest = sha256_bytes(snapshot)
        entries.append(
            {
                "id": row["id"],
                "path": str(path),
                "backup": str(backup_path.relative_to(backup_dir)),
                "sha256": digest,
                "size": len(snapshot),
                "writer_locked_at_snapshot": row["id"] in locked_ids,
            }
        )
        total_bytes += len(snapshot)
    return entries, total_bytes


def apply_rollout_rewrites(
    entries: Iterable[dict[str, Any]],
    provider: str,
    written: list[dict[str, Any]],
    codex_home: Path,
) -> list[dict[str, Any]]:
    for entry in entries:
        if entry["id"] in writer_lock_ids(codex_home):
            raise RepairError(
                "A rollout became writer-locked after backup; no locked file was changed"
            )
        path = Path(entry["path"])
        before, after, changed_events = prepare_rollout_rewrite(path, provider)
        if sha256_bytes(before) != entry["before_sha256"]:
            raise RepairError(f"Concurrent rollout change detected: {path}")
        if (
            sha256_bytes(after) != entry["after_sha256"]
            or changed_events != entry["changed_session_meta_events"]
        ):
            raise RepairError(f"Rollout rewrite plan changed unexpectedly: {path}")
        if entry["id"] in writer_lock_ids(codex_home):
            raise RepairError(
                "A rollout became writer-locked before replacement; no locked file was changed"
            )
        atomic_write_bytes(path, after)
        if sha256_bytes(path.read_bytes()) != entry["after_sha256"]:
            raise RepairError(f"Rollout rewrite verification failed: {path}")
        written.append(entry)
    return written


def restore_written_rollouts(
    entries: Iterable[dict[str, Any]], backup_dir: Path
) -> None:
    for entry in reversed(list(entries)):
        path = Path(entry["path"])
        backup_path = backup_dir / entry["backup"]
        if not backup_path.is_file():
            continue
        if path.is_file() and sha256_bytes(path.read_bytes()) != entry["after_sha256"]:
            continue
        original = backup_path.read_bytes()
        if sha256_bytes(original) == entry["before_sha256"]:
            atomic_write_bytes(path, original)


def print_result(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        print(f"{key}: {value}")


def scan_command(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = resolve_codex_home(args.codex_home)
    database = resolve_database(codex_home, args.database)
    provider, provider_source = parse_provider(codex_home / "config.toml", args.provider)
    with open_database(database, readonly=True) as conn:
        validate_schema(conn)
        integrity_check(conn)
        report = analyze(load_threads(conn), provider, codex_home, database)
    next_action = scan_next_action(report)
    report.update(
        {
            "operation": "scan",
            "provider_source": provider_source,
            "integrity": "ok",
            "repair_complete": history_is_consistent(report),
            "next_action": next_action,
            "rerun_required_after_restart": next_action == "restart_and_rerun",
            "changed": False,
        }
    )
    return report


def codex_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 0 or '"Codex.exe"' in result.stdout


def doctor_command(args: argparse.Namespace) -> dict[str, Any]:
    report = scan_command(args)
    cc_switch = shutil.which("cc-switch") is not None
    schema_supported = True
    safe = (
        report["integrity"] == "ok"
        and report["missing_rollout_files"] == 0
        and not report["writer_locked_user_tasks"]
        and not codex_is_running()
    )
    report.update(
        {
            "operation": "doctor",
            "database_path": report["database"],
            "database_schema_supported": schema_supported,
            "sqlite_integrity": report["integrity"],
            "session_count": report["total_records"],
            "user_thread_count": report["user_tasks"],
            "archived_thread_count": report["archived_user_tasks"],
            "internal_thread_count": report["internal_subagents"],
            "missing_rollout_count": report["missing_rollout_files"],
            "writer_lock_count": report["writer_locked_user_tasks"],
            "provider_mismatch_count": report["runtime_provider_mismatch_tasks"],
            "cc_switch_detected": cc_switch,
            # Unified History is not exposed through a stable public setting.
            "cc_switch_unified_history_detected": None,
            "guard_installed": (codex_home := resolve_codex_home(args.codex_home))
            .joinpath("history-repair-state", "guard-installed.json")
            .is_file(),
            "guard_version": SCRIPT_VERSION,
            "safe_to_bootstrap": safe,
            "recommended_action": (
                "bootstrap" if safe else report["next_action"]
            ),
        }
    )
    return report


def guard_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise RepairError("Guard requires --yes")
    codex_home = resolve_codex_home(args.codex_home)
    database = resolve_database(codex_home, args.database)
    provider, provider_source = parse_provider(codex_home / "config.toml", args.provider)

    with open_database(database, readonly=True) as conn:
        validate_schema(conn)
        integrity_check(conn)
        rows = load_threads(conn)
    user_rows = [row for row in rows if not is_internal_source(row["source"])]
    if codex_is_running() or writer_lock_ids(codex_home):
        report = analyze(rows, provider, codex_home, database)
        write_guard_state(codex_home, provider, "quit_codex_and_retry", user_rows=user_rows)
        return {
            **report,
            "operation": "guard",
            "provider_source": provider_source,
            "changed": False,
            "guard_complete": False,
            "next_action": "quit_codex_and_retry",
        }
    sqlite_mismatch = any(row["model_provider"] != provider for row in user_rows)
    state = read_guard_state(codex_home)
    if (
        not sqlite_mismatch
        and guard_state_matches(state, provider, user_rows, codex_home)
    ):
        report = fast_guard_report(rows, provider, codex_home, database, 0)
        state_written = write_guard_state(
            codex_home, provider, "launch", user_rows=user_rows
        )
        return {
            **report,
            "operation": "guard",
            "provider_source": provider_source,
            "changed": False,
            "guard_complete": True,
            "next_action": "launch",
            "backup_directory": None,
            "repair_complete": True,
            "guard_state_written": state_written,
        }

    # A mismatch, missing metadata, or an unreadable prefix requires the full
    # analysis. This is the conservative path and is the only path allowed to
    # rewrite rollout contents.
    missing_rollouts, runtime_mismatches, unknown_runtime = quick_rollout_check(
        user_rows, provider, codex_home
    )
    report = analyze(rows, provider, codex_home, database)

    if report["missing_rollout_files"]:
        write_guard_state(
            codex_home, provider, "inspect_missing_rollouts", user_rows=user_rows
        )
        return {
            **report,
            "operation": "guard",
            "provider_source": provider_source,
            "changed": False,
            "guard_complete": False,
            "next_action": "inspect_missing_rollouts",
        }
    if report.get("missing_session_meta"):
        write_guard_state(
            codex_home, provider, "inspect_rollout_metadata", user_rows=user_rows
        )
        return {
            **report,
            "operation": "guard",
            "provider_source": provider_source,
            "changed": False,
            "guard_complete": False,
            "next_action": "inspect_rollout_metadata",
        }
    result = repair_command(
        argparse.Namespace(
            yes=True,
            codex_home=str(codex_home),
            database=str(database),
            provider=args.provider,
            unarchive=False,
            index_only=True,
        )
    )
    write_guard_state(
        codex_home,
        provider,
        "launch" if result.get("repair_complete") else result.get("next_action", "repair"),
        result.get("manifest"),
        user_rows=user_rows,
    )
    return {
        **result,
        "operation": "guard",
        "guard_complete": result.get("repair_complete", False),
        "next_action": "launch" if result.get("repair_complete") else result["next_action"],
    }


def bootstrap_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise RepairError("Bootstrap requires --yes")
    if os.name != "nt":
        raise RepairError("The automatic Codex Continuity launcher is currently Windows-only")
    codex_home = resolve_codex_home(args.codex_home)
    if codex_is_running():
        return {
            "operation": "bootstrap",
            "bootstrap_complete": False,
            "changed": False,
            "next_action": "quit_codex_and_retry",
        }

    initial = scan_command(args)
    if initial["missing_rollout_files"]:
        return {
            **initial,
            "operation": "bootstrap",
            "bootstrap_complete": False,
            "changed": False,
            "next_action": "inspect_missing_rollouts",
        }
    if initial["writer_locked_user_tasks"]:
        return {
            **initial,
            "operation": "bootstrap",
            "bootstrap_complete": False,
            "changed": False,
            "next_action": "quit_codex_and_retry",
        }

    snapshot = snapshot_command(
        argparse.Namespace(
            yes=True,
            codex_home=str(codex_home),
            database=args.database,
            provider=args.provider,
        )
    )
    if not snapshot.get("snapshot_complete"):
        return {
            **snapshot,
            "operation": "bootstrap",
            "bootstrap_complete": False,
            "next_action": "inspect_missing_rollouts",
        }

    repair = repair_command(
        argparse.Namespace(
            yes=True,
            codex_home=str(codex_home),
            database=args.database,
            provider=args.provider,
            unarchive=False,
            index_only=True,
        )
    )
    if not repair.get("repair_complete"):
        return {
            **repair,
            "operation": "bootstrap",
            "snapshot_directory": snapshot["backup_directory"],
            "bootstrap_complete": False,
            "next_action": repair["next_action"],
        }

    installer = Path(__file__).resolve().with_name("install_windows.ps1")
    if not installer.is_file():
        raise RepairError(f"Windows installer is missing: {installer}")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer),
        "-CodexHome",
        str(codex_home),
    ]
    if args.codex_app_id:
        command.extend(["-CodexAppId", args.codex_app_id])
    installed = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    if installed.returncode != 0:
        return {
            **repair,
            "operation": "bootstrap",
            "snapshot_directory": snapshot["backup_directory"],
            "bootstrap_complete": False,
            "guard_installed": False,
            "installer_output": installed.stdout[-2000:],
            "installer_error": installed.stderr[-2000:],
            "next_action": "install_guard",
        }

    verification = scan_command(
        argparse.Namespace(
            codex_home=str(codex_home), database=args.database, provider=args.provider
        )
    )
    marker = codex_home / "history-repair-state" / "guard-installed.json"
    complete = verification["repair_complete"] and marker.is_file()
    return {
        **verification,
        "operation": "bootstrap",
        "bootstrap_complete": complete,
        "snapshot_directory": snapshot["backup_directory"],
        "repair_backup_directory": repair.get("backup_directory"),
        "guard_installed": marker.is_file(),
        "next_action": "none" if complete else "install_guard",
        "changed": snapshot.get("changed", False) or repair.get("changed", False),
    }


def handoff_text(content: Any, allowed_types: set[str]) -> list[str]:
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in allowed_types:
            continue
        value = item.get("text")
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return texts


def handoff_event_text(event: dict[str, Any]) -> tuple[str, str] | None:
    """Extract readable user/assistant text from known rollout event forms."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = event.get("type")
    payload_type = payload.get("type")
    if event_type == "event_msg":
        if payload_type == "user_message":
            role = "user"
        elif payload_type in {"agent_message", "assistant_message"}:
            role = "assistant"
        else:
            return None
        value = payload.get("message")
        if isinstance(value, str) and value.strip():
            return role, value.strip()
        return None
    if event_type != "response_item" or payload_type != "message":
        return None
    role = payload.get("role")
    if role == "user":
        text = "\n".join(handoff_text(payload.get("content"), {"input_text"}))
    elif role == "assistant":
        text = "\n".join(handoff_text(payload.get("content"), {"output_text"}))
    else:
        return None
    return (role, text) if text else None


def handoff_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_messages < 1:
        raise RepairError("--max-messages must be at least 1")
    codex_home = resolve_codex_home(args.codex_home)
    database = resolve_database(codex_home, args.database)
    with open_database(database, readonly=True) as conn:
        validate_schema(conn)
        rows = load_threads(conn)
    row = next(
        (
            item
            for item in rows
            if item["id"] == args.thread and not is_internal_source(item["source"])
        ),
        None,
    )
    if row is None:
        raise RepairError(f"User thread not found: {args.thread}")
    path = resolve_rollout_path(row["rollout_path"], codex_home)
    if not path.is_file():
        raise RepairError(f"Rollout file is missing: {path}")

    messages: list[tuple[str, str]] = []
    metadata: dict[str, Any] = {}
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "session_meta":
                for key in ("id", "cwd", "model", "model_provider"):
                    if isinstance(payload.get(key), (str, int, float)):
                        metadata[key] = payload[key]
                continue
            extracted = handoff_event_text(event)
            if extracted:
                messages.append(extracted)

    if not messages:
        raise RepairError("No readable user/assistant text was found for this thread")
    created_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = codex_home / "history-repair-handoffs" / args.thread / created_at
    directory.mkdir(parents=True, exist_ok=False)
    metadata_path = directory / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {"thread_id": args.thread, "created_at": utc_now(), **metadata},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sections = [
        "# Continuity handoff",
        "",
        f"- Source thread: `{args.thread}`",
        f"- CWD: `{metadata.get('cwd', '')}`",
        f"- Model: `{metadata.get('model', '')}`",
        "",
        "> This handoff includes readable user and assistant text only. It excludes tool output, reasoning events, encrypted content, and credentials.",
        "",
        "## Recent conversation",
    ]
    for role, text in messages[-args.max_messages :]:
        sections.extend(["", f"### {role.title()}", "", text])
    handoff_path = directory / "HANDOFF.md"
    handoff_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    source_manifest_path = directory / "source-manifest.json"
    source_manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "created_at": utc_now(),
                "thread_id": args.thread,
                "source_rollout": str(path),
                "source_rollout_sha256": sha256_file(path),
                "source_rollout_size": path.stat().st_size,
                "source_metadata": metadata,
                "readable_message_count": len(messages),
                "exported_message_count": min(len(messages), args.max_messages),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "operation": "handoff",
        "thread_id": args.thread,
        "message_count": min(len(messages), args.max_messages),
        "handoff": str(handoff_path),
        "metadata": str(metadata_path),
        "source_manifest": str(source_manifest_path),
        "original_thread_preserved": True,
        "changed": True,
    }


def snapshot_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise RepairError("Snapshot requires --yes")
    codex_home = resolve_codex_home(args.codex_home)
    database = resolve_database(codex_home, args.database)
    config_path = codex_home / "config.toml"
    provider, provider_source = parse_provider(config_path, args.provider)

    with open_database(database, readonly=True) as conn:
        validate_schema(conn)
        integrity_check(conn)
        rows = load_threads(conn)
        user_rows = [row for row in rows if not is_internal_source(row["source"])]
        report = analyze(rows, provider, codex_home, database)
        manifest = {
            "manifest_version": 3,
            "script_version": SCRIPT_VERSION,
            "created_at": utc_now(),
            "operation": "snapshot",
            "database": str(database),
            "database_name": database.name,
            "provider": provider,
            "provider_source": provider_source,
            "rollouts": [],
            "rows": [],
        }
        backup_dir = create_backup(
            conn, codex_home, database, config_path, manifest
        )

    rollout_entries, total_bytes = backup_rollout_snapshots(
        backup_dir, user_rows, codex_home
    )
    manifest["rollouts"] = rollout_entries
    manifest["database_sha256"] = sha256_file(backup_dir / database.name)
    backup_config = backup_dir / "config.toml"
    manifest["config_sha256"] = (
        sha256_file(backup_config) if backup_config.is_file() else None
    )
    write_manifest(backup_dir / "manifest.json", manifest)

    next_action = scan_next_action(report)
    snapshot_complete = (
        report["missing_rollout_files"] == 0
        and len(rollout_entries) == len(user_rows)
    )
    report.update(
        {
            "operation": "snapshot",
            "provider_source": provider_source,
            "snapshot_complete": snapshot_complete,
            "rollout_files_backed_up": len(rollout_entries),
            "writer_locked_rollouts_captured": sum(
                bool(entry["writer_locked_at_snapshot"])
                for entry in rollout_entries
            ),
            "snapshot_bytes": total_bytes,
            "backup_directory": str(backup_dir),
            "manifest": str(backup_dir / "manifest.json"),
            "repair_complete": history_is_consistent(report),
            "next_action": next_action,
            "rerun_required_after_restart": next_action
            == "restart_and_rerun",
            "integrity": "ok",
            "backup_created": True,
            "changed": False,
        }
    )
    return report


def repair_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise RepairError("Repair requires --yes after reviewing a scan")
    codex_home = resolve_codex_home(args.codex_home)
    database = resolve_database(codex_home, args.database)
    config_path = codex_home / "config.toml"
    provider, provider_source = parse_provider(config_path, args.provider)

    with open_database(database) as conn:
        validate_schema(conn)
        integrity_check(conn)
        rows = load_threads(conn)
        user_rows = [row for row in rows if not is_internal_source(row["source"])]
        _, rollout_mismatch_rows, _ = rollout_provider_analysis(
            user_rows, provider, codex_home
        )
        discovered_legacy_providers = {
            legacy_provider
            for row in rollout_mismatch_rows
            for legacy_provider in row["rollout_providers"]
        }
        legacy_providers, reserved_legacy_providers = aliasable_legacy_providers(
            discovered_legacy_providers, provider
        )
        locked_ids = writer_lock_ids(codex_home)
        rollout_rows = [
            row for row in rollout_mismatch_rows if row["id"] not in locked_ids
        ]
        locked_rollout_rows = [
            row for row in rollout_mismatch_rows if row["id"] in locked_ids
        ]
        all_provider_rows = [
            row for row in user_rows if row["model_provider"] != provider
        ]
        provider_rows = [
            row for row in all_provider_rows if row["id"] not in locked_ids
        ]
        locked_provider_rows = [
            row for row in all_provider_rows if row["id"] in locked_ids
        ]
        archive_rows = [
            row
            for row in user_rows
            if args.unarchive and row["archived"] and row["id"] not in locked_ids
        ]
        provider_row_ids = {row["id"] for row in provider_rows}
        archive_row_ids = {row["id"] for row in archive_rows}
        changed_ids = {row["id"] for row in provider_rows} | {
            row["id"] for row in archive_rows
        } | {row["id"] for row in rollout_rows}
        changed_rows = [row for row in user_rows if row["id"] in changed_ids]
        config_before = config_path.read_text(encoding="utf-8-sig")
        config_after = config_before
        aliases: list[str] = []
        if not args.index_only and legacy_providers:
            config_before, config_after, aliases = build_provider_alias_config(
                config_path, provider, legacy_providers
            )
        config_changed = config_after != config_before
        skipped_locked_count = len(
            {row["id"] for row in locked_provider_rows + locked_rollout_rows}
        )

        if not changed_rows and not config_changed:
            report = analyze(rows, provider, codex_home, database)
            next_action = repair_next_action(report, changed=False)
            report.update(
                {
                    "operation": "repair",
                    "provider_source": provider_source,
                    "provider_rows_changed": 0,
                    "archived_rows_unarchived": 0,
                    "rollout_files_changed": 0,
                    "session_meta_events_changed": 0,
                    "writer_locked_tasks_skipped": skipped_locked_count,
                    "migrated_writer_locked_tasks": 0,
                    "provider_aliases_synced": [],
                    "provider_aliases_skipped_reserved": sorted(
                        reserved_legacy_providers
                    ),
                    "repair_complete": history_is_consistent(report),
                    "next_action": next_action,
                    "rerun_required_after_restart": next_action
                    == "restart_and_rerun",
                    "restart_required": next_action
                    in {"restart_and_rerun", "restart_to_reload"},
                    "backup_directory": None,
                    "manifest": None,
                    "integrity": "ok",
                    "changed": False,
                }
            )
            return report

        manifest = {
            "manifest_version": 3,
            "script_version": SCRIPT_VERSION,
            "created_at": utc_now(),
            "operation": "repair",
            "database": str(database),
            "database_name": database.name,
            "provider": provider,
            "provider_source": provider_source,
            "unarchive": bool(args.unarchive),
            "config": {
                "changed": config_changed,
                "aliases": aliases,
                "before_sha256": sha256_text(config_before),
                "after_sha256": sha256_text(config_after),
            },
            "rollouts": [],
            "rows": [
                {
                    "id": row["id"],
                    "old_provider": row["model_provider"],
                    "new_provider": provider
                    if row["id"] in provider_row_ids
                    else row["model_provider"],
                    "old_archived": int(row["archived"]),
                    "new_archived": 0
                    if row["id"] in archive_row_ids
                    else int(row["archived"]),
                    "old_archived_at": row["archived_at"],
                }
                for row in changed_rows
            ],
        }
        backup_dir = create_backup(conn, codex_home, database, config_path, manifest)
        rollout_entries = backup_rollout_rewrites(
            backup_dir, rollout_rows, provider, codex_home
        )
        manifest["rollouts"] = rollout_entries
        write_manifest(backup_dir / "manifest.json", manifest)

        config_written = False
        written_rollouts: list[dict[str, Any]] = []
        try:
            if changed_ids & writer_lock_ids(codex_home):
                raise RepairError(
                    "A task became writer-locked after backup; rerun the repair"
                )
            apply_rollout_rewrites(
                rollout_entries, provider, written_rollouts, codex_home
            )
            database_change_ids = provider_row_ids | archive_row_ids
            if database_change_ids & writer_lock_ids(codex_home):
                raise RepairError(
                    "A database task became writer-locked before update; repair rolled back"
                )
            conn.execute("begin immediate")
            for row in provider_rows:
                cursor = conn.execute(
                    """
                    update threads set model_provider = ?
                    where id = ? and model_provider = ?
                    """,
                    (provider, row["id"], row["model_provider"]),
                )
                if cursor.rowcount != 1:
                    raise RepairError(f"Concurrent change detected for thread {row['id']}")
            for row in archive_rows:
                conn.execute(
                    "update threads set archived = 0, archived_at = null where id = ?",
                    (row["id"],),
                )
            if config_changed:
                atomic_write_text(config_path, config_after)
                config_written = True
            conn.commit()
        except Exception:
            conn.rollback()
            if config_written:
                atomic_write_text(config_path, config_before)
            restore_written_rollouts(written_rollouts, backup_dir)
            raise

        integrity_check(conn)
        report = analyze(load_threads(conn), provider, codex_home, database)

    changed = bool(changed_rows or rollout_entries or config_changed)
    next_action = repair_next_action(report, changed)
    report.update(
        {
            "operation": "repair",
            "provider_source": provider_source,
            "provider_rows_changed": len(provider_rows),
            "archived_rows_unarchived": len(archive_rows),
            "rollout_files_changed": len(rollout_entries),
            "session_meta_events_changed": sum(
                entry["changed_session_meta_events"] for entry in rollout_entries
            ),
            "writer_locked_tasks_skipped": skipped_locked_count,
            "migrated_writer_locked_tasks": 0,
            "provider_aliases_synced": aliases,
            "provider_aliases_skipped_reserved": sorted(reserved_legacy_providers),
            "repair_complete": history_is_consistent(report),
            "next_action": next_action,
            "rerun_required_after_restart": next_action == "restart_and_rerun",
            "restart_required": next_action
            in {"restart_and_rerun", "restart_to_reload"},
            "backup_directory": str(backup_dir),
            "manifest": str(backup_dir / "manifest.json"),
            "integrity": "ok",
            "changed": changed,
        }
    )
    return report


def resolve_manifest(codex_home: Path, value: str | None, latest: bool) -> Path:
    if value:
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            path = path / "manifest.json"
        if not path.is_file():
            raise RepairError(f"Backup manifest does not exist: {path}")
        return path
    if latest:
        candidates = sorted(
            (codex_home / "history-repair-backups").glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
        raise RepairError("No history repair backup manifest was found")
    raise RepairError("Undo requires --backup PATH or --latest")


def undo_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise RepairError("Undo requires --yes")
    codex_home = resolve_codex_home(args.codex_home)
    manifest_path = resolve_manifest(codex_home, args.backup, args.latest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") not in {1, 2, 3}:
        raise RepairError("Unsupported backup manifest version")
    if manifest.get("operation") not in {None, "repair"}:
        raise RepairError(
            "Undo requires a repair manifest; snapshots are preserved as recovery copies"
        )
    database = resolve_database(codex_home, args.database)

    restored = 0
    skipped = 0
    rollouts_restored = 0
    rollouts_skipped = 0
    config_restored = False
    config_restore_skipped = False
    config_path = codex_home / "config.toml"
    current_config_text = (
        config_path.read_text(encoding="utf-8-sig") if config_path.is_file() else None
    )
    config_info = manifest.get("config") or {}
    restore_config = bool(config_info.get("changed"))
    original_config = None
    if restore_config:
        backup_config = manifest_path.parent / "config.toml"
        if not backup_config.is_file() or not config_path.is_file():
            config_restore_skipped = True
        elif sha256_text(config_path.read_text(encoding="utf-8-sig")) != config_info.get(
            "after_sha256"
        ):
            config_restore_skipped = True
        else:
            original_config = backup_config.read_text(encoding="utf-8-sig")
    with open_database(database) as conn:
        validate_schema(conn)
        integrity_check(conn)
        pre_undo_path = manifest_path.parent / f"pre-undo-{timestamp_slug()}.sqlite"
        if pre_undo_path.exists():
            pre_undo_path = manifest_path.parent / (
                f"pre-undo-{timestamp_slug()}-{uuid.uuid4().hex[:6]}.sqlite"
            )
        with closing(sqlite3.connect(pre_undo_path)) as backup_conn:
            conn.backup(backup_conn)
            integrity_check(backup_conn)
        if config_path.is_file():
            shutil.copy2(
                config_path,
                manifest_path.parent / f"pre-undo-config-{timestamp_slug()}.toml",
            )

        manifest_rows = manifest.get("rows", [])
        eligible_row_ids: set[str] = set()
        for row in manifest_rows:
            current = conn.execute(
                "select model_provider, archived from threads where id = ?",
                (row["id"],),
            ).fetchone()
            if current is None or (
                current["model_provider"] != row["new_provider"]
                or int(current["archived"]) != int(row["new_archived"])
            ):
                skipped += 1
                continue
            eligible_row_ids.add(row["id"])

        locked_ids = writer_lock_ids(codex_home)
        rollout_entries = manifest.get("rollouts") or []
        pre_undo_rollout_dir = manifest_path.parent / (
            f"pre-undo-rollouts-{timestamp_slug()}-{uuid.uuid4().hex[:6]}"
        )
        eligible_rollouts: list[dict[str, Any]] = []
        manifest_row_ids = {row["id"] for row in manifest_rows}
        for entry in rollout_entries:
            path = Path(entry["path"])
            backup_path = manifest_path.parent / entry["backup"]
            row_is_safe = (
                entry["id"] not in manifest_row_ids
                or entry["id"] in eligible_row_ids
            )
            if (
                entry["id"] in locked_ids
                or not row_is_safe
                or not path.is_file()
                or not backup_path.is_file()
                or sha256_bytes(path.read_bytes()) != entry.get("after_sha256")
                or sha256_bytes(backup_path.read_bytes())
                != entry.get("before_sha256")
            ):
                rollouts_skipped += 1
                if entry["id"] in eligible_row_ids:
                    skipped += 1
                eligible_row_ids.discard(entry["id"])
                continue
            pre_undo_rollout_dir.mkdir(parents=True, exist_ok=True)
            pre_undo_rollout_path = pre_undo_rollout_dir / f"{entry['id']}.jsonl"
            shutil.copy2(path, pre_undo_rollout_path)
            prepared = dict(entry)
            prepared["pre_undo_path"] = str(pre_undo_rollout_path)
            eligible_rollouts.append(prepared)

        config_written = False
        restored_rollouts: list[dict[str, Any]] = []
        try:
            conn.execute("begin immediate")
            for entry in eligible_rollouts:
                path = Path(entry["path"])
                original = (manifest_path.parent / entry["backup"]).read_bytes()
                atomic_write_bytes(path, original)
                if sha256_bytes(path.read_bytes()) != entry["before_sha256"]:
                    raise RepairError(f"Rollout undo verification failed: {path}")
                restored_rollouts.append(entry)
            for row in manifest_rows:
                if row["id"] not in eligible_row_ids:
                    continue
                conn.execute(
                    """
                    update threads
                    set model_provider = ?, archived = ?, archived_at = ?
                    where id = ?
                    """,
                    (
                        row["old_provider"],
                        row["old_archived"],
                        row["old_archived_at"],
                        row["id"],
                    ),
                )
                restored += 1
            if original_config is not None:
                atomic_write_text(config_path, original_config)
                config_written = True
            conn.commit()
            config_restored = config_written
            rollouts_restored = len(restored_rollouts)
        except Exception:
            conn.rollback()
            for entry in reversed(restored_rollouts):
                current_path = Path(entry["path"])
                pre_undo_bytes = Path(entry["pre_undo_path"]).read_bytes()
                if sha256_bytes(pre_undo_bytes) == entry["after_sha256"]:
                    atomic_write_bytes(current_path, pre_undo_bytes)
            if config_written:
                if current_config_text is not None:
                    atomic_write_text(config_path, current_config_text)
            raise
        integrity_check(conn)

    return {
        "operation": "undo",
        "database": str(database),
        "manifest": str(manifest_path),
        "rows_restored": restored,
        "rows_skipped_due_to_newer_changes": skipped,
        "rollout_files_restored": rollouts_restored,
        "rollout_files_skipped_due_to_locks_or_newer_changes": rollouts_skipped,
        "provider_alias_config_restored": config_restored,
        "provider_alias_config_restore_skipped": config_restore_skipped,
        "pre_undo_backup": str(pre_undo_path),
        "integrity": "ok",
        "changed": restored > 0 or rollouts_restored > 0 or config_restored,
    }


def add_location_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", help="Override the Codex home directory")
    parser.add_argument("--database", help="Override the state SQLite database")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely restore local Codex task visibility after provider changes"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Inspect local history without changes")
    add_location_arguments(scan)
    scan.add_argument("--provider", help="Override the target model provider")
    scan.add_argument("--json", action="store_true", help="Print JSON output")

    snapshot = subparsers.add_parser(
        "snapshot", help="Back up all local user task rollouts without changing them"
    )
    add_location_arguments(snapshot)
    snapshot.add_argument("--provider", help="Override the provider used for the audit")
    snapshot.add_argument("--yes", action="store_true", help="Confirm the snapshot")
    snapshot.add_argument("--json", action="store_true", help="Print JSON output")

    repair = subparsers.add_parser("repair", help="Back up and repair local history")
    add_location_arguments(repair)
    repair.add_argument("--provider", help="Override the target model provider")
    repair.add_argument(
        "--unarchive", action="store_true", help="Return archived user tasks to the sidebar"
    )
    repair.add_argument(
        "--index-only",
        action="store_true",
        help="Skip compatibility aliases for legacy runtime providers",
    )
    repair.add_argument("--yes", action="store_true", help="Confirm the repair")
    repair.add_argument("--json", action="store_true", help="Print JSON output")

    doctor = subparsers.add_parser("doctor", help="Read-only continuity diagnostics")
    add_location_arguments(doctor)
    doctor.add_argument("--provider", help="Override the target model provider")
    doctor.add_argument("--json", action="store_true", help="Print JSON output")

    guard = subparsers.add_parser(
        "guard", help="Synchronize metadata before launching Codex"
    )
    add_location_arguments(guard)
    guard.add_argument("--provider", help="Override the target model provider")
    guard.add_argument("--yes", action="store_true", help="Confirm the guard")
    guard.add_argument("--json", action="store_true", help="Print JSON output")

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Snapshot history and install the Windows launcher"
    )
    add_location_arguments(bootstrap)
    bootstrap.add_argument("--provider", help="Override the target model provider")
    bootstrap.add_argument("--codex-app-id", help="Optional Codex AppUserModelID")
    bootstrap.add_argument("--yes", action="store_true", help="Confirm bootstrap")
    bootstrap.add_argument("--json", action="store_true", help="Print JSON output")

    handoff = subparsers.add_parser(
        "handoff", help="Export readable conversation context to a new-thread brief"
    )
    add_location_arguments(handoff)
    handoff.add_argument("--thread", required=True, help="Source thread id")
    handoff.add_argument("--max-messages", type=int, default=40)
    handoff.add_argument("--json", action="store_true", help="Print JSON output")

    undo = subparsers.add_parser("undo", help="Undo a repair from its manifest")
    add_location_arguments(undo)
    undo_group = undo.add_mutually_exclusive_group(required=True)
    undo_group.add_argument("--backup", help="Backup directory or manifest path")
    undo_group.add_argument("--latest", action="store_true", help="Use the latest manifest")
    undo.add_argument("--yes", action="store_true", help="Confirm the undo")
    undo.add_argument("--json", action="store_true", help="Print JSON output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "scan":
            result = scan_command(args)
        elif args.command == "snapshot":
            result = snapshot_command(args)
        elif args.command == "repair":
            result = repair_command(args)
        elif args.command == "doctor":
            result = doctor_command(args)
        elif args.command == "guard":
            result = guard_command(args)
        elif args.command == "bootstrap":
            result = bootstrap_command(args)
        elif args.command == "handoff":
            result = handoff_command(args)
        else:
            result = undo_command(args)
        print_result(result, args.json)
        return 0
    except (RepairError, sqlite3.Error, OSError, ValueError) as error:
        payload = {"operation": args.command, "error": str(error), "changed": False}
        print_result(payload, getattr(args, "json", False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
