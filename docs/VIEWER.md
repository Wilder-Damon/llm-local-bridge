# Read-only terminal viewer

```powershell
python viewer.py --db .\data\mailbox.sqlite3 --seconds 1800 --interval 2 --cursor-file .\data\viewer-cursor.json
```

The viewer renders audit events in both directions with escaped controls and bounded body
previews. It opens SQLite read-only, never claims or acknowledges messages, and exposes no
claim token. Duration is 1–3,600 seconds. `--after 0` explicitly replays audit history.

The optional cursor is atomically persisted after output. A crash may replay the last
event; sequence numbers identify duplicates. A cursor ahead of current history fails
closed instead of silently skipping events after a restore. Preserve it and reconcile
before choosing a replay position. Cursor paths cannot alias SQLite databases/sidecars.

`Open-Viewer.ps1` is an optional user-invoked convenience wrapper for the same foreground
viewer. It defaults to this checkout's local mailbox, not an external project.
