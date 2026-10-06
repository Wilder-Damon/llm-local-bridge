"""Measure synthetic CLI-shaped UTF-8 bytes; no models, live data or token estimates."""
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Mailbox, canonical, now


def size(result):
    return len((canonical({'ok': True, **result}) + '\n').encode('utf-8'))


def main():
    with tempfile.TemporaryDirectory(prefix='bridge-changes-measure-') as temporary:
        root = Path(temporary)
        workspace = root / 'synthetic-workspace'
        workspace.mkdir()
        box = Mailbox(root / 'synthetic.sqlite3')
        box.init([str(workspace)])
        ids = []
        def send():
            ident = str(uuid.uuid4())
            message = {'schema_version': 1, 'message_id': ident, 'request_id': ident,
                'thread_id': str(uuid.uuid4()), 'correlation_id': None, 'kind': 'request',
                'sender': 'smith', 'recipient': 'claude', 'created_at': now(),
                'repository': {'path': str(workspace), 'commit': '0' * 40},
                'scope': {'description': 'Synthetic measurement only.', 'paths': ['sample.txt']},
                'acceptance_criteria': ['Read the complete synthetic evidence.'],
                'body': ('Synthetic evidence; preserve all review conditions. ' * 80), 'attachments': []}
            box.put(message, 'smith')
            ids.append(ident)
        for _ in range(12):
            send()
        initial_audit = box.health()['audit_sequence']
        cached, after, anchor = {}, 0, None
        direct_bytes = delta_bytes = full_reads = 0
        phases = []
        for phase in ('initial', 'progress_only', 'one_new_message'):
            if phase == 'progress_only':
                box.claim('claude', message_id=ids[0])
                box.status(ids[0], 'claude', 'working', 1, 'Accepted synthetic task')
            elif phase == 'one_new_message':
                send()
            direct = sum(size(box.read(ident)) for ident in ids)
            delta, manifest, fetched = 0, 0, 0
            while True:
                page = box.changes(after, 100, anchor)
                manifest += size(page)
                for row in page['messages']:
                    if cached.get(row['message_id']) != row['digest']:
                        delta += size(box.read(row['message_id']))
                        cached[row['message_id']] = row['digest']
                        fetched += 1
                after, anchor = page['next_after'], page['anchor']
                if not page['has_more']:
                    break
            delta += manifest
            phases.append({'phase': phase, 'direct_full_read_bytes': direct,
                           'changes_bytes': manifest, 'changes_plus_required_reads_bytes': delta,
                           'required_full_reads': fetched})
            direct_bytes += direct
            delta_bytes += delta
            full_reads += fetched
        print(json.dumps({'fixture': '12 synthetic requests, a progress change, then one new request',
            'measurement': 'canonical CLI-shaped UTF-8 response bytes including newline; not tokens or provider cost',
            'initial_audit_sequence': initial_audit, 'phases': phases,
            'three_scan_full_read_bytes': direct_bytes,
            'three_scan_changes_plus_required_reads_bytes': delta_bytes,
            'reduction_percent': round(100 * (1 - delta_bytes / direct_bytes), 2),
            'direct_full_reads': 37, 'cached_full_reads': full_reads,
            'cold_start_extra_bytes': phases[0]['changes_bytes']}, indent=2))


if __name__ == '__main__':
    main()
