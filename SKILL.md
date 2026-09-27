---
name: repair-codex-history
description: Prevent and repair local Codex Desktop history problems around account, model-provider, configuration, or app changes. Use to install one-time continuity/pre-launch mode, create verified snapshots before changes, repair hidden local threads after changes, generate a safe handoff when encrypted session content cannot be resumed, or undo a repair.
---

# Repair Codex History

Protect local user-owned tasks before risky changes, recover hidden tasks after provider changes, and optionally install a Windows pre-launch continuity guard. The guard is a one-shot process that runs before Codex starts; it is never a background watcher or service. Back up every changed file exactly and never read or modify `auth.json`.

## Choose the workflow

Use **continuity setup** when the user wants future account/provider switches to require only “quit Codex, then launch through Codex Continuity”. Use **recovery** when history is already hidden or a thread still points at an old provider. Use **handoff** only when a visible thread cannot be resumed by the current backend.

## Continuity setup (Windows)

1. Run a read-only diagnostic:

   ```powershell
   python scripts/repair_history.py doctor --json
   ```

2. If the user explicitly requested one-time continuity mode, run:

   ```powershell
   python scripts/repair_history.py bootstrap --yes --json
   ```

3. Require `bootstrap_complete: true` and report the snapshot, repair backup, and `Codex Continuity` shortcut path. Bootstrap creates a full snapshot, aligns user-thread provider metadata, installs the Windows launcher, and verifies the result.
4. If `next_action` is `quit_codex_and_retry`, stop and have the user fully exit Codex before retrying. If it is `inspect_missing_rollouts`, do not modify anything further. If it is `install_guard`, report the installer failure and preserve the snapshot for inspection.
5. Explain the daily workflow: switch account/provider, fully quit Codex, and launch from `Codex Continuity`.

Bootstrap is currently Windows-only because the bundled launcher uses the Windows Start Menu/AppUserModelID. Do not install a watcher, scheduled task, service, or resident Python process. Do not replace the original Codex shortcut unless the user explicitly asks for that behavior.

## Pre-launch Guard

The launcher runs:

```powershell
python scripts/repair_history.py guard --yes --json
```

On the fast path, when user-thread provider metadata already matches the current provider, Guard returns `guard_complete: true`, `changed: false`, and `next_action: launch` without creating a backup. On the repair path, it rechecks Codex and writer locks, validates SQLite, creates a differential repair backup, updates only provider metadata, verifies the result, and returns `next_action: launch`.

Guard uses index-only repair by design. It must not create compatibility aliases or modify `config.toml`, provider endpoints, API keys, OAuth settings, or CC Switch state. The source of truth is the final Codex configuration, regardless of whether the switch came from CC Switch, Codex login, a manual edit, or another tool. `auth.json` is never opened.

If Codex is still running or a writer lock remains, return `quit_codex_and_retry`. Never delete locks, kill processes, or edit active rollouts. If the schema is unknown, SQLite integrity fails, or a rollout is missing, stop safely and report the corresponding `next_action`.

## Recovery workflow

1. Locate this skill directory and run a read-only scan:

   ```bash
   python3 scripts/repair_history.py scan --json
   ```

2. Report the Codex home, database, current provider, user-task count, hidden count, archived count, runtime-provider mismatch count, writer-locked mismatch count, internal subagent count, missing rollout count, `repair_complete`, and `next_action`.
3. Stop without changing anything when the user requested inspection only, the schema is unsupported, SQLite integrity fails, the provider is unknown, or rollout files are missing.
4. When the user asks to repair or restore, run:

   ```bash
   python3 scripts/repair_history.py repair --yes --json
   ```

5. Preserve archived status by default. Add `--unarchive` only when explicitly requested. Use `--index-only` only when the user explicitly wants to skip compatibility aliases; Guard and bootstrap already use index-only repair.
6. Report the backup directory, SQLite rows changed, rollout files changed, metadata events changed, aliases synchronized, locked tasks skipped, `repair_complete`, and `next_action`.

The repair changes only `session_meta.payload.model_provider` in unlocked rollout JSONL files and `threads.model_provider` in the verified SQLite schema. It preserves messages and tool outputs, records before/after SHA-256 hashes, and uses an operation manifest for undo. A no-op repair is valid and should not be described as a rewrite.

## Handoff fallback

Provider metadata repair restores local visibility, but it cannot make a backend decrypt another backend's `encrypted_content`. If a visible thread cannot resume, preserve the original and run:

```bash
python3 scripts/repair_history.py handoff --thread THREAD_ID --json
```

The command writes `HANDOFF.md`, `metadata.json`, and a source hash manifest under `~/.codex/history-repair-handoffs/<thread-id>/<timestamp>/`. It extracts readable user/assistant text and basic session metadata only. It excludes tool output, hidden reasoning, `encrypted_content`, and credentials. Never delete or rewrite the original Thread to make handoff appear successful.

## Snapshot and undo

Before a known change, use:

```bash
python3 scripts/repair_history.py snapshot --yes --json
```

For a provider switch with an already configured target provider, proactive migration remains available:

```bash
python3 scripts/repair_history.py repair --provider TARGET --yes --json
```

Undo a repair, Guard repair, or bootstrap migration with its operation manifest:

```bash
python3 scripts/repair_history.py undo --backup /path/to/repair-backup --yes --json
```

Never pass a full snapshot manifest to `undo`; snapshots are disaster-recovery copies, while undo requires the operation manifest and protects work created after the repair.

## Safety rules

- Treat this as a local compatibility and continuity tool, not cloud-account recovery.
- Never claim to recover a conversation whose rollout file is absent locally.
- Never edit `auth.json`, API keys, cookies, OAuth tokens, messages, assistant content, tool output, or `encrypted_content`.
- Copy the current provider configuration into compatibility aliases only in explicit manual `repair`; never copy an old endpoint forward. Guard/bootstrap do not create aliases.
- Never hardcode a username, provider name, Codex home, or database version.
- Exclude internal subagent threads from normal user history migration.
- Recheck locks immediately before backup and replacement; a new lock means stop and retry.
- Keep backups and manifests until the user verifies the result.
- Do not replace the live database wholesale from a snapshot or improvise SQL for an unknown schema.
