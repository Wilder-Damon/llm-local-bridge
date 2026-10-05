import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_bridge import request
from bridge import BridgeError, Mailbox
from watch import event_key, load_seen, pending, save_seen, watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-watch-test-')
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.box = Mailbox(self.root / 'mailbox.sqlite3')
        self.box.init([str(self.workspace)])
        self.message = request(self.workspace)
        self.message['sender'], self.message['recipient'] = 'claude', 'smith'
        self.box.put(self.message, 'claude')
        self.seen = self.root / 'seen.json'
        self.events = self.root / 'events.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def run_watch(self, duration=3):
        current, emitted = [0], []
        def sleep(seconds):
            current[0] += seconds
        watch([self.box.path], 'smith', duration, 1, self.seen, self.events,
              emit=emitted.append, clock=lambda: current[0], sleep=sleep)
        return emitted

    def test_read_only_snapshot_has_scope_not_body_or_tokens(self):
        before = self.box.audit_log()
        rows = pending(self.box.path, 'smith')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['scope'], self.message['scope'])
        for key in ('body', 'payload', 'attachments', 'lease_token', 'claim_token'):
            self.assertNotIn(key, rows[0])
        self.assertEqual(self.box.audit_log(), before)
        self.assertEqual(self.box.read(self.message['message_id'])['delivery'], 'queued')

    def test_dedup_across_polls_and_restart_and_new_arrival(self):
        first = self.run_watch()
        self.assertEqual(sum(e['event'] == 'message_available' for e in first), 1)
        second = self.run_watch()
        self.assertEqual(sum(e['event'] == 'message_available' for e in second), 0)
        other = request(self.workspace)
        other['sender'], other['recipient'] = 'claude', 'smith'
        self.box.put(other, 'claude')
        notifications = [e for e in self.run_watch() if e['event'] == 'message_available']
        self.assertEqual([e['message_id'] for e in notifications], [other['message_id']])

    def test_bounded_deadline_and_persisted_journal(self):
        events = self.run_watch(3)
        self.assertEqual(events[-1]['event'], 'watch_stopped')
        self.assertEqual(events[-1]['reason'], 'deadline')
        self.assertEqual([json.loads(line) for line in self.events.read_text().splitlines()], events)
        self.assertEqual(len(load_seen(self.seen)), 1)

    def test_acknowledged_delivery_is_not_reported(self):
        claim = self.box.claim('smith')
        self.box.ack(self.message['message_id'], 'smith', claim['claim_token'])
        self.assertEqual(pending(self.box.path, 'smith'), [])

    def test_seed_previously_delivered_notifications(self):
        self.run_watch()  # A seen checkpoint must retain its durable journal.
        save_seen(self.seen, {event_key(pending(self.box.path, 'smith')[0])})
        self.assertFalse(any(e['event'] == 'message_available' for e in self.run_watch()))

    def test_invalid_bounds_and_database_path_collisions_rejected(self):
        for seconds, interval in [(0, 1), (3601, 1), (30, 0), (30, 61)]:
            with self.assertRaises(BridgeError):
                watch([self.box.path], 'smith', seconds, interval, self.seen, self.events)
        with self.assertRaises(BridgeError):
            watch([self.box.path], 'smith', 1, 1, self.box.path, self.events)

    def test_missing_database_never_created(self):
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            pending(missing, 'smith')
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
