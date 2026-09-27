# Troubleshooting

## `quit_codex_and_retry`

Fully exit Codex and retry from the `Codex Continuity` shortcut. Do not delete
writer-lock files. The guard refuses to edit an active rollout.

## `inspect_missing_rollouts`

The SQLite index points to a rollout file that is absent. The tool cannot
recover that conversation from GitHub or the cloud and stops without inventing
replacement content.

## `unsupported_schema` or SQLite integrity failure

Stop and preserve the current data. The repair tool only updates a verified
`threads` schema and never guesses SQL for an unknown database layout.

## A visible thread cannot resume

Provider metadata repair affects local visibility. A backend may still reject
encrypted reasoning content produced by another backend. Generate a readable
handoff with:

```powershell
python scripts/repair_history.py handoff --thread THREAD_ID --json
```

The original thread remains untouched.
