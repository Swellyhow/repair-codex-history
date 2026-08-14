---
name: repair-codex-history
description: Prevent and repair local Codex Desktop history problems around account, model-provider, configuration, or app changes. Use before switching accounts/providers or upgrading Codex to create a verified snapshot and preflight migration, and after changes when tasks disappear, old conversations call an old URL, history vanishes again after restart, all local history must be restored, or a repair must be undone.
---

# Repair Codex History

Protect local user-owned tasks before risky changes, then recover them when necessary by migrating both the SQLite index and rollout provider metadata. Back up every changed file exactly and never read or modify `auth.json`.

## Prevention First

Use this path when the user has not switched accounts/providers or upgraded Codex yet.

1. Run a read-only scan.
2. Create a full verified snapshot before any configuration or account change:

   ```bash
   python3 scripts/repair_history.py snapshot --yes --json
   ```

3. Require `snapshot_complete: true`. Report the backup directory, rollout count, byte count, locked rollouts captured, and manifest. A snapshot reads locked rollouts only after two identical reads and never modifies them.
4. For a provider switch, first ensure the target provider table already exists in `config.toml`, then proactively migrate history before changing the top-level provider:

   ```bash
   python3 scripts/repair_history.py repair --provider TARGET --yes --json
   ```

5. After the proactive migration, change the configured provider, restart Codex, scan again, and follow `next_action`. Locked active tasks may still require the normal second pass.
6. For an account-only change that keeps the same provider, preserve the snapshot, switch accounts, then scan immediately after reopening Codex.
7. Keep the last verified snapshot and repair backup until the post-change scan reports `repair_complete: true` and `next_action: none`.

## Recovery Workflow

1. Locate this skill directory and use `scripts/repair_history.py` from it.
2. Run a read-only scan first:

   ```bash
   python3 scripts/repair_history.py scan --json
   ```

3. Report the detected Codex home, database, current provider, user-task count, hidden count, archived count, runtime-provider mismatch count, writer-locked mismatch count, internal subagent count, missing rollout-file count, `repair_complete`, and `next_action`.
4. Stop without changing anything when:
   - the user requested inspection only;
   - the database schema is unsupported;
   - SQLite integrity fails;
   - no current provider can be determined;
   - rollout files are unexpectedly missing and the user has not acknowledged that risk.
5. When the user explicitly asks to repair or restore, run:

   ```bash
   python3 scripts/repair_history.py repair --yes --json
   ```

6. Preserve archived status by default. Add `--unarchive` only when the user explicitly asks for archived tasks to return to the main sidebar.
7. The repair changes only `session_meta.payload.model_provider` in unlocked rollout JSONL files. It preserves all conversation messages and tool outputs byte-for-byte, stores exact originals with SHA-256 hashes, and updates SQLite consistently.
8. By default, also synchronize every custom legacy rollout provider as a compatibility alias of the current provider for already loaded sessions. Use `--index-only` only when the user explicitly wants to skip aliases.
   - Keep reserved built-in providers such as `openai`, `oss`, `ollama`, and `lmstudio` unchanged.
9. After repair, report the backup directory, SQLite rows changed, rollout files changed, session metadata events changed, aliases synchronized, writer-locked tasks skipped, `repair_complete`, and `next_action`.
10. Follow `next_action` exactly:
    - `restart_and_rerun`: quit and reopen Codex, then run the same scan and repair again. The first pass intentionally skipped active old-provider rollouts.
    - `restart_to_reload`: the persistent migration is complete; reopen Codex once to reload the sidebar, but do not claim another repair pass is required.
    - `repair` or `repair_again`: run the repair command when safety checks permit.
    - `inspect_missing_rollouts`: stop and report that local rollout files are absent.
    - `none`: no further action is needed.
11. Treat `rerun_required_after_restart`, not the older `restart_required` field alone, as the signal for a mandatory second repair pass.
12. A no-op repair is valid and creates no backup. When `changed` is false and `backup_directory` is null, report the verified state without implying that files were rewritten.

## Undo

Use the manifest from the repair output. Prefer manifest-based undo over replacing the entire live database because it preserves tasks created after the repair.

```bash
python3 scripts/repair_history.py undo --backup /path/to/backup-directory --yes --json
```

Use `--latest` only when the user clearly wants to undo the most recent repair.
Never pass a snapshot manifest to `undo`; snapshots are complete recovery copies, not change manifests.

## Safety Rules

- Treat this as a local compatibility repair, not cloud-account recovery.
- Never claim to recover conversations whose rollout files are absent locally.
- Prefer prevention mode before a known account, provider, configuration, or app change; use recovery mode only after the state has already diverged.
- Never edit `auth.json`, API keys, cookies, conversation messages, or tool outputs.
- Modify only `session_meta.payload.model_provider` in rollout JSONL files, after byte-for-byte backup and before/after SHA-256 recording.
- Copy the current provider configuration into compatibility aliases; never copy an old provider endpoint forward.
- Never hardcode a username, provider name, Codex home, or database version.
- Exclude internal subagent threads from normal user-task migration.
- Treat writer-lock files as a restart-and-rerun signal, not as permission to delete locks, kill processes, or edit active rollouts.
- Recheck writer locks immediately before backup and replacement. If a new lock appears after the scan, stop and rerun instead of modifying that rollout.
- Keep the generated database backup and manifest until the user verifies the result.
- Do not replace the live database wholesale from a snapshot; use manifest-based repair/undo so tasks created later are preserved.
- Do not improvise raw SQL when the bundled script rejects an unknown schema; update and retest the script instead.
