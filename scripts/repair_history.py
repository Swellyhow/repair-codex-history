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
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = 5
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
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(path, timeout=10)
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


def rollout_provider_values(path_value: str) -> list[str]:
    path = Path(path_value).expanduser()
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
    user_rows: Iterable[dict[str, Any]], provider: str
) -> tuple[Counter[str], list[dict[str, Any]], int]:
    counts: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []
    missing_meta = 0
    for row in user_rows:
        values = rollout_provider_values(row["rollout_path"])
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


def analyze(
    rows: Iterable[dict[str, Any]], provider: str, codex_home: Path, database: Path
) -> dict[str, Any]:
    rows = list(rows)
    user_rows = [row for row in rows if not is_internal_source(row["source"])]
    internal_rows = [row for row in rows if is_internal_source(row["source"])]
    hidden_rows = [row for row in user_rows if row["model_provider"] != provider]
    archived_rows = [row for row in user_rows if bool(row["archived"])]
    missing_rollouts = [
        row for row in user_rows if not Path(row["rollout_path"]).expanduser().is_file()
    ]
    locked_ids = writer_lock_ids(codex_home)
    locked_user_rows = [row for row in user_rows if row["id"] in locked_ids]
    locked_hidden_rows = [row for row in hidden_rows if row["id"] in locked_ids]
    repairable_hidden_rows = [
        row for row in hidden_rows if row["id"] not in locked_ids
    ]
    providers = Counter(row["model_provider"] for row in user_rows)
    rollout_providers, rollout_mismatches, missing_session_meta = (
        rollout_provider_analysis(user_rows, provider)
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
    with sqlite3.connect(destination) as backup_conn:
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
        path = Path(row["rollout_path"]).expanduser().resolve()
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
        path = Path(row["rollout_path"]).expanduser().resolve()
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
        _, rollout_mismatch_rows, _ = rollout_provider_analysis(user_rows, provider)
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
        with sqlite3.connect(pre_undo_path) as backup_conn:
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
