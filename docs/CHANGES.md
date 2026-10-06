# Read-only changes and exact-revision handoffs

`changes` provides a bounded audit page and current metadata for message IDs affected
by those events. It reuses the v1 audit trail; it does not migrate the database, mutate
delivery, read attachments, or infer authorization from content.

```powershell
$page = python bridge.py --db .\data\shared.sqlite3 changes --limit 25 | ConvertFrom-Json
if (-not $page.ok) { throw 'Changes read failed; preserve cursor and inspect.' }
$page.messages
```

Handle the page first, then save **both** `next_after` and `anchor`. Resume on the same
absolute database path:

```powershell
$page = python bridge.py --db .\data\shared.sqlite3 changes --after $page.next_after --anchor $page.anchor --limit 25 | ConvertFrom-Json
if (-not $page.ok) { throw 'Changes read failed; preserve cursor and inspect.' }
```

Drain pages while `has_more` is true. `--limit` bounds audit events (1..100), not bytes or
unique tasks. The initial initialization event may produce a page with no messages;
advance using its cursor. An empty tail keeps the same cursor. No role filter hides
events while advancing; clients may route rows after receiving the complete page.

The anchor binds the canonical database path, initial audit record and cursor-boundary
record. Missing anchors on resume, different mailboxes/paths, missing boundaries and
ahead-of-history cursors fail closed. Preserve the cursor and reconcile deliberately;
do not reset automatically. This is accidental-mismatch detection, not authentication
or a tamper-proof history. An identical restored history prefix at the same path cannot
be distinguished, and effects outside SQLite still need their own reconciliation.

## What rows mean

Rows contain full message/request IDs, sender/recipient, original task **owner**, kind,
reply parent, exact repository path/SHA, payload digest, delivery, and current task
state/revision/reason. Replies retain the root recipient as owner even when routing
reverses. No body, acceptance-criteria text, attachment contents or claim token is emitted.

Each call uses one read-only SQLite snapshot. Event rows are historical triggers;
message/task metadata reflects the **current snapshot**, which can be newer than that
page's last event. A task may appear again on the next page. The command reports
`snapshot_sequence` so clients can distinguish that snapshot from `next_after`.
For historical transitions and their reasons use `audit --after`; metadata is not a
complete review or decision history. Lease expiry alone does not append an event;
use `health`, `list` or `claim` for current lease availability.

| Evidence | What it establishes |
| --- | --- |
| Committed send / `queued` | Message is stored for delivery |
| `claimed` / task `acknowledged` | A recipient obtained a delivery lease |
| `acked` | That delivery attempt was handled; task may still be only `acknowledged` |
| Explicit `working` and reason | Recipient reports accepting authorized work |
| `ready_for_review` and evidence | Recipient reports an outcome ready for review |
| `closed` | Requester ended tracking |

None establishes independent review, approval, publication or merge. Those need explicit
evidence against the exact reviewed SHA and their own authorization. The transport does
not parse a body or status reason to invent these milestones. A blocked task's reason
is current reported context, not authority to override a restriction.

## Fetch exact content before acting

Cache immutable messages by message ID and digest **after reading them fully**. Metadata
that is present in a cache is not proof that an agent has read/accepted it. Read unknown,
changed or required review/decision context in full. Read changed task reasons too.
Save a cursor only after retaining everything needed for safe handling; on interruption,
replay the page and deduplicate by IDs/digests without repeating external effects.

An optional exact-revision guard catches a stale handoff:

```powershell
# Set these from the selected row and the explicitly intended revision.
python bridge.py --db .\data\shared.sqlite3 read $messageId --expected-commit $expectedCommit
```

The expected SHA must be a complete lowercase 40/64-hex value. It is supplied by the
caller; the bridge does not run Git or assert that it is HEAD. A mismatch returns exit
2 and preserves the message, lease and task. Inspect the old request and unresolved
findings; do not ack, close, or discard it merely because its revision is stale.
Omitting the option preserves the previous `read` behavior for deliberate inspection.

V1 replies must preserve the input repository SHA. If implementation produced a new
commit, identify the **output SHA** in the inert reply body. An independently authorized
review of that new commit needs a new root pinned to it, with the earlier request
referenced for context. Never rewrite the original request or imply that reviewing the
old SHA approves the new one.

## Compact changes-only bodies

Keep IDs, routing and input SHA in their existing envelope fields. A short inert body
can name the prior message and changed facts without repeating the full transcript:

```text
Since: <prior message ID>
Work: completed by the owner; independent review pending
Output SHA: <exact new commit, when applicable>
Changed: <files and concrete behavior>
Checks: <actual commands/results and evidence reference>
Blockers/conditions: <all unresolved findings, limitations or approval conditions>
Next: <named owner and bounded next step>
```

For a review, explicitly name the exact reviewed SHA, verdict, findings and conditions.
For an acceptance, say what scope was accepted and record `working` explicitly. Never
use a short completion note as a substitute for unread findings or acceptance criteria.
No body template is a new top-level v1 field or an executable instruction.

## Reproducible overhead measurement

Run `python examples/measure_changes.py`. It creates/removes a synthetic mailbox with
12 requests, a progress update, and one new request across three scans. It measures
canonical CLI-shaped UTF-8 response bytes, including required full body reads. It makes
no model calls and measures neither tokens nor provider cost. Fixture sizes and local
temporary path lengths affect byte totals.

The cold path always adds a changes page when every body is needed. Warm paths can avoid
repeating known immutable bodies; the example still fetches the new message in full and
retains the changed status reason. This benchmark does not prove savings for every workload,
actual recipient reading, or safe automatic omission of reviews.

A representative Windows run measured **191,807 bytes** for three complete scans versus
**77,185 bytes** for changes plus all required uncached reads (**59.76% fewer bytes**).
The cold first scan grew from 62,196 to 70,125 bytes because the metadata page added
7,929 bytes. Full-body reads decreased from 37 to 13 across the three scans. Re-run the
script to measure the fixture in your environment; these are synthetic response bytes,
not a production savings guarantee.
