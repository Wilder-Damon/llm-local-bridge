# Agent Bridge protocol v1

The authoritative v1 validator and transaction rules are in `bridge.py`. Database
`PRAGMA user_version` is 1. Unknown database or message versions are rejected, with no
automatic migration. Messages are immutable; delivery and task state are separate records.

## Envelope

Every field below is required. Unknown keys and duplicate JSON keys are rejected.
The input must be UTF-8 JSON, at most 65,536 bytes before and after canonical serialization.
Non-finite numbers, excessively deep JSON, invalid timestamps, and wrong field types fail.

| Field | Rule |
| --- | --- |
| `schema_version` | Integer `1` (boolean is not accepted) |
| `message_id` | Canonical lowercase UUID; durable idempotency key |
| `request_id` | Root request's `message_id`; copied into every reply |
| `thread_id` | Fresh canonical UUID for the conversation; unique per root request |
| `correlation_id` | `null` for a root; exact immediately preceding message ID for a reply |
| `kind` | `request` or `reply` |
| `sender`, `recipient` | Distinct routing labels: `smith`, `claude`, or `human` |
| `created_at` | UTC RFC3339 ending `Z`; optional 1–6 fractional digits |
| `repository` | Object with canonical absolute local `path` and exact lowercase 40/64-hex `commit` |
| `scope` | Object with nonempty `description` (≤4,096 chars) and `paths` (1–64 relative references) |
| `acceptance_criteria` | 1–32 nonempty strings, each ≤2,048 chars |
| `body` | Nonempty inert text, ≤32,768 chars |
| `attachments` | 0–16 objects, each with relative `path` and nonempty `description` (≤1,024 chars) |

Strings reject control characters except tab, CR, and LF in prose. Path references reject
all those characters. Relative paths use `/`; no `..`, `.`, empty components, wildcard,
drive/stream syntax, Windows reserved device names, trailing dot/space, or absolute paths.
Each reference is resolved for containment within the approved repository, including
existing symlinks/junctions. References need not exist and their contents are never read.
Workspace roots must exist and match the canonical approved root exactly. UNC paths are
rejected. Do not put the database on a mapped network drive or synced folder.

Scope paths identify exact files or directory subtrees; prose narrows the intended operation.
Neither scope nor a role label grants permission. A receiver must compare all fields with
the actual user's authorization, repository revision, and its own execution constraints.
The transport cannot prevent an agent from acting outside these constraints; the integration
instructions are mandatory for safe use.

Replies reverse the parent sender/recipient and preserve the exact `request_id`, `thread_id`,
repository, scope, and acceptance criteria. New message ID, timestamp, body, and attachment
references may differ. A scope or commit change requires user clarification and a new
root request; it must never be smuggled into a reply.

## Delivery and idempotency

Delivery is `queued` → `claimed` → `acked`. `received_at` is the transport's UTC timestamp;
it is distinct from the sender's `created_at`. Claims use wall-clock Unix expiry seconds,
random UUID attempt tokens, and a recipient role. Keep the Windows clock reasonably stable;
large clock changes can affect lease timing.

`BEGIN IMMEDIATE` serializes selection and claim changes. One competing process obtains
a particular available message. A stale claim is reclaimed with a new token; old tokens
cannot renew, ack, or send a fresh reply. Lease renewal requires a still-active claim.
Processing is at least once: a receiver may crash after doing work but before acknowledging.
Agents must independently check existing artifacts before repeating external work.

Fresh replies require an active claim. Reply insert, parent ack, and both audit events commit
atomically. A send/reply retry with the same ID and canonical content returns `duplicate: true`;
different content under an existing ID fails. A reply retry also requires its original parent
and claim token. Repeated ack with the same token succeeds even after expiry once already acked.
No new reply may be appended to an acked delivery; use reply instead of ack when responding.

There is no promise of exactly-once execution outside SQLite. The bridge performs no such
execution. Claim tokens protect against accidental stale workers, not a hostile local user.
`read` omits tokens; the successful claimant receives its token directly. List/read do not mutate.

## Task state

| State | Meaning |
| --- | --- |
| `requested` | Root accepted by the mailbox, not yet claimed by its recipient |
| `acknowledged` | Recipient has claimed the root; it has not thereby approved the work |
| `working` | Recipient explicitly reports authorized work in progress |
| `blocked` | Recipient cannot safely or usefully proceed; reason and human escalation needed |
| `ready_for_review` | Recipient reports that evidence is ready; not approval or merge permission |
| `closed` | Original requester explicitly finished tracking the task; not a code-review approval |

Transitions:

```text
requested -> acknowledged (first claim) or blocked
acknowledged -> working or blocked
working -> blocked or ready_for_review
blocked -> working
ready_for_review -> working or closed
closed -> terminal
```

Only the original recipient updates progress; only the original requester can set `closed`.
Every explicit transition requires a nonempty reason and current `--expected-revision`.
Successful transitions increment the revision, starting at 0 for the root and 1 after its
first claim. A revision conflict requires reading and reconsidering current status.
Retries of state changes are not silently accepted: query status to see whether the intended
transition committed before retrying. Ack changes delivery only; it never closes a task.
Claim recovery does not reset task progress. Closed tasks cannot be claimed or receive new
replies; existing delivery metadata remains available for inspection.

`acknowledged` is an automatic claim milestone, not acceptance. `working` is the recipient's
explicit work report; `ready_for_review` is an outcome report, not an independent review.
Review verdict, exact reviewed/output SHA, conditions, publication and merge evidence must
be stated explicitly in inert message bodies and evaluated under actual user authorization.
No transport state automatically establishes those external milestones.

## Read-only changes and revision checks

The additive `changes` command pages the existing audit trail with current message/task
metadata from one read-only snapshot. It requires the returned anchor when resuming after
a positive sequence, detects database/path/boundary mismatches and rejects cursors ahead of
history. It does not alter v1 storage or envelopes. Metadata is not a read receipt or a
replacement for complete reviews/criteria; see [the feed contract](CHANGES.md).

`read MESSAGE_ID --expected-commit SHA` optionally rejects a different exact input SHA.
It does not run Git, discover HEAD, discard stale messages or modify task/delivery state.
Replies still preserve the original input SHA. Report a new output SHA in the body and,
when authorized, originate a separate root pinned to that revision for independent review.

## Escalation and loop bounds

The transport permits **at most 12 replies per root request**. The thirteenth is rejected
without acknowledging its parent. Do not create another root just to evade this limit.
Stop and involve the user. Keep an ordinary task to two clarification/rework exchanges;
escalate repeated disagreement, recurring blockers, scope changes, lease conflicts, or
missing authorization sooner. These earlier judgment thresholds are agent operating rules;
the 12-reply ceiling is enforced by the database transaction.

There is no automatic polling/retry loop, scheduler, agent spawning, review approval, or
merge. If blocked, the original recipient explicitly records `blocked` with the reason and
the user is informed in the current conversation. After explicit clarification it may
resume `working`. Any new external effects still need the agent's normal authorization.

## Audit and errors

Each committed initialization, send/reply, claim/reclaim, renewal, ack, and task transition
appends a sequenced event with UTC time, actor, event name, message ID, and JSON details.
Send events include the canonical payload SHA-256 digest. Updates/deletes to audit rows
are blocked by SQLite triggers, and no CLI command removes events. Idempotent retries add
no duplicate success events. Rejected input and failed transactions return errors and add
no partial records; stderr/transcript capture is the operator's record of rejected attempts.
The audit is append-only through this application, not cryptographically tamper-proof.

SQLite WAL plus FULL synchronous commits provide crash-safe transactions on a functioning
local disk. All foreign-key checks are enabled on operational connections. Writes wait up
to 15 seconds for contention, then return an error. Failed writes roll back. No operation
executes a command embedded in message content, fetches URLs, reads private session files,
contacts GitHub, or interprets attachment content.
