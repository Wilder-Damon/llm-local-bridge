"""Failure injection uses only synthetic temporary workspaces and mailboxes."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from bridge import BridgeError, Mailbox, canonical, storage_busy
from local_io import distinct_state_paths, exclusive_state
from relay_feed import feed
import test_bridge
import test_watch
from test_bridge import request, reply_to
from viewer import events, follow, load_cursor
from watch import pending, save_seen, watch

ROOT = Path(__file__).resolve().parents[1]


def busy_error():
    error = sqlite3.OperationalError('synthetic database is locked')
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


class ReliabilityTests(unittest.TestCase):
    setUp = test_watch.WatchTests.setUp
    tearDown = test_watch.WatchTests.tearDown
    run_watch = test_watch.WatchTests.run_watch

    def test_torn_journal_restart_preserves_bytes_and_checkpoint(self):
        self.run_watch()
        before_seen = self.seen.read_bytes()
        with self.events.open('ab') as output:
            output.write(b'{"event":"message_avail')
        before = self.events.read_bytes()
        with self.assertRaisesRegex(BridgeError, 'incomplete'):
            self.run_watch()
        self.assertEqual(self.events.read_bytes(), before)
        self.assertEqual(self.seen.read_bytes(), before_seen)

    def test_corrupt_complete_journal_record_is_preserved(self):
        self.events.write_bytes(b'not-json\n')
        with self.assertRaises(ValueError):
            self.run_watch()
        self.assertEqual(self.events.read_bytes(), b'not-json\n')
        self.assertFalse(self.seen.exists())

    def test_missing_journal_with_checkpoint_does_not_silently_skip(self):
        self.run_watch()
        self.events.unlink()  # synthetic lost file only
        with self.assertRaisesRegex(BridgeError, 'journal is missing'):
            self.run_watch()
        self.assertFalse(self.events.exists())

    def test_crash_between_journal_and_checkpoint_replays_same_id(self):
        with patch('watch.save_seen', side_effect=OSError('synthetic checkpoint failure')):
            with self.assertRaises(OSError):
                self.run_watch()
        first = [e for e in feed(self.events, [self.box.path]) if e['event'] == 'message_available']
        self.assertEqual(len(first), 1)
        self.assertFalse(self.seen.exists())
        self.run_watch()
        notifications = [e for e in feed(self.events, [self.box.path]) if e['event'] == 'message_available']
        self.assertEqual(len(notifications), 2)
        self.assertEqual(notifications[0]['notification_id'], notifications[1]['notification_id'])

    def test_oversized_checkpoint_is_rejected_before_replacement(self):
        self.run_watch()
        before = self.seen.read_bytes()
        with self.assertRaisesRegex(BridgeError, 'oversized'):
            save_seen(self.seen, {'x' * (16 * 1024 * 1024)})
        self.assertEqual(self.seen.read_bytes(), before)

    def test_busy_retries_back_off_and_report_recovery(self):
        current, emitted, attempts = [0], [], []
        real_pending = pending
        def poll(database, recipient, timeout=5):
            attempts.append(current[0])
            if len(attempts) <= 3:
                raise busy_error()
            return real_pending(database, recipient, timeout)
        def sleep(seconds):
            current[0] += seconds
        with patch('watch.pending', side_effect=poll):
            watch([self.box.path], 'smith', 10, 1, self.seen, self.events,
                  emit=emitted.append, clock=lambda: current[0], sleep=sleep)
        self.assertEqual(attempts[:4], [0, 1, 3, 7])
        self.assertEqual([e['retry_in_seconds'] for e in emitted if e['event'] == 'watch_retry'], [1, 2, 4])
        self.assertEqual(sum(e['event'] == 'watch_recovered' for e in emitted), 1)
        self.assertEqual(sum(e['event'] == 'message_available' for e in emitted), 1)
        self.assertFalse(emitted[-1]['degraded'])
        self.assertEqual(current[0], 10)

    def test_one_busy_source_does_not_block_other_and_deadline_stays_bounded(self):
        current, emitted = [0], []
        blocked = self.root / 'blocked.sqlite3'
        def poll(database, recipient, timeout=5):
            if Path(database) == blocked:
                raise busy_error()
            return pending(database, recipient, timeout)
        def sleep(seconds):
            current[0] += seconds
        with patch('watch.pending', side_effect=poll):
            watch([blocked, self.box.path], 'smith', 65, 1, self.seen, self.events,
                  emit=emitted.append, clock=lambda: current[0], sleep=sleep)
        self.assertEqual(current[0], 65)
        self.assertEqual(sum(e['event'] == 'message_available' for e in emitted), 1)
        retries = [e for e in emitted if e['event'] == 'watch_retry']
        self.assertEqual([e['retry_in_seconds'] for e in retries], [1, 2, 4, 8, 16, 32, 60])
        self.assertTrue(emitted[-1]['degraded'])
        self.assertEqual(sum(e['event'] == 'watch_health' for e in emitted), 1)

    def test_non_busy_storage_failure_stops_without_retry(self):
        with patch('watch.pending', side_effect=sqlite3.DatabaseError('synthetic corruption')):
            with self.assertRaises(sqlite3.DatabaseError):
                self.run_watch()
        rows = feed(self.events, [self.box.path])
        self.assertEqual(rows[-1]['event'], 'watch_failed')
        self.assertFalse(any(row['event'] == 'watch_retry' for row in rows))
        self.assertFalse(self.seen.exists())

    def test_slow_read_does_not_emit_messages_after_deadline(self):
        current, emitted = [0], []
        def slow_poll(*args, **kwargs):
            current[0] += 2
            return pending(self.box.path, 'smith')
        with patch('watch.pending', side_effect=slow_poll):
            watch([self.box.path], 'smith', 1, 1, self.seen, self.events,
                  emit=emitted.append, clock=lambda: current[0], sleep=lambda _: None)
        self.assertFalse(any(e['event'] == 'message_available' for e in emitted))
        self.assertFalse(self.seen.exists())

    def test_sqlite_real_read_lock_has_retryable_code(self):
        other = Mailbox(self.root / 'locked.sqlite3')
        other.init([str(self.workspace)])
        db = sqlite3.connect(other.path)
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(sqlite3.OperationalError) as raised:
                pending(other.path, 'smith', timeout=0.01)
            self.assertTrue(storage_busy(raised.exception))
        finally:
            db.rollback()
            db.close()
        self.assertEqual(pending(other.path, 'smith'), [])

    def test_output_paths_cannot_overwrite_database_sidecars(self):
        for suffix in ('', '-wal', '-shm', '-journal'):
            output = Path(str(self.box.path) + suffix)
            with self.subTest(suffix=suffix):
                with self.assertRaises(BridgeError):
                    watch([self.box.path], 'smith', 1, 1, output, self.events)
                with self.assertRaises(BridgeError):
                    watch([self.box.path], 'smith', 1, 1, self.seen, output)
                with self.assertRaises(BridgeError):
                    follow(self.box.path, seconds=1, cursor=output)

    def test_existing_hard_link_alias_of_database_is_rejected(self):
        alias = self.root / 'alias.json'
        os.link(self.box.path, alias)
        with self.assertRaises(BridgeError):
            distinct_state_paths([self.box.path], [alias])

    def test_duplicate_watcher_state_fails_before_journal_changes(self):
        self.run_watch()
        before = self.events.read_bytes(), self.seen.read_bytes()
        # Both checkpoint and journal independently prohibit another owner.
        for path in (self.seen, self.events):
            with exclusive_state([Path(str(path) + '.lock')]):
                with self.assertRaisesRegex(BridgeError, 'already in use'):
                    self.run_watch()
        self.assertEqual((self.events.read_bytes(), self.seen.read_bytes()), before)
        self.run_watch()  # normal exit released both locks

    def test_abrupt_owner_exit_releases_process_lock(self):
        lock = Path(str(self.seen) + '.lock')
        marker = self.root / 'lock-ready'
        code = ("import sys,time; from pathlib import Path; from local_io import exclusive_state; "
                "ctx=exclusive_state([Path(sys.argv[1])]); ctx.__enter__(); "
                "Path(sys.argv[2]).write_text('ready'); time.sleep(20)")
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(lock), str(marker)],
                                 cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            end = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < end:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), 'synthetic lock-holder did not become ready')
            with self.assertRaises(BridgeError):
                self.run_watch()
            child.kill()
            child.wait(timeout=5)
            self.run_watch()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_viewer_ahead_cursor_is_detected_and_preserved(self):
        cursor = self.root / 'viewer.json'
        text = canonical({'version': 1, 'database': str(self.box.path), 'sequence': 999})
        cursor.write_text(text)
        with self.assertRaisesRegex(BridgeError, 'ahead'):
            follow(self.box.path, seconds=1, cursor=cursor, emit=lambda _: None)
        self.assertEqual(cursor.read_text(), text)

    def test_partial_utf8_journal_tail_is_deferred(self):
        self.run_watch()
        before = feed(self.events, [self.box.path])
        with self.events.open('ab') as output:
            output.write(b'{"event":"note","body":"\xe2')
        self.assertEqual(feed(self.events, [self.box.path]), before)

    def test_unknown_schema_heartbeat_read_is_rejected_without_migration(self):
        message = request(self.workspace)
        message.update(sender='claude', recipient='smith',
                       body='Heartbeat from Claude at 2026-10-05T14:22:57Z. Synthetic.')
        message['scope']['description'] = 'Heartbeat only. No work requested and no change to any repository.'
        self.box.put(message, 'claude')
        self.run_watch()
        db = sqlite3.connect(self.box.path)
        db.execute('PRAGMA user_version=99')
        db.close()
        with self.assertRaisesRegex(BridgeError, 'unsupported database version'):
            feed(self.events, [self.box.path])
        db = sqlite3.connect(self.box.path)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)
        db.close()

    def test_health_is_read_only_and_omits_payloads_tokens(self):
        claim = self.box.claim('smith')
        before = self.box.audit_log()
        health = self.box.health()
        self.assertEqual(health['quick_check'], 'ok')
        self.assertEqual(health['delivery_counts'], {'claimed': 1})
        self.assertEqual(self.box.audit_log(), before)
        self.assertNotIn(claim['claim_token'], canonical(health))
        self.assertNotIn(self.message['body'], canonical(health))
        self.assertNotIn(self.message['message_id'], canonical(health))

    def test_health_missing_corrupt_and_newer_databases_preserved(self):
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.Error):
            Mailbox(missing).health()
        self.assertFalse(missing.exists())
        corrupt = self.root / 'corrupt.db'
        corrupt.write_bytes(b'Synthetic invalid SQLite bytes')
        with self.assertRaises(sqlite3.Error):
            Mailbox(corrupt).health()
        self.assertEqual(corrupt.read_bytes(), b'Synthetic invalid SQLite bytes')
        db = sqlite3.connect(self.box.path)
        db.execute('PRAGMA user_version=99')
        db.close()
        for operation in (self.box.connect, self.box.health):
            with self.assertRaises(BridgeError):
                operation()
        # Explicit rejection closes connections: no leaked open database handle.
        renamed = self.box.path.with_suffix('.renamed')
        self.box.path.rename(renamed)
        renamed.rename(self.box.path)


class PeerAndTransactionTests(unittest.TestCase):
    setUp = test_bridge.MailboxTests.setUp
    tearDown = test_bridge.MailboxTests.tearDown

    def test_either_peer_can_execute_and_other_review(self):
        for sender, recipient in [('smith', 'claude'), ('claude', 'smith')]:
            with self.subTest(sender=sender):
                message = request(self.workspace)
                message.update(sender=sender, recipient=recipient)
                self.box.put(message, sender)
                claim = self.box.claim(recipient, message_id=message['message_id'])
                rid = message['request_id']
                self.box.status(rid, recipient, 'working', 1, 'Synthetic authorized implementation')
                self.box.status(rid, recipient, 'ready_for_review', 2, 'Synthetic evidence ready')
                response = reply_to(message)
                self.box.put(response, recipient, message['message_id'], claim['claim_token'])
                review = self.box.claim(sender, message_id=response['message_id'])
                self.box.ack(response['message_id'], sender, review['claim_token'])
                self.box.status(rid, sender, 'closed', 3, 'Synthetic independent review complete')
                self.assertEqual(self.box.status(rid)['tasks'][0]['state'], 'closed')

    def test_expired_claim_renew_rejected_and_reply_retry_survives_restart(self):
        self.box.put(self.message, 'smith')
        with patch('bridge.time.time', return_value=100):
            claim = self.box.claim('claude', 1)
        with patch('bridge.time.time', return_value=102):
            with self.assertRaises(BridgeError):
                self.box.renew(self.message['message_id'], 'claude', claim['claim_token'], 300)
            renewed = self.box.claim('claude')
            response = reply_to(self.message)
            self.box.put(response, 'claude', self.message['message_id'], renewed['claim_token'])
        before = self.box.audit_log()
        restarted = Mailbox(self.box.path)
        result = restarted.put(response, 'claude', self.message['message_id'], renewed['claim_token'])
        self.assertTrue(result['duplicate'])
        self.assertEqual(restarted.audit_log(), before)

    def test_busy_write_rolls_back_then_identical_retry_commits_once(self):
        lock = sqlite3.connect(self.box.path)
        lock.execute('BEGIN IMMEDIATE')
        real_connect = self.box.connect
        def short_connect():
            db = real_connect()
            db.execute('PRAGMA busy_timeout=10')
            return db
        try:
            with patch.object(self.box, 'connect', side_effect=short_connect):
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    self.box.put(self.message, 'smith')
            self.assertTrue(storage_busy(raised.exception))
            self.assertEqual(self.box.list()['messages'], [])
        finally:
            lock.rollback()
            lock.close()
        self.assertFalse(self.box.put(self.message, 'smith')['duplicate'])
        self.assertTrue(self.box.put(copy.deepcopy(self.message), 'smith')['duplicate'])
        self.assertEqual(len(self.box.audit_log()['events']), 2)


if __name__ == '__main__':
    unittest.main()
