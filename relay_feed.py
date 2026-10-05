"""Read-only local journal reader for an active agent's supported-tool relay."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import uuid
from bridge import VERSION, canonical, require


def feed(events_path, databases):
    allowed = {str(Path(path).resolve()).casefold() for path in databases}
    path = Path(events_path)
    require(path.stat().st_size <= 16 * 1024 * 1024, 'event journal oversized; inspect manually')
    result = []
    raw = path.read_bytes()
    lines = raw.split(b'\n')[:-1]
    # A writer may be between its JSON write and newline/flush. Retry that last
    # incomplete record on the next read instead of treating it as corruption.
    for line in lines:
        item = json.loads(line)
        require(isinstance(item, dict), 'invalid event journal record')
        if item.get('event') == 'message_available':
            database = Path(item['database']).resolve()
            require(str(database).casefold() in allowed, 'journal names an unapproved relay database')
            require(str(uuid.UUID(item['message_id'])) == item['message_id'], 'invalid message ID')
            if item.get('scope', {}).get('description') == 'Heartbeat only. No work requested and no change to any repository.':
                db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)
                try:
                    db.execute('PRAGMA query_only=ON')
                    require(db.execute('PRAGMA user_version').fetchone()[0] == VERSION, 'unsupported database version')
                    row = db.execute('SELECT payload FROM messages WHERE message_id=?', (item['message_id'],)).fetchone()
                    message = json.loads(row[0]) if row else {}
                finally:
                    db.close()
                body = message.get('body', '')
                if message.get('sender') == 'claude' and re.match(r'^Heartbeat from Claude at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\.', body):
                    item['routine_heartbeat'] = True
                    item['heartbeat_status'] = re.sub(r'^Heartbeat from Claude at \S+Z\.', 'Heartbeat from Claude at <timestamp>.', body)
                    item['heartbeat_signature'] = canonical({'body': item['heartbeat_status'], 'repository': message['repository']})
                    item['heartbeat_received_at'] = item['received_at']
        result.append(item)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events-file', required=True)
    parser.add_argument('--db', action='append', required=True)
    args = parser.parse_args()
    print(canonical({'events': feed(args.events_file, args.db)}))
