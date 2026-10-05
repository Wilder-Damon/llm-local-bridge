"""Human-readable, read-only foreground audit viewer for both agent directions."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from bridge import BridgeError, VERSION, canonical, require
from local_io import distinct_state_paths

DEFAULT_PROJECT_DB = Path(__file__).resolve().parent / 'data' / 'mailbox.sqlite3'


def safe(value):
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def events(database, after=0, limit=100):
    path = Path(database).resolve()
    require(not str(path).startswith(('\\\\', '//')), 'viewer database must be local')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        require(db.execute('PRAGMA user_version').fetchone()[0] == VERSION, 'unsupported database version')
        latest = db.execute('SELECT COALESCE(MAX(sequence),0) FROM audit').fetchone()[0]
        require(after <= latest, 'viewer cursor is ahead of audit history; inspect database/restore before choosing a replay sequence')
        rows = db.execute('''SELECT a.sequence,a.at,a.actor,a.event,a.message_id,a.details,
                            m.request_id,m.sender,m.recipient,m.payload
                            FROM audit a LEFT JOIN messages m ON a.message_id=m.message_id
                            WHERE a.sequence>? ORDER BY a.sequence LIMIT ?''', (after, limit))
        return [dict(row) for row in rows]
    finally:
        db.close()


def render(row, body_chars=160):
    details = json.loads(row['details'])
    body = ''
    if row['event'] in ('sent', 'reply_sent') and row['payload']:
        body = json.loads(row['payload']).get('body', '')
    elif row['event'] == 'state_changed':
        body = f"{details.get('from', '?')} -> {details.get('to', '?')}; {details.get('reason', '')}"
    if len(body) > body_chars:
        body = body[:body_chars] + '...'
    route = f"{row['sender']}->{row['recipient']}" if row['sender'] else '-'
    return (f"#{row['sequence']} {safe(row['at'])} {safe(row['actor'])} {safe(row['event'])} {safe(route)} "
            f"request={safe(row['request_id'] or '-')} message={safe(row['message_id'] or '-')}"
            + (f' | {safe(body)}' if body else ''))


def load_cursor(path, database):
    if path is None or not path.exists():
        return 0
    require(path.stat().st_size <= 4096, 'viewer cursor oversized')
    value = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(value, dict) and type(value.get('version')) is int
            and value['version'] == 1 and value.get('database') == str(database), 'cursor belongs to a different database')
    require(type(value.get('sequence')) is int and value['sequence'] >= 0, 'invalid viewer cursor')
    return value['sequence']


def save_cursor(path, database, sequence):
    # Atomic replace/fsync utility, with a distinct cursor format from watcher dedup.
    import os
    import uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(canonical({'version': 1, 'database': str(database), 'sequence': sequence}))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def follow(database, seconds=1800, interval=2, after=None, cursor=None, body_chars=160,
           emit=print, clock=time.monotonic, sleep=time.sleep):
    database = Path(database).resolve()
    cursor = Path(cursor).resolve() if cursor else None
    require(1 <= seconds <= 3600 and 1 <= interval <= 60 and 1 <= body_chars <= 500, 'invalid viewer bounds')
    if cursor:
        distinct_state_paths([database], [cursor])
    require(after is None or (type(after) is int and after >= 0), 'invalid starting sequence')
    sequence = after if after is not None else load_cursor(cursor, database)
    deadline = clock() + seconds
    emit(f'Agent Bridge | READ ONLY | {safe(database)}')
    emit(f'Both directions; audit after #{sequence}; {seconds}s window; Ctrl+C stops. No claims, acknowledgments, or execution.')
    empty_reported = False
    try:
        while clock() < deadline:
            batch = events(database, sequence)
            for row in batch:
                emit(render(row, body_chars))
                sequence = row['sequence']
                if cursor:
                    save_cursor(cursor, database, sequence)
            if not batch and not empty_reported:
                emit(f'Waiting for new events after #{sequence} ...')
                empty_reported = True
            if batch:
                empty_reported = False
            remaining = deadline - clock()
            if remaining > 0 and len(batch) < 100:
                sleep(min(interval, remaining))
    except KeyboardInterrupt:
        emit(f'Viewer stopped by user; last sequence #{sequence}.')
        return sequence
    emit(f'Viewer window ended; last sequence #{sequence}. Run again to resume.')
    return sequence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_PROJECT_DB))
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--interval', type=int, default=2)
    parser.add_argument('--after', type=int, help='explicit audit sequence, 0 replays history')
    parser.add_argument('--cursor-file', help='optional resume file; never use a mailbox path')
    parser.add_argument('--body-chars', type=int, default=160)
    args = parser.parse_args()
    try:
        follow(args.db, args.seconds, args.interval, args.after, args.cursor_file, args.body_chars,
               emit=lambda line: print(line, flush=True))
        return 0
    except (BridgeError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        print('Viewer error: ' + safe(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
