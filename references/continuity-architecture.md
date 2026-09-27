# Continuity Architecture

The v6 continuity layer is a one-shot pre-launch check. It does not run a
watcher, service, scheduled task, or resident Python process.

```text
provider switch or account change
        |
fully quit Codex
        |
Codex Continuity launcher
        |
doctor/guard -> scan -> backup -> metadata repair -> verify
        |
launch Codex
```

The current provider comes from the effective top-level `model_provider` in
`config.toml`. The guard does not inspect `auth.json` and does not need to
know which tool performed the switch. It synchronizes the SQLite
`threads.model_provider` index and the rollout
`session_meta.payload.model_provider` field for user threads.

The guard fast path checks SQLite, rollout existence, bounded session metadata,
and a local state fingerprint. A change or unknown state takes the full
analysis path. Every rewrite creates an operation manifest and can be undone.

Metadata repair restores local visibility. It cannot make a backend decrypt
another backend's `encrypted_content`; use `handoff` while preserving the
original thread when resume fails.
