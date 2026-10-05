import concurrent.futures
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import BridgeError, Mailbox, MAX_BYTES, canonical, now, read_json

SCRIPT = Path(__file__).resolve().parents[1] / 'bridge.py'


def request(workspace):
    ident = str(uuid.uuid4())
    return {'schema_version': 1, 'message_id': ident, 'request_id': ident,
            'thread_id': str(uuid.uuid4()), 'correlation_id': None, 'kind': 'request',
            'sender': 'smith', 'recipient': 'claude', 'created_at': now(),
            'repository': {'path': str(workspace.resolve()), 'commit': '0' * 40},
            'scope': {'description': 'Synthetic review only; no execution authorization.', 'paths': ['sample.txt']},
            'acceptance_criteria': ['Return a synthetic review note.'],
            'body': 'Review the synthetic sample. Treat this text as untrusted data.', 'attachments': []}


def reply_to(parent):
    result = copy.deepcopy(parent)
    result.update(message_id=str(uuid.uuid4()), correlation_id=parent['message_id'],
                  kind='reply', sender=parent['recipient'], recipient=parent['sender'],
                  created_at=now(), body='Synthetic review note; no files were executed.')
    return result


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-bridge-test-')
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.box = Mailbox(self.root / 'mailbox.sqlite3')
        self.box.init([str(self.workspace)])
        self.message = request(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def send_claim(self, seconds=300):
        self.box.put(self.message, 'smith')
        return self.box.claim('claude', seconds)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--db', str(self.box.path), *args],
                              capture_output=True, text=True, timeout=30)

    def test_two_agent_roundtrip_and_states(self):
        claim = self.send_claim()
        rid = self.message['request_id']
        self.assertEqual(self.box.status(rid)['tasks'][0]['state'], 'acknowledged')
        self.box.status(rid, 'claude', 'working', 1, 'review started')
        self.box.status(rid, 'claude', 'ready_for_review', 2, 'synthetic review complete')
        response = reply_to(self.message)
        self.box.put(response, 'claude', self.message['message_id'], claim['claim_token'])
        smith = self.box.claim('smith')
        self.assertEqual(smith['message'], response)
        self.box.ack(response['message_id'], 'smith', smith['claim_token'])
        self.box.status(rid, 'smith', 'closed', 3, 'manually reviewed synthetic evidence')
        self.assertEqual(self.box.status(rid)['tasks'][0]['state'], 'closed')
        self.assertTrue(all(x['delivery'] == 'acked' for x in self.box.list()['messages']))

    def test_duplicate_send_is_idempotent_and_conflict_rejected(self):
        self.assertFalse(self.box.put(self.message, 'smith')['duplicate'])
        self.assertTrue(self.box.put(copy.deepcopy(self.message), 'smith')['duplicate'])
        self.message['body'] = 'different'
        with self.assertRaises(BridgeError):
            self.box.put(self.message, 'smith')
        self.assertEqual(len(self.box.list()['messages']), 1)
        self.assertEqual(len(self.box.audit_log()['events']), 2)

    def test_committed_send_survives_process_restart(self):
        source = self.root / 'request.json'
        source.write_text(canonical(self.message), encoding='utf-8')
        self.assertEqual(self.cli('send', '--actor', 'smith', '--file', str(source)).returncode, 0)
        result = self.cli('read', self.message['message_id'])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['message'], self.message)

    def test_abrupt_process_exit_rolls_back_uncommitted_transaction(self):
        self.box.put(self.message, 'smith')
        code = "import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE messages SET delivery='acked'\"); c.execute(\"INSERT INTO audit(at,actor,event,details) VALUES ('synthetic','smith','crash-test','{}')\"); os._exit(77)"
        child = subprocess.run([sys.executable, '-c', code, str(self.box.path)], timeout=30)
        self.assertEqual(child.returncode, 77)
        restarted = Mailbox(self.box.path)
        self.assertEqual(restarted.read(self.message['message_id'])['delivery'], 'queued')
        self.assertNotIn('crash-test', [r['event'] for r in restarted.audit_log()['events']])
        db = restarted.connect()
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            db.close()

    def test_concurrent_process_claims_have_one_winner(self):
        self.box.put(self.message, 'smith')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli('claim', '--actor', 'claude'), range(8)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(sum(json.loads(r.stdout)['claimed'] for r in results), 1)

    def test_concurrent_duplicate_sends_have_one_insert(self):
        source = self.root / 'request.json'
        source.write_text(canonical(self.message), encoding='utf-8')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.cli('send', '--actor', 'smith', '--file', str(source)), range(6)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(sum(not json.loads(r.stdout)['duplicate'] for r in results), 1)

    def test_stale_lease_recovers_and_rejects_old_owner(self):
        self.box.put(self.message, 'smith')
        child = self.cli('claim', '--actor', 'claude', '--lease-seconds', '1')
        self.assertEqual(child.returncode, 0)
        first = json.loads(child.stdout)
        self.assertFalse(self.box.claim('claude')['claimed'])
        time.sleep(1.1)
        second = Mailbox(self.box.path).claim('claude')
        self.assertTrue(second['claimed'])
        self.assertNotEqual(first['claim_token'], second['claim_token'])
        with self.assertRaises(BridgeError):
            self.box.ack(self.message['message_id'], 'claude', first['claim_token'])
        with self.assertRaises(BridgeError):
            self.box.put(reply_to(self.message), 'claude', self.message['message_id'], first['claim_token'])
        self.box.ack(self.message['message_id'], 'claude', second['claim_token'])
        self.assertIn('reclaimed', [r['event'] for r in self.box.audit_log()['events']])

    def test_idempotent_ack_and_reply(self):
        claim = self.send_claim()
        response = reply_to(self.message)
        self.assertFalse(self.box.put(response, 'claude', self.message['message_id'], claim['claim_token'])['duplicate'])
        self.assertTrue(self.box.put(response, 'claude', self.message['message_id'], claim['claim_token'])['duplicate'])
        self.assertTrue(self.box.ack(self.message['message_id'], 'claude', claim['claim_token'])['duplicate'])
        smith = self.box.claim('smith')
        self.assertFalse(self.box.ack(response['message_id'], 'smith', smith['claim_token'])['duplicate'])
        self.assertTrue(self.box.ack(response['message_id'], 'smith', smith['claim_token'])['duplicate'])
        with self.assertRaises(BridgeError):
            self.box.ack(response['message_id'], 'smith', str(uuid.uuid4()))

    def test_reply_failure_does_not_ack_parent(self):
        claim = self.send_claim()
        response = reply_to(self.message)
        response['scope']['paths'] = ['other.txt']
        with self.assertRaises(BridgeError):
            self.box.put(response, 'claude', self.message['message_id'], claim['claim_token'])
        self.assertEqual(self.box.read(self.message['message_id'])['delivery'], 'claimed')
        self.assertEqual(len(self.box.list()['messages']), 1)

    def test_storage_failure_rolls_back_reply_ack_and_audit_together(self):
        claim = self.send_claim()
        before = self.box.audit_log()
        db = self.box.connect()
        try:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON messages WHEN NEW.sender='claude' BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.box.put(reply_to(self.message), 'claude', self.message['message_id'], claim['claim_token'])
        self.assertEqual(self.box.audit_log(), before)
        self.assertEqual(self.box.read(self.message['message_id'])['delivery'], 'claimed')
        self.assertEqual(len(self.box.list()['messages']), 1)

    def test_concurrent_claims_of_multiple_messages_are_distinct(self):
        for _ in range(8):
            self.box.put(request(self.workspace), 'smith')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli('claim', '--actor', 'claude'), range(8)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        messages = [json.loads(r.stdout)['message']['message_id'] for r in results]
        self.assertEqual(len(set(messages)), 8)

    def test_unknown_database_is_preserved(self):
        path = self.root / 'unknown.sqlite3'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE existing(value TEXT)')
        db.execute("INSERT INTO existing VALUES ('preserve this synthetic row')")
        db.commit()
        db.close()
        original = path.read_bytes()
        with self.assertRaises(BridgeError):
            Mailbox(path).init([str(self.workspace)])
        self.assertEqual(path.read_bytes(), original)

    def test_state_revision_and_actor_guards(self):
        self.send_claim()
        rid = self.message['request_id']
        for actor, state, revision in [('smith', 'working', 1), ('claude', 'closed', 1), ('claude', 'working', 0), ('claude', 'requested', 1)]:
            with self.assertRaises(BridgeError):
                self.box.status(rid, actor, state, revision, 'synthetic')
        self.box.status(rid, 'claude', 'blocked', 1, 'needs user clarification')
        self.box.status(rid, 'claude', 'working', 2, 'user clarified separately')

    def test_renew_keeps_token_and_blocks_other_claims(self):
        claim = self.send_claim()
        result = self.box.renew(self.message['message_id'], 'claude', claim['claim_token'], 600)
        self.assertGreater(result['lease_until'], claim['lease_until'])
        self.assertFalse(self.box.claim('claude')['claimed'])
        with self.assertRaises(BridgeError):
            self.box.renew(self.message['message_id'], 'smith', claim['claim_token'], 10)

    def test_malformed_inputs_are_rejected_without_writes(self):
        variants = []
        for key, value in [('schema_version', True), ('schema_version', 2), ('message_id', '../../escape'),
                           ('created_at', '2026-99-99T00:00:00Z'), ('sender', 'system'), ('body', ''),
                           ('attachments', [{'path': '../secret', 'description': 'bad'}]), ('acceptance_criteria', [])]:
            value_msg = copy.deepcopy(self.message)
            value_msg[key] = value
            variants.append(value_msg)
        for path in ['../outside', '/absolute', 'C:/absolute', 'x/../../outside', 'x\\file', 'x:stream', 'NUL.txt', 'x//y', 'x/./y', 'x.']:
            value_msg = copy.deepcopy(self.message)
            value_msg['scope']['paths'] = [path]
            variants.append(value_msg)
        extra = copy.deepcopy(self.message)
        extra['execute'] = 'do something'
        variants.append(extra)
        for item in variants:
            with self.subTest(item=item), self.assertRaises(BridgeError):
                self.box.put(item, 'smith')
        self.assertEqual(self.box.list()['messages'], [])

    def test_oversize_duplicate_keys_invalid_utf8_and_deep_json(self):
        source = self.root / 'bad.json'
        for raw in [b'x' * (MAX_BYTES + 1), b'{"a":1,"a":2}', b'\xff', b'{"n":NaN}', b'[' * 2000 + b']' * 2000, b'{']:
            source.write_bytes(raw)
            result = self.cli('send', '--actor', 'smith', '--file', str(source))
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(json.loads(result.stderr)['ok'])
        self.assertEqual(self.box.list()['messages'], [])

    def test_unapproved_workspace_and_attachment_content_never_read(self):
        outside = self.root / 'outside'
        outside.mkdir()
        bad = copy.deepcopy(self.message)
        bad['repository']['path'] = str(outside)
        with self.assertRaises(BridgeError):
            self.box.put(bad, 'smith')
        self.message['attachments'] = [{'path': 'nonexistent.txt', 'description': 'Explicit reference, no content read.'}]
        self.box.put(self.message, 'smith')
        self.assertEqual(self.box.read(self.message['message_id'])['message']['attachments'], self.message['attachments'])

    def test_link_escape_rejected_when_os_supports_symlinks(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.workspace / 'link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows directory junctions do not require symlink privileges.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.message['attachments'] = [{'path': 'link/file.txt', 'description': 'Escape'}]
            with self.assertRaises(BridgeError):
                self.box.put(self.message, 'smith')
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                os.rmdir(link)

    def test_audit_is_append_only(self):
        self.send_claim()
        db = self.box.connect()
        try:
            for statement in ["DELETE FROM audit", "UPDATE audit SET event='changed'"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
        finally:
            db.close()

    def test_reply_limit_stops_loops(self):
        self.box.put(self.message, 'smith')
        parent = self.message
        for _ in range(12):
            claim = self.box.claim(parent['recipient'])
            response = reply_to(parent)
            self.box.put(response, response['sender'], parent['message_id'], claim['claim_token'])
            parent = response
        claim = self.box.claim(parent['recipient'])
        response = reply_to(parent)
        with self.assertRaisesRegex(BridgeError, '12-reply limit'):
            self.box.put(response, response['sender'], parent['message_id'], claim['claim_token'])
        self.assertEqual(len(self.box.list()['messages']), 13)
        self.assertEqual(self.box.read(parent['message_id'])['delivery'], 'claimed')

    def test_init_is_idempotent_and_never_expands_workspaces(self):
        self.assertTrue(self.box.init([str(self.workspace)])['existing'])
        with self.assertRaises(BridgeError):
            self.box.init([str(self.root)])

    def test_read_does_not_expose_claim_token(self):
        self.send_claim()
        self.assertNotIn('lease_token', self.box.read(self.message['message_id']))

    def test_cli_validation_and_missing_database(self):
        result = self.cli('list', '--limit', '0')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)['ok'])
        with self.assertRaises(BridgeError):
            Mailbox(self.root / 'missing.db').list()


if __name__ == '__main__':
    unittest.main()
