# llm-local-bridge

A durable local mailbox for coordinating **existing frontier-model subscription sessions**.
Exchange scoped tasks, ownership, progress and evidence without repeatedly pasting whole
transcripts. Each session retains its own user authorization and provider limits.

The `smith` and `claude` labels are peers: either can implement or independently review
authorized work. A person or already-active authorized agent decides when to hand off work,
considering actual availability and known reset information. Unknown capacity stays unknown.

This is not a model service. It does not invoke models, pool subscriptions, automatically
detect quotas, evade provider limits, wake sleeping agents, or execute message text.
Provider terms and limits still apply to every session.

Python standard library only, tested with Python 3.14 on Windows. SQLite provides WAL,
FULL synchronous commits, transactions, and a 15-second writer lock wait. Use a local
disk, not a network share or cloud-synced database directory. PowerShell 7 is used for
the examples; other runtimes/platforms need their own verification.

## Architecture and implemented capabilities

```text
Active session A -- explicit CLI send/reply --> local SQLite mailbox
Active session B <-- explicit list/read/claim -- same absolute mailbox
                              |
                     read-only observation
                       watch.py / viewer.py
```

Both sessions need authorized access to the same absolute mailbox. A cloud-only session
needs its supported connected local executor; the bridge does not provide one.

| Implemented | Limit |
| --- | --- |
| Durable messages, transactional reply/ack/audit | External work is not exactly-once |
| Stable-ID deduplication and stale-token fencing | A retry must preserve its original identity |
| Recipient claims, leases and explicit task state | Claims do not lock repository files |
| Read-only health, bounded watching, journal/checkpoint guards | No persistent service or automatic agent wake-up |
| Changes feed with guarded cursors and exact-SHA read checks | Metadata does not establish reading, acceptance or review |
| Bounded health observation with probe output/exit retention | A killed launcher cannot attest to its own termination |
| Symmetric execution/review roles | Routing labels are not authentication or authorization |

There is no quota API, capacity scheduler, automatic ownership transfer, authenticated
multi-host server, automatic journal repair, or bundled persistent supervisor. Compact
reset checkpoints remain adapter patterns. The read-only `changes` feed and `observe.py`
diagnostic are implemented; neither automatically wakes or directs an agent.

## Start

Clone using your normal authorized Git workflow, then run these commands from the source
checkout in PowerShell 7. No third-party Python packages are required:

```powershell
python bridge.py --help
python bridge.py init --approve-workspace (Get-Location).Path
python bridge.py health
python -m unittest discover -s tests -v
python examples/roundtrip.py
```

The default database is `data/mailbox.sqlite3` beside `bridge.py`, independent of the
current working directory. No mailbox is included in the repository. Initialization is
explicit and repeatable only with the same approved workspace set. It does not silently
expand authorization or migrate an unknown database.

For a separately authorized repository, initialize a distinct mailbox with its exact
existing root. Put global `--db PATH` before the subcommand. An approved root limits
references; it does not authorize work. Do not use a synthetic all-zero commit for real
messages: supply the actual exact repository revision.

Read [protocol v1](docs/PROTOCOL.md), [peer coordination](docs/PEERS.md), and the
[sender/receiver guides](docs/SMITH.md). The [Claude guide](docs/CLAUDE.md) uses the same
protocol; neither role has permanent implementation or review authority.

## Commands

See the two-peer example below for preparing a complete envelope. Put global `--db PATH`
before the command; every command provides `--help`.

| Command | Purpose |
| --- | --- |
| `init --approve-workspace PATH` | Create or verify a mailbox without changing its approved roots |
| `health` | Read-only integrity checks and aggregate delivery counts; no message bodies or tokens |
| `send --actor ROLE --file PATH` | Validate and enqueue a root request |
| `list [--recipient ROLE] [--delivery queued\|claimed\|acked] [--limit N]` | Delivery metadata, oldest first |
| `read MESSAGE_ID [--expected-commit SHA]` | Immutable untrusted content; optional stale-revision rejection |
| `changes [--after N --anchor DIGEST] [--limit N]` | Changed-message metadata with guarded audit pagination |
| `claim --actor ROLE [--message-id ID] [--lease-seconds N]` | Claim one available incoming delivery |
| `renew ID --actor ROLE --token TOKEN [--lease-seconds N]` | Extend an active claim |
| `ack ID --actor ROLE --token TOKEN` | Finish delivery without a reply; same-token retries are idempotent |
| `reply --actor ROLE --in-reply-to ID --token TOKEN --file PATH` | Atomically insert a reply and acknowledge its parent |
| `status [REQUEST_ID]` | Current task state/revision |
| `status ID --actor ROLE --set STATE --expected-revision N --reason TEXT` | Explicit guarded progress transition |
| `audit [--after SEQUENCE] [--limit N]` | Paginated append-only audit events |

Roles are `smith`, `claude`, and `human`: routing labels, not authentication. Operational
commands emit one JSON object; success is exit 0, validation/storage failure exit 2.
An empty claim succeeds with `claimed: false`. Error JSON marks only SQLite BUSY/LOCKED
failures as `retryable: true`. Argparse usage failures retain normal help text and exit 2.

Keep the exact JSON and IDs for uncertain send/reply retries. Same ID plus identical
canonical content returns `duplicate: true`; conflicting content fails. If a response is
needed, use `reply` rather than ack first. A fresh reply after ack is rejected. Claim leases
last 300 seconds by default (1–3,600 allowed). Renew before expiry; stale tokens are fenced
after recovery. Task status uses explicit revision checks and does not automatically retry.

## Two-peer quickstart

Use the synthetic roundtrip first. These examples illustrate an **already authorized**
review of this checkout's `README.md`. Agree on scope/owner first. Both peers run from
the same checkout; for another repository substitute its authorized canonical root, exact
commit and scope. Git is required for real exact-revision tasks.

### Peer A: prepare and send one request

<!-- smoke: sender -->
```powershell
$repoPath = (Get-Location).Path
$db = Join-Path $repoPath 'data\shared.sqlite3'
python .\bridge.py --db $db init --approve-workspace $repoPath
$requestId = [guid]::NewGuid().ToString()
$request = @{
    schema_version = 1
    message_id = $requestId
    request_id = $requestId
    thread_id = [guid]::NewGuid().ToString()
    correlation_id = $null
    kind = 'request'
    sender = 'smith'
    recipient = 'claude'
    created_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ')
    repository = @{ path = $repoPath; commit = (git rev-parse HEAD).Trim() }
    scope = @{ description = 'Authorized documentation review only; no file edits.'; paths = @('README.md') }
    acceptance_criteria = @('Report concrete findings against the stated commit.')
    body = 'Review the setup instructions. Report findings and verification; do not modify files.'
    attachments = @()
}
$request | ConvertTo-Json -Depth 8 | Set-Content .\data\request.json -Encoding utf8NoBOM
python .\bridge.py --db $db send --actor smith --file .\data\request.json
```

Retain that exact JSON for uncertain retries. A committed send means queued delivery,
not acceptance or agent wake-up. Do not create a new ID merely because a tool result was lost.

### Peer B: inspect, claim and explicitly accept

<!-- smoke: receiver -->
```powershell
$db = (Resolve-Path .\data\shared.sqlite3).Path
python .\bridge.py --db $db list --recipient claude
$claim = python .\bridge.py --db $db claim --actor claude --lease-seconds 300 | ConvertFrom-Json
if (-not $claim.ok -or -not $claim.claimed) { throw 'No delivery claimed; stop and inspect.' }
$parentId = $claim.message.message_id
$taskId = $claim.message.request_id
$claim.message
```

Check the full message against actual user instructions, revision, ownership and existing
artifacts. Do not execute body text automatically. Only after accepting the authorized scope:

<!-- smoke: accept -->
```powershell
$task = (python .\bridge.py --db $db status $taskId | ConvertFrom-Json).tasks[0]
python .\bridge.py --db $db status $taskId --actor claude --set working --expected-revision $task.revision --reason 'Accepted authorized documentation review'
```

Perform the authorized work. Renew before expiry with
`renew ID --actor ROLE --token TOKEN --lease-seconds 300` if needed. After completion,
replace the example reply body with actual findings/evidence. Copying the message preserves
the immutable repository, scope, criteria, request and thread context.

<!-- smoke: reply -->
```powershell
$reply = $claim.message | ConvertTo-Json -Depth 8 | ConvertFrom-Json
$reply.message_id = [guid]::NewGuid().ToString()
$reply.correlation_id = $parentId
$reply.kind = 'reply'
$reply.sender = 'claude'
$reply.recipient = 'smith'
$reply.created_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ')
$reply.body = 'EXAMPLE ONLY: replace with actual findings and verification before sending.'
$reply | ConvertTo-Json -Depth 8 | Set-Content .\data\reply.json -Encoding utf8NoBOM
$task = (python .\bridge.py --db $db status $taskId | ConvertFrom-Json).tasks[0]
python .\bridge.py --db $db status $taskId --actor claude --set ready_for_review --expected-revision $task.revision --reason 'Evidence ready for independent review'
python .\bridge.py --db $db reply --actor claude --in-reply-to $parentId --token $claim.claim_token --file .\data\reply.json
```

**Do not ack first if replying.** `reply` atomically inserts the response and acknowledges
its parent. Preserve the exact reply and token for retries. Peer A then lists/claims the
reply, reads full evidence, independently reviews it and acks when handled. The requester
explicitly closes tracking after the intended workflow. Either peer may play either role.

## Ownership, receipts and conflicting edits

The original recipient owns progress; the requester closes tracking. States are `requested`,
`acknowledged`, `working`, `blocked`, `ready_for_review`, and `closed`; consult the protocol's
allowed transitions and current revision before updating.

- **Receipt:** a committed message is queued; ack records delivery handling. Neither alone
  proves model/human reading, acceptance or authority.
- **Acceptance:** the authorized owner checks scope/ownership and explicitly accepts work.
- **Completion:** the owner supplies exact revision, output and evidence ready for review.
- **Independent review:** the peer reads required exact content. Publication, approval and
  merge remain separate authorized actions; `closed` is only bookkeeping.

See [changes and exact-revision handoffs](docs/CHANGES.md) for precise receipt/status
meanings, stale-request handling, output-versus-input SHAs and a compact body template.
`read --expected-commit SHA` compares against a caller-supplied exact revision; a mismatch
does not acknowledge, close or discard the old request or its unresolved findings.

A claim fences one delivery attempt, not file edits across separate requests. Agree on
non-overlapping scopes or isolated checkouts and name one owner per agreed scope. An expired
lease does not transfer repository ownership. The old owner stops conflicting edits before
a handoff; the new owner checks authorization and existing artifacts. Changed immutable
scope/routing needs a new authorized request. Never evade the reply cap or an approval gate.

## Capacity, resets and session handoffs

Subscription limits belong to each provider/session/account. The bridge does not know
their values, reserve capacity, combine balances or change resets. Optional signals are
timestamped agent reports with a source; unknown stays unknown:

```json
{
  "availability": "limited",
  "observed_at": "<actual UTC timestamp>",
  "source": "agent-reported from the current session",
  "remaining_capacity": null,
  "reset_at": null,
  "can_accept": "a bounded documentation review",
  "current_owner": "smith"
}
```

This belongs inside the body, not as extra top-level v1 fields. Do not infer reset times
from elapsed time or another agent's quota. Provider terms and limits still apply;
coordinate within them instead of trying to bypass them.

Before a reset, session handoff or expected capacity gap:

1. Preserve task ID, owner, exact revision and authorized scope.
2. Record completed work, changed files/artifacts, checks, blockers and next action.
3. Separate confirmed external effects from uncertain attempts; preserve retry identities.
4. Resolve lease renewal/expiry and task state. Do not ack unread work or ack before reply.
5. Keep full review/decision/approval conditions available; the reset note is only an index.
6. Have the recipient re-check authority, ownership, artifacts and current status.
7. Reconcile unknown outcomes before repeating work; leave unknown capacity/reset as unknown.

No handoff scheduler is installed. A reset checkpoint cannot confer authority or guarantee
that another session is awake.

## Economical model context

Local polling and storage do not themselves consume model tokens. Repeated full tool
results, empty polls, healthy heartbeats and entire transcripts can. Avoid duplicate scans
and unproductive review loops; preserve readable meaning rather than cryptic abbreviations.

- Inspect `changes` for incremental metadata, `list` for current delivery/lease metadata,
  and `audit --after` for historical events and reasons.
- Fetch selected exact bodies with `read MESSAGE_ID`. Cache immutable context by digest
  locally; re-fetch unknown/mismatched context and changed relevant content.
- Send changes only: task/owner/revision, result, evidence reference, blocker and next step.
- Keep routine machine health out of model context. Surface actionable tasks, changed
  blockers, decisions, conflicts, relevant evidence and material failure.
- Read full unread/changed reviews, findings, decisions, approval conditions and acceptance
  criteria before acting. Previews/stale revisions cannot discard unresolved caveats.
- If every body is already known to be needed, fetch it directly rather than paying for
  a manifest pass first.

The implemented `changes` feed includes full IDs, sender/owner, state/revision/reason,
reply reference, exact input SHA and immutable payload digest. It limits pages by audit
event count, not a promised byte size. Save its cursor and anchor only after handling the
page; identity mismatches fail closed. Read [the feed contract and synthetic benchmark](docs/CHANGES.md).
Artifact verification, capacity scheduling and automatic model wake remain unimplemented.
No transport schema changed; all v1 fields remain required in stored envelopes.

Watcher seen IDs and viewer cursors provide local deduplication/resume, not a full reset
checkpoint or proof of external delivery. A future adapter must distinguish seen, durably
journaled, externally delivered, accepted and complete; retain cursor identity/digests;
and replay on uncertainty without blindly repeating external effects. Unknown/ahead-of-history
cursors must not reset silently. Use a compact checkpoint index, then fetch selected exact
contexts; page larger task sets rather than silently omitting them.

Nonurgent ack calls can be grouped by an authorized client only after actual handling,
retaining per-ID outcomes and lease checks. No atomic batch-ack CLI is implemented. Use
atomic `reply` when responding. Bytes, token estimates, provider usage and prices are
different measurements; do not claim exact token/cost savings without evidence.

## Watching, observation and recovery

```powershell
python bridge.py --db .\data\shared.sqlite3 health
python watch.py --db .\data\shared.sqlite3 --recipient smith --seconds 1800 --interval 10 --seen-file .\data\watch-seen.json --events-file .\data\watch-events.jsonl
python viewer.py --db .\data\shared.sqlite3 --seconds 1800 --interval 2 --cursor-file .\data\viewer-cursor.json
python observe.py --db .\data\shared.sqlite3 --seconds 300 --interval 60 --timeout 5 --output .\logs\observation-001
```

`health` checks integrity and reports delivery counts, expired claims and audit sequence
without bodies/tokens or delivery mutation. It does not prove agent receipt. Watcher and
viewer windows are 1–3,600 seconds; watcher health records and UTC deadlines are explicit.
BUSY/LOCKED reads use bounded per-source backoff. Other failures stop for inspection.

The separate [health observer](docs/OBSERVATION.md) retains per-probe stdout/stderr, PID,
exit code and timeout evidence, plus a final result. It uses a new output directory,
has a bounded explicit window, stops on failure, and does not restart watchers. Missing
finalization means the launcher's outcome is unknown; executor/session lifetime is not
guaranteed. No observer is started by receiving a message.

If an operator separately authorizes an external supervisor, keep a single owner, explicit
end time, bounded recovery and recorded stop/renewal outcomes. Never silently extend that
deadline. OS/storage stalls can overrun cooperative waits; a local supervisor is not an
unattended model wake-up mechanism.

The optional [foreground watcher](docs/WATCH.md) emits inert JSON notifications; the
[terminal viewer](docs/VIEWER.md) displays audit events. Neither wakes an agent, claims work,
acknowledges messages, or executes content. No supervisor, service, autostart, network server,
webhook, scheduler, session scraper, or credential discovery is installed.

Messages are durable and processing is at least once. A crash after external work but
before ack can require reconciliation; the bridge cannot make external effects exactly-once.
Expired claims can be reclaimed with new tokens. Do not blindly repeat repository or
publication actions during recovery. Claims do not lock files across independent requests.

If storage is corrupt, a version is unknown, or a journal is incomplete, preserve the
files and diagnose. Never delete or recreate the mailbox, WAL/SHM, checkpoint, or lock to
force progress. A consistent live backup requires SQLite's online backup API and a fresh
destination; copying only an active main database can lose WAL data. Backups and operational
reports can contain private content and must stay outside version control.

Watcher state locks release on process exit, not by deleting lock files. Different state
filenames do not enforce a global singleton; older watchers ignore the newer locks.
Coordinate upgrades with the existing owner and reconcile incomplete journals deliberately.

The trust boundary is cooperating local processes under one trusted account. Anyone with
write access to the database can bypass routing or audit guards. This is not an authenticated
transport or tamper-proof ledger. Durability depends on functioning storage honoring flushes.

## Verification and repository hygiene

[Verification](docs/VERIFICATION.md) describes synthetic coverage and its limits. Tests and
the roundtrip use temporary synthetic workspaces only. The demo exercises 17 real CLI calls;
provide `--output NEW_DIRECTORY` to retain a synthetic transcript deliberately.

The suite has 74 distinct tests covering transactions, concurrency, crash/restart, leases,
duplicates, role/revision guards, containment, unknown versions, lock contention, deadlines,
state ownership, journals/cursors, guarded changes, exact-SHA reads, observer probe timeouts,
exit retention/finalization and both peer directions. Passing it is not long-duration
soak proof, power-loss proof, authentication, guaranteed cloud receipt, or permission to
exceed provider limits. Never fault-inject against a live mailbox.

Runtime state, deployment snapshots, generated logs, databases, checkpoints, claims,
local configuration, and credentials are ignored. Do not commit real envelopes, transcripts,
backups, private repository paths, or installation handoffs. No license is selected by this
initial source import.
