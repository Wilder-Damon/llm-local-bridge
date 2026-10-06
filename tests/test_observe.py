"""Observation tests use short-lived synthetic probes and temporary mailboxes only."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_bridge
from bridge import BridgeError
from observe import observe, run_probe

SCRIPT = Path(__file__).resolve().parents[1] / 'observe.py'


class ObserveTests(unittest.TestCase):
    setUp = test_bridge.MailboxTests.setUp
    tearDown = test_bridge.MailboxTests.tearDown

    def test_real_bounded_run_retains_output_exit_and_finalization(self):
        output = self.root / 'observation'
        before = self.box.health()['audit_sequence']
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--db', str(self.box.path),
                                 '--seconds', '1', '--interval', '10', '--output', str(output)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        final = json.loads((output / 'result.json').read_text())
        self.assertEqual((final['exit_code'], final['reason'], final['probes']), (0, 'deadline', 1))
        exit_record = json.loads((output / 'probe-000001.exit.json').read_text())
        self.assertEqual(exit_record['exit_code'], 0)
        self.assertIsNotNone(exit_record['pid'])
        self.assertEqual((output / 'probe-000001.stderr.log').read_bytes(), b'')
        self.assertTrue(json.loads((output / 'probe-000001.stdout.log').read_text())['health'][0]['read_only'])
        self.assertEqual(self.box.health()['audit_sequence'], before)

    def test_nonzero_health_exit_retained_and_no_automatic_retry(self):
        output = self.root / 'failure'
        absent = self.root / 'absent.sqlite3'
        result = observe([absent], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5), output)
        self.assertEqual((result['exit_code'], result['reason'], result['probes']), (2, 'probe_failed', 1))
        self.assertEqual(json.loads((output / 'probe-000001.exit.json').read_text())['exit_code'], 2)
        self.assertFalse(json.loads((output / 'probe-000001.stderr.log').read_text())['ok'])
        self.assertFalse(absent.exists())

    def test_timeout_kills_and_reaps_only_owned_probe_retaining_partial_output(self):
        output = self.root / 'timeout'
        output.mkdir()
        real_popen = subprocess.Popen
        children = []
        def synthetic_probe(command, **kwargs):
            self.assertFalse(kwargs['shell'])
            child = real_popen([sys.executable, '-c',
                'import time; print("partial-output", flush=True); time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        with patch('observe.subprocess.Popen', side_effect=synthetic_probe):
            start = time.monotonic()
            result = run_probe([str(self.box.path)], output, 1, 0.5)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['ok'])
        self.assertIsNotNone(children[0].poll())
        self.assertIn(b'partial-output', (output / 'probe-000001.stdout.log').read_bytes())
        self.assertTrue(json.loads((output / 'probe-000001.exit.json').read_text())['timed_out'])

    def test_interrupt_and_probe_exception_have_distinct_final_results(self):
        for error, code, reason in [(KeyboardInterrupt(), 130, 'interrupted'),
                                     (RuntimeError('synthetic failure'), 2, 'observation_error')]:
            output = self.root / reason
            with self.subTest(reason=reason), patch('observe.run_probe', side_effect=error):
                result = observe([self.box.path], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5), output)
            self.assertEqual((result['exit_code'], result['reason']), (code, reason))
            self.assertEqual(json.loads((output / 'result.json').read_text())['exit_code'], code)

    def test_existing_output_and_past_deadline_are_preserved(self):
        output = self.root / 'existing'
        output.mkdir()
        marker = output / 'result.json'
        marker.write_bytes(b'preserve this')
        with self.assertRaises(FileExistsError):
            observe([self.box.path], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5), output)
        self.assertEqual(marker.read_bytes(), b'preserve this')
        with self.assertRaises(BridgeError):
            observe([self.box.path], dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1), self.root / 'new')
        self.assertFalse((self.root / 'new').exists())

    def test_timeout_is_capped_by_remaining_window(self):
        captured = []
        def stalled(databases, output, index, timeout):
            captured.append(timeout)
            return {'ok': False, 'timed_out': True}
        with patch('observe.run_probe', side_effect=stalled):
            result = observe([self.box.path], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=0.5),
                             self.root / 'short', timeout=60)
        self.assertLessEqual(captured[0], 0.5)
        self.assertEqual((result['reason'], result['probes']), ('probe_timeout', 1))

    def test_output_cannot_create_a_database_sidecar_or_contain_a_database(self):
        absent = self.root / 'absent.sqlite3'
        for output, source in [(absent, absent), (Path(str(absent) + '-wal'), absent),
                               (self.root / 'new', self.root / 'new' / 'db.sqlite3')]:
            with self.subTest(output=output), self.assertRaises(BridgeError):
                observe([source], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5), output)
            self.assertFalse(output.exists())

    def test_zero_exit_with_malformed_response_is_not_healthy(self):
        real_popen = subprocess.Popen
        def invalid_probe(command, **kwargs):
            return real_popen([sys.executable, '-c', 'print("not a health response")'], **kwargs)
        output = self.root / 'invalid-response'
        with patch('observe.subprocess.Popen', side_effect=invalid_probe):
            result = observe([self.box.path], dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5), output)
        self.assertEqual((result['exit_code'], result['reason']), (2, 'observation_error'))
        self.assertEqual(json.loads((output / 'probe-000001.exit.json').read_text())['exit_code'], 0)
        self.assertEqual(json.loads((output / 'result.json').read_text())['exit_code'], 2)


if __name__ == '__main__':
    unittest.main()
