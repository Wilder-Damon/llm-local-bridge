#!/usr/bin/env python3
"""Local, inert, transactional agent mailbox. Python standard library only."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
import time
import uuid

VERSION = 1
MAX_BYTES = 65536
MAX_REPLIES = 12
ROLES = {'smith', 'claude', 'human'}
STATES = {'requested', 'acknowledged', 'working', 'blocked', 'ready_for_review', 'closed'}
TRANSITIONS = {
    'requested': {'acknowledged', 'blocked'},
    'acknowledged': {'working', 'blocked'},
    'working': {'blocked', 'ready_for_review'},
    'blocked': {'working'},
    'ready_for_review': {'working', 'closed'},
    'closed': set(),
}
DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'mailbox.sqlite3'


class BridgeError(Exception):
    pass


def storage_busy(exc):
    return (isinstance(exc, sqlite3.Error)
            and (getattr(exc, 'sqlite_errorcode', 0) & 255)
            in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


def require(ok, message):
    if not ok:
        raise BridgeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, 'input exceeds 65536 bytes')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=strict_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(BridgeError('non-finite JSON number')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError(f'invalid UTF-8 JSON: {exc}') from exc


def text_field(value, name, limit=8192):
    require(isinstance(value, str) and 0 < len(value) <= limit, f'invalid {name}')
    require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value), f'control character in {name}')


def uuid_field(value, name):
    require(isinstance(value, str), f'invalid {name}')
    try:
        require(str(uuid.UUID(value)) == value, f'{name} must be a canonical UUID')
    except ValueError as exc:
        raise BridgeError(f'invalid {name}') from exc


def relative_path(value):
    text_field(value, 'relative path', 512)
    require('\\' not in value and ':' not in value and '*' not in value and '?' not in value,
            'paths must use relative forward-slash syntax without wildcards or drive names')
    parts = value.split('/')
    require(not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).is_absolute(), 'absolute reference rejected')
    require(all(p not in {'', '.', '..'} and not p.endswith((' ', '.')) for p in parts), 'path traversal or ambiguous path rejected')
    require(not any(re.match(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', p, re.I) for p in parts), 'Windows reserved path rejected')
    require(not any(c in value for c in '<>"|\r\n\t'), 'invalid path character')


def local_workspace(value):
    text_field(value, 'workspace', 1024)
    path = Path(value)
    require(path.is_absolute() and not str(value).startswith(('\\\\', '//')), 'workspace must be an absolute local path')
    require('..' not in path.parts, 'workspace traversal rejected')
    require(path.is_dir(), 'approved workspace must already exist')
    return str(path.resolve())


def validate(message, approved):
    require(isinstance(message, dict), 'message must be an object')
    fields = {'schema_version', 'message_id', 'request_id', 'thread_id', 'correlation_id', 'kind',
              'sender', 'recipient', 'created_at', 'repository', 'scope', 'acceptance_criteria', 'body', 'attachments'}
    require(set(message) == fields, 'missing or unknown message fields')
    require(type(message['schema_version']) is int and message['schema_version'] == VERSION, 'unsupported schema version')
    for key in ('message_id', 'request_id', 'thread_id'):
        uuid_field(message[key], key)
    require(message['kind'] in ('request', 'reply'), 'invalid kind')
    require(message['sender'] in ROLES and message['recipient'] in ROLES and message['sender'] != message['recipient'], 'invalid sender/recipient roles')
    if message['kind'] == 'request':
        require(message['message_id'] == message['request_id'] and message['correlation_id'] is None, 'request must be its own root')
    else:
        uuid_field(message['correlation_id'], 'correlation_id')
    stamp = message['created_at']
    require(isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z', stamp), 'created_at must be UTC RFC3339 ending Z')
    try:
        dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError as exc:
        raise BridgeError('invalid timestamp') from exc
    repo = message['repository']
    require(isinstance(repo, dict) and set(repo) == {'path', 'commit'}, 'invalid repository')
    resolved = local_workspace(repo['path'])
    require(resolved in approved, 'repository is not an explicitly approved workspace')
    require(repo['path'] == resolved, 'repository path must be canonical absolute path')
    require(isinstance(repo['commit'], str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', repo['commit']), 'commit must be exact lowercase 40/64 hex SHA')
    scope = message['scope']
    require(isinstance(scope, dict) and set(scope) == {'description', 'paths'}, 'invalid scope')
    text_field(scope['description'], 'scope description', 4096)
    require(isinstance(scope['paths'], list) and 1 <= len(scope['paths']) <= 64, 'scope needs 1..64 paths')
    require(isinstance(message['attachments'], list) and len(message['attachments']) <= 16, 'too many attachments')
    refs = list(scope['paths'])
    for attachment in message['attachments']:
        require(isinstance(attachment, dict) and set(attachment) == {'path', 'description'}, 'invalid attachment')
        text_field(attachment['description'], 'attachment description', 1024)
        refs.append(attachment['path'])
    for ref in refs:
        relative_path(ref)
        require((Path(resolved) / ref).resolve().is_relative_to(Path(resolved)), 'reference escapes approved workspace through link')
    criteria = message['acceptance_criteria']
    require(isinstance(criteria, list) and 1 <= len(criteria) <= 32, 'acceptance criteria needs 1..32 items')
    for item in criteria:
        text_field(item, 'acceptance criterion', 2048)
    text_field(message['body'], 'body', 32768)
    require(len(canonical(message).encode('utf-8')) <= MAX_BYTES, 'normalized message exceeds 65536 bytes')


DDL = '''
CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks(request_id TEXT PRIMARY KEY, thread_id TEXT UNIQUE NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE messages(message_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES tasks(request_id),
 payload TEXT NOT NULL, digest TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
 received_at TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'queued',
 lease_token TEXT, lease_until REAL, claimed_by TEXT, acked_at TEXT);
CREATE INDEX inbox ON messages(recipient, delivery, received_at);
CREATE TABLE audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
 actor TEXT NOT NULL, event TEXT NOT NULL, message_id TEXT, details TEXT NOT NULL);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
'''


class Mailbox:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path).resolve()
        require(not str(self.path).startswith(('\\\\', '//')), 'mailbox must be on a local disk, not a network share')

    def connect(self):
        require(self.path.is_file(), 'mailbox is not initialized; run init first')
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            require(db.execute('PRAGMA user_version').fetchone()[0] == VERSION, 'unsupported database version')
            return db
        except BaseException:
            db.close()
            raise

    def health(self):
        """Explicit read-only snapshot; does not inspect message bodies or claim tokens."""
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            require(version == VERSION, 'unsupported database version')
            check = [row[0] for row in db.execute('PRAGMA quick_check')]
            require(check == ['ok'], 'database integrity check failed; preserve files and stop')
            require(db.execute('PRAGMA foreign_key_check').fetchone() is None,
                    'database foreign key check failed; preserve files and stop')
            stamp = time.time()
            counts = dict(db.execute('SELECT delivery,COUNT(*) FROM messages GROUP BY delivery'))
            expired = db.execute("SELECT COUNT(*) FROM messages WHERE delivery='claimed' AND lease_until<=?", (stamp,)).fetchone()[0]
            claimable = db.execute("""SELECT COUNT(*) FROM messages m JOIN tasks t USING(request_id)
                WHERE t.state!='closed' AND (m.delivery='queued' OR (m.delivery='claimed' AND m.lease_until<=?))""", (stamp,)).fetchone()[0]
            return {'database': str(self.path), 'checked_at': now(), 'read_only': True,
                    'schema_version': version, 'quick_check': 'ok',
                    'journal_mode': db.execute('PRAGMA journal_mode').fetchone()[0],
                    'delivery_counts': counts, 'expired_claims': expired, 'claimable': claimable,
                    'audit_sequence': db.execute('SELECT COALESCE(MAX(sequence),0) FROM audit').fetchone()[0]}
        finally:
            db.close()

    def init(self, workspaces):
        approved = sorted(set(local_workspace(p) for p in workspaces))
        require(approved, 'at least one approved workspace is required')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serializes competing initializers; schema and version commit together.
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version:
                require(version == VERSION, 'unsupported database version')
                existing = json.loads(db.execute("SELECT value FROM config WHERE key='workspaces'").fetchone()[0])
                require(existing == approved, 'init will not change workspace authorization; use a separate mailbox')
                db.commit()
                db.execute('PRAGMA journal_mode=WAL')
                return {'initialized': True, 'existing': True, 'database': str(self.path), 'workspaces': approved}
            require(not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), 'refusing to initialize nonempty unknown database')
            # execute individually: executescript would implicitly commit our transaction.
            statement = ''
            for line in DDL.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
            db.execute('INSERT INTO config VALUES (?,?)', ('workspaces', canonical(approved)))
            db.execute(f'PRAGMA user_version={VERSION}')
            self.audit(db, 'operator', 'initialized', None, {'workspaces': approved})
            db.commit()
            db.execute('PRAGMA journal_mode=WAL')
            return {'initialized': True, 'existing': False, 'database': str(self.path), 'workspaces': approved}
        finally:
            db.close()

    @staticmethod
    def audit(db, actor, event, message_id, details):
        db.execute('INSERT INTO audit(at,actor,event,message_id,details) VALUES (?,?,?,?,?)',
                   (now(), actor, event, message_id, canonical(details)))

    def write(self, action):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            result = action(db)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def row(db, message_id):
        row = db.execute('SELECT * FROM messages WHERE message_id=?', (message_id,)).fetchone()
        require(row is not None, 'message not found')
        return row

    @staticmethod
    def check_lease(row, actor, token):
        require(row['recipient'] == actor and row['claimed_by'] == actor and row['lease_token'] == token,
                'claim token or recipient mismatch')
        require(row['delivery'] == 'claimed' and row['lease_until'] > time.time(), 'claim expired or already acknowledged')

    def put(self, message, actor, parent_id=None, token=None):
        def action(db):
            approved = json.loads(db.execute("SELECT value FROM config WHERE key='workspaces'").fetchone()[0])
            validate(message, approved)
            require(message['sender'] == actor, 'actor must equal sender')
            require((message['kind'] == 'reply') == (parent_id is not None), 'replies require the reply command and claim token')
            payload = canonical(message)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            existing = db.execute('SELECT digest FROM messages WHERE message_id=?', (message['message_id'],)).fetchone()
            parent = None
            if parent_id is not None:
                require(message['correlation_id'] == parent_id, 'correlation must name the claimed message')
                parent = self.row(db, parent_id)
                require(parent['recipient'] == actor and parent['lease_token'] == token and parent['claimed_by'] == actor, 'claim token or recipient mismatch')
            if existing:
                require(existing['digest'] == digest, 'message ID conflicts with different content')
                return {'message_id': message['message_id'], 'duplicate': True}
            if parent is None:
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                           (message['request_id'], message['thread_id'], actor, message['recipient'], 'requested', 0, now(), 'request received'))
            else:
                self.check_lease(parent, actor, token)
                original = json.loads(parent['payload'])
                for key in ('request_id', 'thread_id', 'repository', 'scope', 'acceptance_criteria'):
                    require(message[key] == original[key], f'reply cannot change {key}')
                require(message['recipient'] == original['sender'], 'reply must return to the previous sender')
                task = db.execute('SELECT state FROM tasks WHERE request_id=?', (message['request_id'],)).fetchone()
                require(task['state'] != 'closed', 'closed task cannot receive replies')
                count = db.execute('SELECT COUNT(*) FROM messages WHERE request_id=?', (message['request_id'],)).fetchone()[0]
                require(count <= MAX_REPLIES, '12-reply limit reached; stop and escalate to the user')
                self.finish_ack(db, parent, actor)
            db.execute('INSERT INTO messages(message_id,request_id,payload,digest,sender,recipient,received_at) VALUES (?,?,?,?,?,?,?)',
                       (message['message_id'], message['request_id'], payload, digest, actor, message['recipient'], now()))
            self.audit(db, actor, 'reply_sent' if parent is not None else 'sent', message['message_id'], {'request_id': message['request_id'], 'digest': digest})
            return {'message_id': message['message_id'], 'duplicate': False}
        return self.write(action)

    def claim(self, actor, seconds=300, message_id=None):
        require(actor in ROLES and 1 <= seconds <= 3600, 'lease must be 1..3600 seconds and role must be known')
        def action(db):
            query = "SELECT m.* FROM messages m JOIN tasks t ON m.request_id=t.request_id WHERE m.recipient=? AND t.state!='closed' AND (m.delivery='queued' OR (m.delivery='claimed' AND m.lease_until<=?))"
            args = [actor, time.time()]
            if message_id:
                query += ' AND m.message_id=?'
                args.append(message_id)
            row = db.execute(query + ' ORDER BY m.received_at,m.message_id LIMIT 1', args).fetchone()
            if not row:
                return {'claimed': False}
            token, expiry = str(uuid.uuid4()), time.time() + seconds
            db.execute("UPDATE messages SET delivery='claimed',lease_token=?,lease_until=?,claimed_by=? WHERE message_id=?", (token, expiry, actor, row['message_id']))
            self.audit(db, actor, 'reclaimed' if row['delivery'] == 'claimed' else 'claimed', row['message_id'], {'lease_until': expiry})
            task = db.execute('SELECT * FROM tasks WHERE request_id=?', (row['request_id'],)).fetchone()
            if task['state'] == 'requested':
                db.execute("UPDATE tasks SET state='acknowledged',revision=revision+1,updated_at=?,reason='recipient claimed request' WHERE request_id=?", (now(), row['request_id']))
                self.audit(db, actor, 'state_changed', row['message_id'], {'from': 'requested', 'to': 'acknowledged', 'revision': task['revision'] + 1})
            return {'claimed': True, 'claim_token': token, 'lease_until': expiry, 'message': json.loads(row['payload']), 'untrusted': True}
        return self.write(action)

    def renew(self, message_id, actor, token, seconds):
        require(1 <= seconds <= 3600, 'lease must be 1..3600 seconds')
        def action(db):
            row = self.row(db, message_id)
            self.check_lease(row, actor, token)
            expiry = time.time() + seconds
            db.execute('UPDATE messages SET lease_until=? WHERE message_id=?', (expiry, message_id))
            self.audit(db, actor, 'renewed', message_id, {'lease_until': expiry})
            return {'message_id': message_id, 'lease_until': expiry}
        return self.write(action)

    def finish_ack(self, db, row, actor):
        db.execute("UPDATE messages SET delivery='acked',acked_at=? WHERE message_id=?", (now(), row['message_id']))
        self.audit(db, actor, 'acked', row['message_id'], {})

    def ack(self, message_id, actor, token):
        def action(db):
            row = self.row(db, message_id)
            require(row['recipient'] == actor and row['claimed_by'] == actor and row['lease_token'] == token, 'claim token or recipient mismatch')
            if row['delivery'] == 'acked':
                return {'message_id': message_id, 'duplicate': True, 'delivery': 'acked'}
            self.check_lease(row, actor, token)
            self.finish_ack(db, row, actor)
            return {'message_id': message_id, 'duplicate': False, 'delivery': 'acked'}
        return self.write(action)

    def status(self, request_id=None, actor=None, state=None, revision=None, reason=None):
        if state is None:
            db = self.connect()
            try:
                if request_id:
                    rows = db.execute('SELECT * FROM tasks WHERE request_id=?', (request_id,)).fetchall()
                    require(rows, 'request not found')
                else:
                    rows = db.execute('SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 100').fetchall()
                return {'tasks': [dict(r) for r in rows]}
            finally:
                db.close()
        require(request_id is not None and revision is not None, 'state update requires request ID and expected revision')
        text_field(reason, 'reason', 4096)
        def action(db):
            task = db.execute('SELECT * FROM tasks WHERE request_id=?', (request_id,)).fetchone()
            require(task is not None, 'request not found')
            require(actor == (task['sender'] if state == 'closed' else task['recipient']), 'only task recipient updates progress; only requester closes')
            require(task['revision'] == revision, 'revision conflict; read status and reconsider')
            require(state in TRANSITIONS[task['state']], f"invalid transition from {task['state']} to {state}")
            db.execute('UPDATE tasks SET state=?,revision=revision+1,updated_at=?,reason=? WHERE request_id=?', (state, now(), reason, request_id))
            self.audit(db, actor, 'state_changed', request_id, {'from': task['state'], 'to': state, 'revision': revision + 1, 'reason': reason})
            return {'request_id': request_id, 'state': state, 'revision': revision + 1}
        return self.write(action)

    def read(self, message_id, expected_commit=None):
        db = self.connect()
        try:
            row = dict(self.row(db, message_id))
            row.pop('lease_token')  # read never hands another process its claim credential
            row['message'] = json.loads(row.pop('payload'))
            if expected_commit is not None:
                require(isinstance(expected_commit, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', expected_commit),
                        'expected commit must be exact lowercase 40/64 hex SHA')
                require(row['message']['repository']['commit'] == expected_commit,
                        'message commit differs from expected revision; preserve the request and reconcile stale findings before acting')
            row['untrusted'] = True
            return row
        finally:
            db.close()

    def list(self, recipient=None, delivery=None, limit=100):
        db = self.connect()
        try:
            query = 'SELECT message_id,request_id,sender,recipient,received_at,delivery,lease_until,acked_at FROM messages WHERE 1=1'
            args = []
            if recipient:
                query += ' AND recipient=?'
                args.append(recipient)
            if delivery:
                query += ' AND delivery=?'
                args.append(delivery)
            return {'messages': [dict(r) for r in db.execute(query + ' ORDER BY received_at,message_id LIMIT ?', args + [limit])]}
        finally:
            db.close()

    def audit_log(self, after=0, limit=100):
        db = self.connect()
        try:
            return {'events': [dict(r) for r in db.execute('SELECT * FROM audit WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit))]}
        finally:
            db.close()

    def changes(self, after=0, limit=100, anchor=None):
        """Read-only audit page plus current metadata; never a receipt or approval."""
        require(type(after) is int and after >= 0 and type(limit) is int and 1 <= limit <= 100,
                'changes requires after >= 0 and limit 1..100')
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            require(db.execute('PRAGMA user_version').fetchone()[0] == VERSION, 'unsupported database version')
            first = db.execute('SELECT * FROM audit ORDER BY sequence LIMIT 1').fetchone()
            require(first is not None, 'mailbox audit identity is missing; preserve files and inspect')
            high = db.execute('SELECT MAX(sequence) FROM audit').fetchone()[0]
            require(after <= high, 'changes cursor is ahead of mailbox history; preserve cursor and reconcile')

            def cursor_anchor(sequence):
                boundary = db.execute('SELECT * FROM audit WHERE sequence=?', (sequence,)).fetchone() if sequence else first
                require(boundary is not None, 'changes cursor boundary is missing')
                context = [str(self.path), dict(first), dict(boundary)]
                return hashlib.sha256(canonical(context).encode('utf-8')).hexdigest()

            require(after == 0 or anchor is not None, 'resuming changes requires the saved --anchor')
            if anchor is not None:
                require(anchor == cursor_anchor(after), 'changes cursor identity mismatch; preserve cursor and reconcile')
            page = list(db.execute('SELECT sequence,event,message_id FROM audit WHERE sequence>? ORDER BY sequence LIMIT ?',
                                   (after, limit)))
            ids = list(dict.fromkeys(row['message_id'] for row in page if row['message_id'] is not None))
            messages = []
            for message_id in ids:
                row = db.execute('''SELECT m.message_id,m.request_id,m.sender,m.recipient,m.delivery,
                    m.digest,m.payload,t.recipient AS owner,t.state AS task_state,t.revision AS task_revision,
                    t.reason AS task_reason
                    FROM messages m JOIN tasks t USING(request_id) WHERE m.message_id=?''', (message_id,)).fetchone()
                require(row is not None, 'audit references missing message; preserve files and inspect')
                item = dict(row)
                payload = json.loads(item.pop('payload'))
                item.update(kind=payload['kind'], correlation_id=payload['correlation_id'],
                            repository=payload['repository'])
                messages.append(item)
            next_after = page[-1]['sequence'] if page else after
            return {'events': [dict(row) for row in page], 'messages': messages,
                    'next_after': next_after, 'anchor': cursor_anchor(next_after),
                    'snapshot_sequence': high, 'has_more': next_after < high,
                    'read_only': True, 'untrusted': True}
        finally:
            db.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default=str(DEFAULT_DB), help='local SQLite mailbox (default: beside this script in data/)')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('health', help='read-only integrity and delivery-count snapshot; no message bodies or tokens')
    init = sub.add_parser('init')
    init.add_argument('--approve-workspace', action='append', required=True)
    for name in ('send', 'reply'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--file', required=True, help='explicit JSON input file; never executed')
        cmd.add_argument('--actor', choices=sorted(ROLES), required=True)
        if name == 'reply':
            cmd.add_argument('--in-reply-to', required=True)
            cmd.add_argument('--token', required=True)
    cmd = sub.add_parser('list')
    cmd.add_argument('--recipient', choices=sorted(ROLES))
    cmd.add_argument('--delivery', choices=['queued', 'claimed', 'acked'])
    cmd.add_argument('--limit', type=int, default=100)
    cmd = sub.add_parser('read')
    cmd.add_argument('message_id')
    cmd.add_argument('--expected-commit', help='reject a handoff pinned to a different exact revision')
    cmd = sub.add_parser('changes', help='read-only changed-message metadata; bodies and tokens omitted')
    cmd.add_argument('--after', type=int, default=0)
    cmd.add_argument('--anchor', help='saved cursor identity; required when after > 0')
    cmd.add_argument('--limit', type=int, default=100, help='audit events per page, 1..100')
    cmd = sub.add_parser('claim')
    cmd.add_argument('--actor', choices=sorted(ROLES), required=True)
    cmd.add_argument('--message-id')
    cmd.add_argument('--lease-seconds', type=int, default=300)
    for name in ('ack', 'renew'):
        cmd = sub.add_parser(name)
        cmd.add_argument('message_id')
        cmd.add_argument('--actor', choices=sorted(ROLES), required=True)
        cmd.add_argument('--token', required=True)
        if name == 'renew':
            cmd.add_argument('--lease-seconds', type=int, default=300)
    cmd = sub.add_parser('status')
    cmd.add_argument('request_id', nargs='?')
    cmd.add_argument('--actor', choices=sorted(ROLES))
    cmd.add_argument('--set', choices=sorted(STATES), dest='state')
    cmd.add_argument('--expected-revision', type=int)
    cmd.add_argument('--reason')
    cmd = sub.add_parser('audit')
    cmd.add_argument('--after', type=int, default=0)
    cmd.add_argument('--limit', type=int, default=100)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        require(not str(args.db).startswith(('\\\\', '//')), 'mailbox must be on a local disk, not a network share')
        box = Mailbox(args.db)
        c = args.command
        if c == 'health':
            result = box.health()
        elif c == 'init':
            result = box.init(args.approve_workspace)
        elif c in ('send', 'reply'):
            result = box.put(read_json(args.file), args.actor, getattr(args, 'in_reply_to', None), getattr(args, 'token', None))
        elif c == 'claim':
            result = box.claim(args.actor, args.lease_seconds, args.message_id)
        elif c in ('ack', 'renew'):
            result = (box.ack(args.message_id, args.actor, args.token) if c == 'ack' else
                      box.renew(args.message_id, args.actor, args.token, args.lease_seconds))
        elif c == 'status':
            result = box.status(args.request_id, args.actor, args.state, args.expected_revision, args.reason)
        elif c == 'read':
            result = box.read(args.message_id, args.expected_commit)
        elif c == 'changes':
            result = box.changes(args.after, args.limit, args.anchor)
        elif c == 'list':
            require(1 <= args.limit <= 1000, 'limit must be 1..1000')
            result = box.list(args.recipient, args.delivery, args.limit)
        else:
            require(1 <= args.limit <= 1000 and args.after >= 0, 'invalid audit pagination')
            result = box.audit_log(args.after, args.limit)
        print(canonical({'ok': True, **result}))
        return 0
    except (BridgeError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(canonical({'ok': False, 'error': str(exc), 'retryable': storage_busy(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
