# Synthetic verification

Run from the source checkout:

```text
python -m unittest discover -s tests -v
python examples/roundtrip.py
```

The suite contains 59 distinct tests and uses temporary synthetic workspaces/mailboxes.
The roundtrip exercises 17 real CLI invocations. No live mailbox contents or private
repository files are fixtures. The suite was exercised on Python 3.14 on Windows;
other runtimes/platforms need their own verification.

Coverage includes concurrent process claims and duplicate sends; atomic reply/ack/audit
rollback; committed restart and abrupt uncommitted process exit; lease expiry, renewal and
stale-token fencing; idempotent retries; guarded status transitions; malformed/oversized
input and reference containment; unknown database preservation; append-only audit guards;
real SQLite lock errors; bounded retries and deadlines; state ownership and crash-release;
torn/missing journals and checkpoint failures; sidecar/hard-link guards; ahead-of-history
cursors; read-only health; and both peer execution/review directions.

Passing synthetic tests is not proof of long-duration soak behavior, real agent delivery,
model/human receipt, power-loss safety, cloud wake-up, tamper resistance, or external
exactly-once effects. SQLite durability depends on storage behavior. Do not fault-inject
against a live mailbox. Local verification outputs remain ignored because they may contain
machine paths, synthetic tokens, or operational details.
