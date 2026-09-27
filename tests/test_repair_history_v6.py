import importlib.util
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_history.py"
SPEC = importlib.util.spec_from_file_location("repair_history", SCRIPT)
assert SPEC and SPEC.loader
repair_history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_history)


THREAD_SCHEMA = """
create table threads (
    id text primary key,
    rollout_path text not null,
    source text not null,
    model_provider text not null,
    archived integer not null default 0,
    archived_at text
)
"""


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )


def create_codex_home(
    root: Path,
    *,
    thread_id: str = "thread-1",
    provider: str = "custom",
    stored_provider: str | None = None,
    rollout_events: list[dict] | None = None,
    source: str = "{}",
    rollout_exists: bool = True,
) -> tuple[Path, Path, Path]:
    home = root / "codex"
    home.mkdir()
    database = home / "state_1.sqlite"
    rollout = home / "rollouts" / f"{thread_id}.jsonl"
    rollout.parent.mkdir()
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript(THREAD_SCHEMA)
        conn.execute(
            "insert into threads (id, rollout_path, source, model_provider, archived, archived_at) "
            "values (?, ?, ?, ?, 0, null)",
            (thread_id, str(rollout), source, stored_provider or provider),
        )
        conn.commit()
    (home / "config.toml").write_text(
        "model_provider = \"{}\"\n\n[model_providers.{}]\nname = \"{}\"\nbase_url = \"https://example.test/v1\"\n".format(
            provider, provider, provider
        ),
        encoding="utf-8",
    )
    if rollout_exists:
        write_jsonl(
            rollout,
            rollout_events
            or [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "cwd": "C:/work",
                        "model": "gpt-test",
                        "model_provider": stored_provider or provider,
                    },
                }
            ],
        )
    return home, database, rollout


def command_args(home: Path, database: Path, **kwargs: object) -> Namespace:
    values = {
        "codex_home": str(home),
        "database": str(database),
        "provider": None,
        "yes": True,
        "index_only": True,
        "unarchive": False,
    }
    values.update(kwargs)
    return Namespace(**values)


class V6RepairHistoryTests(unittest.TestCase):
    def test_prepare_rollout_rewrite_changes_only_session_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            original_message = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "keep this"}],
                },
            }
            write_jsonl(
                path,
                [
                    {
                        "type": "session_meta",
                        "payload": {"model_provider": "legacy", "id": "x"},
                    },
                    original_message,
                    {"type": "malformed", "payload": {"x": 1}},
                ],
            )
            before, after, changed = repair_history.prepare_rollout_rewrite(path, "custom")

            self.assertEqual(changed, 1)
            self.assertNotEqual(before, after)
            rewritten = [json.loads(line) for line in after.splitlines()]
            self.assertEqual(rewritten[0]["payload"]["model_provider"], "custom")
            self.assertEqual(rewritten[1], original_message)
            self.assertEqual(rewritten[2], {"type": "malformed", "payload": {"x": 1}})

    def test_doctor_reports_missing_rollout_and_blocks_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, database, _ = create_codex_home(
                Path(directory), rollout_exists=False
            )
            args = command_args(home, database)
            with patch.object(repair_history, "codex_is_running", return_value=False):
                result = repair_history.doctor_command(args)

            self.assertEqual(result["operation"], "doctor")
            self.assertEqual(result["sqlite_integrity"], "ok")
            self.assertEqual(result["missing_rollout_count"], 1)
            self.assertFalse(result["safe_to_bootstrap"])
            self.assertEqual(result["recommended_action"], "inspect_missing_rollouts")

    def test_guard_refuses_to_touch_locked_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, database, _ = create_codex_home(
                Path(directory), stored_provider="legacy"
            )
            locks = home / "thread-writer-locks"
            locks.mkdir()
            (locks / "thread-1.lock").write_text("", encoding="utf-8")
            args = command_args(home, database)
            with patch.object(repair_history, "codex_is_running", return_value=False):
                result = repair_history.guard_command(args)

            self.assertFalse(result["guard_complete"])
            self.assertFalse(result["changed"])
            self.assertEqual(result["next_action"], "quit_codex_and_retry")
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(
                    conn.execute("select model_provider from threads where id = 'thread-1'").fetchone()[0],
                    "legacy",
                )

    def test_guard_repairs_index_and_rollout_then_allows_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, database, rollout = create_codex_home(
                Path(directory), stored_provider="legacy"
            )
            args = command_args(home, database)
            with patch.object(repair_history, "codex_is_running", return_value=False):
                result = repair_history.guard_command(args)

            self.assertEqual(result["operation"], "guard")
            self.assertTrue(result["guard_complete"])
            self.assertTrue(result["repair_complete"])
            self.assertEqual(result["next_action"], "launch")
            self.assertEqual(result["provider_rows_changed"], 1)
            self.assertEqual(result["rollout_files_changed"], 1)
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(
                    conn.execute("select model_provider from threads where id = 'thread-1'").fetchone()[0],
                    "custom",
                )
            events = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["payload"]["model_provider"], "custom")

    def test_repair_alias_and_undo_preserve_v5_rollback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, database, rollout = create_codex_home(
                Path(directory), stored_provider="legacy"
            )
            repair = repair_history.repair_command(
                command_args(home, database, index_only=False)
            )

            self.assertTrue(repair["repair_complete"])
            self.assertEqual(repair["provider_aliases_synced"], ["legacy"])
            self.assertIsNotNone(repair["backup_directory"])
            config_after = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[model_providers.legacy]", config_after)
            self.assertEqual(
                json.loads(rollout.read_text(encoding="utf-8").splitlines()[0])["payload"][
                    "model_provider"
                ],
                "custom",
            )

            undone = repair_history.undo_command(
                Namespace(
                    codex_home=str(home),
                    database=str(database),
                    backup=repair["backup_directory"],
                    latest=False,
                    yes=True,
                )
            )
            self.assertEqual(undone["integrity"], "ok")
            self.assertEqual(undone["rows_restored"], 1)
            self.assertEqual(undone["rollout_files_restored"], 1)
            self.assertTrue(undone["provider_alias_config_restored"])
            self.assertNotIn("[model_providers.legacy]", (home / "config.toml").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(
                    conn.execute("select model_provider from threads where id = 'thread-1'").fetchone()[0],
                    "legacy",
                )

    def test_handoff_exports_readable_messages_and_excludes_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-1",
                        "cwd": "C:/project",
                        "model": "gpt-test",
                        "model_provider": "custom",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Question"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "secret_tool",
                        "arguments": "do not export",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Answer"}],
                    },
                },
            ]
            home, database, _ = create_codex_home(
                Path(directory), rollout_events=events
            )
            result = repair_history.handoff_command(
                command_args(home, database, thread="thread-1", max_messages=10)
            )

            self.assertTrue(result["original_thread_preserved"])
            self.assertEqual(result["message_count"], 2)
            handoff = Path(result["handoff"]).read_text(encoding="utf-8")
            self.assertIn("Question", handoff)
            self.assertIn("Answer", handoff)
            self.assertNotIn("secret_tool", handoff)
            self.assertNotIn("do not export", handoff)
            metadata = json.loads(Path(result["metadata"]).read_text(encoding="utf-8"))
            self.assertEqual(metadata["model_provider"], "custom")


if __name__ == "__main__":
    unittest.main()
