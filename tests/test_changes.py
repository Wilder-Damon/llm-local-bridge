"""Changes feeds use synthetic temporary mailboxes, never operational state."""
import json
import sqlite3
import unittest

from bridge import BridgeError, Mailbox, canonical
import test_bridge
from test_bridge import request, reply_to


class ChangesTests(unittest.TestCase):
    setUp = test_bridge.MailboxTests.setUp
    tearDown = test_bridge.MailboxTests.tearDown
    cli = test_bridge.MailboxTests.cli

    def test_pagination_resume_and_quiet_feed(self):
        for _ in range(3):
            self.box.put(request(self.workspace), 'smith')
        after, anchor, events = 0, None, []
        while True:
            page = self.box.changes(after, 1, anchor)
            events.extend(row['sequence'] for row in page['events'])
            after, anchor = page['next_after'], page['anchor']
            if not page['has_more']:
                break
        self.assertEqual(events, [1, 2, 3, 4])
        self.assertEqual(self.box.changes(after, 1, anchor)['messages'], [])
        self.assertEqual(self.box.health()['audit_sequence'], 4)

    def test_resume_rejects_missing_wrong_ahead_or_cross_database_cursor(self):
        page = self.box.changes()
        for after, anchor in [(1, None), (1, 'bad'), (99, page['anchor'])]:
            with self.subTest(after=after, anchor=anchor), self.assertRaises(BridgeError):
                self.box.changes(after, 1, anchor)
        other = Mailbox(self.root / 'other.sqlite3')
        other.init([str(self.workspace)])
        with self.assertRaisesRegex(BridgeError, 'identity mismatch'):
            other.changes(page['next_after'], 1, page['anchor'])

    def test_changes_are_metadata_not_receipt_acceptance_or_review(self):
        self.message['body'] = 'PRIVATE BODY SENTINEL'
        self.box.put(self.message, 'smith')
        start = self.box.changes()
        claim = self.box.claim('claude')
        self.box.ack(self.message['message_id'], 'claude', claim['claim_token'])
        page = self.box.changes(start['next_after'], 100, start['anchor'])
        self.assertEqual(len(page['messages']), 1)
        row = page['messages'][0]
        self.assertEqual((row['delivery'], row['task_state']), ('acked', 'acknowledged'))
        self.assertEqual(row['owner'], 'claude')
        self.assertNotIn(claim['claim_token'], canonical(page))
        self.assertNotIn('PRIVATE BODY SENTINEL', canonical(page))
        self.assertNotIn('acceptance_criteria', row)
        self.assertNotIn('review_verdict', row)
        self.assertEqual(row['digest'], self.box.read(row['message_id'])['digest'])

    def test_reply_owner_stays_original_recipient_and_progress_is_current(self):
        self.box.put(self.message, 'smith')
        claim = self.box.claim('claude')
        self.box.status(self.message['request_id'], 'claude', 'working', 1, 'Accepted synthetic work')
        reply = reply_to(self.message)
        self.box.put(reply, 'claude', self.message['message_id'], claim['claim_token'])
        rows = self.box.changes()['messages']
        reply_row = next(r for r in rows if r['kind'] == 'reply')
        self.assertEqual(reply_row['recipient'], 'smith')
        self.assertEqual(reply_row['owner'], 'claude')
        self.assertEqual(reply_row['task_state'], 'working')
        self.assertEqual(reply_row['task_reason'], 'Accepted synthetic work')
        self.assertEqual(reply_row['correlation_id'], self.message['message_id'])

    def test_exact_commit_read_guard_does_not_mutate_or_discard_stale_request(self):
        self.box.put(self.message, 'smith')
        before = self.box.health()
        self.assertEqual(self.box.read(self.message['message_id'], '0' * 40)['message'], self.message)
        for commit in ['1' * 40, '0' * 7, 'A' * 40]:
            with self.subTest(commit=commit), self.assertRaises(BridgeError):
                self.box.read(self.message['message_id'], commit)
        self.assertEqual(self.box.health()['audit_sequence'], before['audit_sequence'])
        self.assertEqual(self.box.read(self.message['message_id'])['delivery'], 'queued')
        result = self.cli('read', self.message['message_id'], '--expected-commit', '1' * 40)
        self.assertEqual(result.returncode, 2)
        self.assertIn('differs', json.loads(result.stderr)['error'])

    def test_unknown_version_preserved_and_no_database_creation(self):
        db = sqlite3.connect(self.box.path)
        db.execute('PRAGMA user_version=77')
        db.close()
        before = self.box.path.read_bytes()
        with self.assertRaisesRegex(BridgeError, 'unsupported'):
            self.box.changes()
        self.assertEqual(self.box.path.read_bytes(), before)
        absent = self.root / 'absent.sqlite3'
        with self.assertRaises(sqlite3.Error):
            Mailbox(absent).changes()
        self.assertFalse(absent.exists())

    def test_cli_cursor_roundtrip_and_bounds(self):
        result = self.cli('changes', '--limit', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        page = json.loads(result.stdout)
        result = self.cli('changes', '--after', str(page['next_after']), '--anchor', page['anchor'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['events'], [])
        for after, limit in [(-1, 1), (0, 101), (0, 0), (True, 1)]:
            with self.subTest(after=after, limit=limit), self.assertRaises(BridgeError):
                self.box.changes(after, limit)


if __name__ == '__main__':
    unittest.main()
