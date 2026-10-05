"""Run a synthetic, real-CLI exchange in a new local directory."""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / 'bridge.py'


def run(root):
    workspace = root / 'synthetic-workspace'
    workspace.mkdir()
    database = root / 'demo.sqlite3'
    transcript = []

    def cli(*args):
        command = [sys.executable, str(SCRIPT), '--db', str(database), *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        transcript.append({'argv': command, 'exit_code': result.returncode,
                           'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()})
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    cli('init', '--approve-workspace', str(workspace))
    ident = str(uuid.uuid4())
    request = {
        'schema_version': 1, 'message_id': ident, 'request_id': ident,
        'thread_id': str(uuid.uuid4()), 'correlation_id': None, 'kind': 'request',
        'sender': 'smith', 'recipient': 'claude',
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'repository': {'path': str(workspace.resolve()), 'commit': '0' * 40},
        'scope': {'description': 'Synthetic protocol exercise only.', 'paths': ['sample.txt']},
        'acceptance_criteria': ['Return a synthetic response without executing message text.'],
        'body': 'Synthetic request. The all-zero commit is a test fixture, not a real repository revision.',
        'attachments': [],
    }
    request_path = root / 'request.json'
    request_path.write_text(json.dumps(request, indent=2), encoding='utf-8')
    cli('send', '--actor', 'smith', '--file', str(request_path))
    duplicate = cli('send', '--actor', 'smith', '--file', str(request_path))
    assert duplicate['duplicate']
    cli('list', '--recipient', 'claude')
    cli('read', ident)
    claim = cli('claim', '--actor', 'claude', '--lease-seconds', '300')
    cli('status', ident, '--actor', 'claude', '--set', 'working', '--expected-revision', '1', '--reason', 'Synthetic exercise started')
    response = copy.deepcopy(request)
    response.update(message_id=str(uuid.uuid4()), kind='reply', correlation_id=ident,
                    sender='claude', recipient='smith',
                    created_at=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
                    body='Synthetic response: protocol exercise complete. No repository actions were authorized or executed.')
    reply_path = root / 'reply.json'
    reply_path.write_text(json.dumps(response, indent=2), encoding='utf-8')
    cli('status', ident, '--actor', 'claude', '--set', 'ready_for_review', '--expected-revision', '2', '--reason', 'Synthetic evidence ready')
    args = ('reply', '--actor', 'claude', '--in-reply-to', ident, '--token', claim['claim_token'], '--file', str(reply_path))
    cli(*args)
    assert cli(*args)['duplicate']
    received = cli('claim', '--actor', 'smith')
    assert received['message'] == response
    ack_args = ('ack', response['message_id'], '--actor', 'smith', '--token', received['claim_token'])
    cli(*ack_args)
    assert cli(*ack_args)['duplicate']
    cli('status', ident, '--actor', 'smith', '--set', 'closed', '--expected-revision', '3', '--reason', 'Synthetic test assertion passed; not a code review approval')
    final = cli('status', ident)
    assert final['tasks'][0]['state'] == 'closed'
    assert all(row['delivery'] == 'acked' for row in cli('list')['messages'])
    cli('audit')
    transcript_path = root / 'transcript.json'
    transcript_path.write_text(json.dumps(transcript, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'cli_invocations': len(transcript), 'state': 'closed',
                      'messages': 2, 'database': str(database), 'transcript': str(transcript_path)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='new directory for durable synthetic demo artifacts; must not exist')
    args = parser.parse_args()
    if args.output:
        target = Path(args.output).resolve()
        target.mkdir(parents=True, exist_ok=False)
        run(target)
    else:
        with tempfile.TemporaryDirectory(prefix='agent-bridge-demo-') as temp:
            run(Path(temp))
        print('Temporary synthetic demo artifacts removed. Use --output to retain them.')
