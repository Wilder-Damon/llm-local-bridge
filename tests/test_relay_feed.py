import json
import unittest
import test_watch
from relay_feed import feed


class RelayFeedTests(unittest.TestCase):
    setUp = test_watch.WatchTests.setUp
    tearDown = test_watch.WatchTests.tearDown
    run_watch = test_watch.WatchTests.run_watch

    def test_ordinary_message_not_classified_as_heartbeat(self):
        self.run_watch()
        rows = feed(self.events, [self.box.path])
        msg = next(row for row in rows if row['event'] == 'message_available')
        self.assertNotIn('routine_heartbeat', msg)
        self.assertEqual(self.box.read(self.message['message_id'])['delivery'], 'queued')

    def test_heartbeat_timestamp_normalized_but_status_changes_preserved(self):
        from test_bridge import request
        signatures = []
        for stamp, status in [('2026-10-05T14:22:57Z', 'Watcher running.'),
                              ('2026-10-05T14:27:57Z', 'Watcher running.'),
                              ('2026-10-05T14:32:57Z', 'Watcher stopped.')]:
            message = request(self.workspace)
            message.update(sender='claude', recipient='smith', body=f'Heartbeat from Claude at {stamp}. {status}')
            message['scope']['description'] = 'Heartbeat only. No work requested and no change to any repository.'
            self.box.put(message, 'claude')
        self.run_watch()
        rows = feed(self.events, [self.box.path])
        signatures = [row['heartbeat_signature'] for row in rows if row.get('routine_heartbeat')]
        self.assertEqual(signatures[0], signatures[1])
        self.assertNotEqual(signatures[1], signatures[2])

    def test_journal_cannot_redirect_database_reads(self):
        self.run_watch()
        with self.assertRaises(Exception):
            feed(self.events, [self.root / 'unapproved.sqlite3'])

    def test_partial_final_journal_record_waits_for_next_read(self):
        self.run_watch()
        before = feed(self.events, [self.box.path])
        with self.events.open('a', encoding='utf-8') as output:
            output.write('{"event":"watch_started"')
        self.assertEqual(feed(self.events, [self.box.path]), before)
        with self.events.open('a', encoding='utf-8') as output:
            output.write('}\n')
        self.assertEqual(len(feed(self.events, [self.box.path])), len(before) + 1)


if __name__ == '__main__':
    unittest.main()
