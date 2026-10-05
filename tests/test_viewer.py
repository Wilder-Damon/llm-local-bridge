import json
import unittest
import test_watch
from test_bridge import reply_to
from viewer import events, follow, render, safe


class ViewerTests(unittest.TestCase):
    setUp = test_watch.WatchTests.setUp
    tearDown = test_watch.WatchTests.tearDown
    def test_render_escapes_controls_and_limits_body(self):
        self.message['body'] = 'hello\nnext\tline'
        row = events(self.box.path)[-1]
        payload = json.loads(row['payload'])
        payload['body'] = 'hello\n\x1b[31m\r\tunsafe'
        row['payload'] = json.dumps(payload)
        line = render(row, 500)
        self.assertNotIn('\x1b', line)
        self.assertNotIn('\n', line)
        self.assertNotIn('\r', line)
        self.assertIn('\\u001b', line)
        self.assertIn('...', render(row, 3))
        self.assertEqual(safe('\x07'), '\\u0007')

    def test_both_directions_state_events_and_no_claim_token(self):
        claimed = self.box.claim('smith')
        self.box.status(self.message['request_id'], 'smith', 'working', 1, 'synthetic')
        reply = reply_to(self.message)
        self.box.put(reply, 'smith', self.message['message_id'], claimed['claim_token'])
        before = self.box.audit_log()
        lines = '\n'.join(render(row) for row in events(self.box.path))
        self.assertIn('claude->smith', lines)
        self.assertIn('smith->claude', lines)
        self.assertIn('acknowledged -> working', lines)
        self.assertNotIn(claimed['claim_token'], lines)
        self.assertEqual(before, self.box.audit_log())

    def test_dedup_resume_and_empty_state(self):
        cursor = self.root / 'viewer.json'
        def invoke():
            current, output = [0], []
            def sleep(seconds):
                current[0] += seconds
            follow(self.box.path, seconds=3, interval=1, cursor=cursor,
                   emit=output.append, clock=lambda: current[0], sleep=sleep)
            return output
        first = invoke()
        self.assertEqual(sum(line.startswith('#') for line in first), 2)
        second = invoke()
        self.assertFalse(any(line.startswith('#') for line in second))
        self.assertEqual(sum(line.startswith('Waiting') for line in second), 1)
        self.assertIn('window ended', second[-1])


if __name__ == '__main__':
    unittest.main()
