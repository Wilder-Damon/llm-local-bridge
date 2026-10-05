"""Bounded foreground, read-only mailbox watcher. Emits inert JSON lines only."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from bridge import BridgeError, DEFAULT_DB, ROLES, VERSION, canonical, require, storage_busy
from local_io import distinct_state_paths, exclusive_state


def pending(database, recipient, timeout=5):
    path = Path(database).resolve()
    require(not str(path).startswith(('\\\\', '//')), 'watch database must be local')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA user_version').fetchone()[0] == VERSION, 'unsupported database version')
        rows = db.execute("""SELECT m.message_id,m.request_id,m.sender,m.recipient,m.delivery,
                           m.received_at,m.digest,m.payload,t.thread_id,t.state,t.revision
                           FROM messages m JOIN tasks t ON m.request_id=t.request_id
                           WHERE m.recipient=? AND m.delivery!='acked' AND t.state!='closed'
                           ORDER BY m.received_at,m.message_id""", (recipient,))
        result = []
        for row in rows:
            require(len(result) < 10000, 'watch backlog exceeds 10000; inspect manually')
            item = dict(row)
            payload = json.loads(item.pop('payload'))
            item.update(database=str(path), repository=payload['repository'], scope=payload['scope'], untrusted=True)
            result.append(item)
        return result
    finally:
        db.close()


def event_key(item):
    return str(Path(item['database']).resolve()).casefold() + '|' + item['message_id']


def load_seen(path):
    if not path.exists():
        return set()
    require(path.stat().st_size <= 16 * 1024 * 1024, 'watch checkpoint is oversized')
    value = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1, 'invalid watch checkpoint')
    require(isinstance(value.get('seen'), list) and all(isinstance(x, str) for x in value['seen']), 'invalid seen list')
    return set(value['seen'])


def save_seen(path, seen):
    payload = canonical({'version': 1, 'seen': sorted(seen)})
    require(len(payload.encode('utf-8')) <= 16 * 1024 * 1024, 'watch checkpoint is oversized')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def watch(databases, recipient, seconds, interval, seen_path, events_path,
          emit=None, clock=time.monotonic, sleep=time.sleep):
    require(recipient in ROLES, 'unknown recipient')
    require(1 <= seconds <= 3600 and 1 <= interval <= 60, 'duration 1..3600 seconds; interval 1..60 seconds')
    seen_path, events_path = Path(seen_path).resolve(), Path(events_path).resolve()
    sources = list(dict.fromkeys(str(Path(p).resolve()) for p in databases))
    require(sources and len(sources) <= 8, 'watch requires 1..8 mailboxes')
    locks = [Path(str(path) + '.lock') for path in (seen_path, events_path)]
    distinct_state_paths(sources, [seen_path, events_path, *locks])
    with exclusive_state(locks):
        return _watch(sources, recipient, seconds, interval, seen_path, events_path,
                      emit, clock, sleep)


def _watch(sources, recipient, seconds, interval, seen_path, events_path, emit, clock, sleep):
    seen = load_seen(seen_path)
    require(not seen or events_path.is_file(), 'checkpoint has seen IDs but journal is missing; preserve checkpoint and recover journal')
    if events_path.exists():
        # Never append to a torn record: doing so turns a recoverable tail into
        # permanent corruption in the middle of the journal. Preserve it for recovery.
        require(events_path.stat().st_size <= 16 * 1024 * 1024, 'event journal oversized; inspect manually')
        with events_path.open('rb') as source:
            for line in source:
                require(line.endswith(b'\n'), 'incomplete event journal tail; preserve journal and recover before restart')
                require(isinstance(json.loads(line), dict), 'invalid event journal record')
    events_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = clock() + seconds
    utc_deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    count = 0
    failures = {database: 0 for database in sources}
    retry_at = {database: 0 for database in sources}
    last_poll = {database: None for database in sources}
    health_at = clock() + 60
    if emit is None:
        emit = lambda item: print(canonical(item), flush=True)
    with events_path.open('a', encoding='utf-8', newline='\n') as journal:
        def publish(item):
            item['at'] = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
            line = canonical(item) + '\n'
            require(journal.tell() + len(line.encode('utf-8')) <= 16 * 1024 * 1024,
                    'event journal oversized; inspect manually')
            journal.write(line)
            journal.flush()
            os.fsync(journal.fileno())
            emit(item)
        publish({'event': 'watch_started', 'recipient': recipient, 'databases': sources,
                 'deadline': utc_deadline.isoformat().replace('+00:00', 'Z'),
                 'interval_seconds': interval, 'read_only': True})
        try:
            while clock() < deadline:
                for database in sources:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        break
                    if clock() < retry_at[database]:
                        continue
                    try:
                        items = pending(database, recipient, timeout=min(1, remaining))
                    except sqlite3.Error as exc:
                        if not storage_busy(exc):
                            raise
                        failures[database] += 1
                        delay = min(60, interval * 2 ** min(failures[database] - 1, 6))
                        retry_at[database] = clock() + delay
                        publish({'event': 'watch_retry', 'database': database,
                                 'consecutive_failures': failures[database],
                                 'retry_in_seconds': delay, 'error': str(exc)})
                        continue
                    last_poll[database] = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
                    if failures[database]:
                        publish({'event': 'watch_recovered', 'database': database,
                                 'consecutive_failures': failures[database]})
                    failures[database] = 0
                    for item in items:
                        if clock() >= deadline:
                            break
                        key = event_key(item)
                        if key not in seen:
                            publish({'event': 'message_available', 'notification_id': key, **item})
                            seen.add(key)
                            save_seen(seen_path, seen)
                            count += 1
                if clock() >= health_at and clock() < deadline:
                    publish({'event': 'watch_health', 'last_successful_poll': dict(last_poll),
                             'consecutive_failures': dict(failures), 'new_messages': count})
                    health_at = clock() + 60
                remaining = deadline - clock()
                if remaining > 0:
                    sleep(min(interval, remaining))
        except KeyboardInterrupt:
            publish({'event': 'watch_stopped', 'reason': 'interrupted', 'new_messages': count})
            return
        except (BridgeError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            publish({'event': 'watch_failed', 'error': str(exc), 'new_messages': count})
            raise
        publish({'event': 'watch_stopped', 'reason': 'deadline', 'new_messages': count,
                 'degraded': any(failures.values()), 'last_successful_poll': last_poll})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', help='repeat for each local mailbox')
    parser.add_argument('--recipient', choices=sorted(ROLES), default='smith')
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--interval', type=int, default=10)
    parser.add_argument('--seen-file', required=True, help='local dedup checkpoint; not the mailbox')
    parser.add_argument('--events-file', required=True, help='durable local JSONL notification journal')
    args = parser.parse_args()
    try:
        watch(args.db or [DEFAULT_DB], args.recipient, args.seconds, args.interval, args.seen_file, args.events_file)
        return 0
    except (BridgeError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        print(canonical({'ok': False, 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
