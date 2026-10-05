# Bounded foreground watching

`watch.py` opens mailboxes with SQLite `mode=ro` and `query_only=ON`. It emits pending
message identifiers, scope/repository references, delivery state, and task revision as
inert JSON lines. It omits message bodies, attachments, and claim tokens. It never claims,
acks, replies, changes task state, reads attachment contents, or executes message content.

```powershell
python watch.py --db .\data\mailbox.sqlite3 --recipient smith --seconds 1800 --interval 10 --seen-file .\data\watch-seen.json --events-file .\data\watch-events.jsonl
```

The duration is 1–3,600 seconds and polling interval 1–60 seconds. A monotonic deadline
bounds polling; Ctrl+C stops it. A start record reports the UTC deadline. There is no
service, automatic renewal, autostart, or agent wake-up. OS I/O stalls can delay cooperative
shutdown; an explicitly authorized external supervisor may impose a harder bound.

Reuse the same checkpoint/journal pair across deliberate windows. Each notification is
fsynced to the journal before emission and checkpoint replacement. A crash between these
steps can duplicate a notification ID; consumers must deduplicate and reconcile external
delivery separately. A seen ID is not proof of cloud delivery, receipt, or task completion.

Exclusive OS locks guard both state paths using adjacent persistent `.lock` files. Process
exit/crash releases ownership; never delete a lock file to bypass it. Older watcher versions
ignore these locks, and different state filenames do not provide a global singleton. Coordinate
one owner. Output paths cannot alias the database, SQLite sidecars, or each other.

Only SQLite BUSY/LOCKED reads retry, independently per source, with exponential delay capped
at 60 seconds within the original deadline. Each read waits at most one second for a lock,
bounded by remaining time. Other sources continue. Retry/recovery records and 60-second health
records expose last successful reads. Deadline stop reports degraded state when appropriate.

Before append, an incomplete/corrupt journal is preserved and rejected. Nonempty seen IDs
without their journal also fail closed. State limits are 16 MiB on reads/writes. There is no
automatic truncation, rotation, cursor reset, mailbox repair, or hidden recovery. Preserve the
pair, reconcile the complete journal prefix and confirmed delivery, and plan an explicit replay.

`relay_feed.py` is a read-only journal helper for an active authorized agent. It defers an
incomplete final record and can identify explicitly labeled routine Claude heartbeats, retaining
material text/context changes. It does not send anything itself or confer authorization. Keep
routine healthy polling out of model context where possible. A missing heartbeat is uncertainty,
not proof that another agent failed. Any external relay requires separate user authorization.
