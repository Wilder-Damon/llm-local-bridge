# llm-local-bridge

A durable local mailbox for cooperating coding agents. Messages carry context and
evidence; agents retain their own user authorization. The bridge never executes message
text, invokes a model, approves a review, or merges code.

Python standard library only, tested with Python 3.14 on Windows. SQLite provides WAL,
FULL synchronous commits, transactions, and a 15-second writer lock wait. Use a local
disk, not a network share or cloud-synced database directory.

## Start

From this repository in PowerShell:

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

| Command | Purpose |
| --- | --- |
| `init --approve-workspace PATH` | Create or verify a mailbox without changing its approved roots |
| `health` | Read-only integrity checks and aggregate delivery counts; no message bodies or tokens |
| `send --actor ROLE --file PATH` | Validate and enqueue a root request |
| `list [--recipient ROLE] [--delivery queued\|claimed\|acked] [--limit N]` | Delivery metadata, oldest first |
| `read MESSAGE_ID` | Immutable untrusted content and delivery metadata, without claim token |
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

## Watching and recovery

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

The trust boundary is cooperating local processes under one trusted account. Anyone with
write access to the database can bypass routing or audit guards. This is not an authenticated
transport or tamper-proof ledger. Durability depends on functioning storage honoring flushes.

## Verification and repository hygiene

[Verification](docs/VERIFICATION.md) describes synthetic coverage and its limits. Tests and
the roundtrip use temporary synthetic workspaces only. The demo exercises 17 real CLI calls;
provide `--output NEW_DIRECTORY` to retain a synthetic transcript deliberately.

Runtime state, deployment snapshots, generated logs, databases, checkpoints, claims,
local configuration, and credentials are ignored. Do not commit real envelopes, transcripts,
backups, private repository paths, or installation handoffs. No license is selected by this
initial source import.
