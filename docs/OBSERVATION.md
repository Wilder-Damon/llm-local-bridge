# Bounded health observation

`observe.py` is an explicitly launched foreground diagnostic. It runs fixed, read-only
health probes, records each probe's stdout, stderr and process exit, and stops at its
deadline or first failure. It does not supervise/restart a watcher, invoke a model,
read mailbox bodies/tokens, or change claims. No receiving-message path launches it.

From an authorized checkout, use a **new** local output directory under ignored `logs/`:

```powershell
python observe.py --db .\data\shared.sqlite3 --seconds 300 --interval 60 --timeout 5 --output .\logs\observation-001
```

Alternatively, supply `--until` with an explicit UTC timestamp ending `Z`. The time
must be in the next eight hours. `--seconds` accepts 1..28,800 seconds; an interval is
1..3,600 seconds and a per-check timeout is 0.1..60 seconds. Repeat `--db` for up to
eight mailboxes; the timeout bounds the **whole check**, including startup and all
databases. It is capped by the remaining window. A backwards wall-clock adjustment
cannot extend the initial duration. No implicit window or automatic renewal exists.

Each probe is a directly owned Python child running this helper's fixed health-only
entrypoint, with no shell and no child commands sourced from messages. On timeout or
interrupt, the launcher kills/reaps only that probe. A timeout is a failed/incomplete
check, not a healthy result or permission to restart anything. OS or storage stalls
can still delay termination and diagnostic writes.

## Retained evidence

| File | Meaning |
| --- | --- |
| `lifecycle.jsonl` | Fsynced start, per-check health results and normal terminal record |
| `probe-NNNNNN.stdout.log` | Exact probe stdout, including partial output |
| `probe-NNNNNN.stderr.log` | Exact probe stderr; inspect after a nonzero exit |
| `probe-NNNNNN.exit.json` | Probe PID, start/end, actual exit code, timeout flag and log references |
| `result.json` | Launcher finalization: deadline, reason, number of probes, and intended exit code |

Probe exit evidence is written before parsing health output. A zero exit alone is
insufficient: the launcher also validates the response. A missing PID/exit code means
the probe could not be started; the final result identifies the launch error.

Exit 0 means the checks succeeded until the deadline. Exit 2 means invalid configuration,
probe failure/timeout, or an observation error; exit 130 records a handled interruption.
A setup error may occur before any output directory/result is available. Existing output
directories are refused and preserved. Never overwrite old results to retry.

**A missing `result.json` means termination is unverified.** A process cannot reliably
record its own forced termination or power loss. These files improve diagnosis but do
not prove that the foreground execution host remains alive. If an independently owned
launcher is separately authorized, it must retain this helper's stdout/stderr and actual
exit code too. No such service, host-lifetime guarantee, or persistent launcher is installed.
Do not infer why an observer vanished from an absent result, nor restart after its deadline.

Keep logs local and ignored: diagnostics include database paths and delivery counts.
Tests use synthetic temporary mailboxes only. No live fault injection is appropriate.
