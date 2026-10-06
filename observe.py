#!/usr/bin/env python3
"""Explicit bounded foreground health observation; never started by a mailbox event."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from bridge import BridgeError, Mailbox, canonical, now, require
from local_io import distinct_state_paths


def durable_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as output:
        output.write(canonical(value) + '\n')
        output.flush()
        os.fsync(output.fileno())


def run_probe(databases, output, index, timeout):
    """Only this fixed health-only child runs; no shell or message-supplied commands."""
    stdout_path = output / f'probe-{index:06d}.stdout.log'
    stderr_path = output / f'probe-{index:06d}.stderr.log'
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--_probe']
    for database in databases:
        command.extend(['--db', database])
    record = {'index': index, 'started_at': now(), 'timeout_seconds': timeout,
              'stdout': stdout_path.name, 'stderr': stderr_path.name,
              'pid': None, 'exit_code': None, 'timed_out': False, 'ok': False}
    with stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr:
        child = None
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, shell=False,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            record['pid'] = child.pid
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record['timed_out'] = True
                child.kill()
                child.wait()
        finally:
            # On an interrupt/error, reap only our own health child. It has no children.
            if child is not None:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                record['exit_code'] = child.returncode
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            record['finished_at'] = now()
            # Exit evidence precedes response validation; it makes no health verdict.
            durable_json(output / f'probe-{index:06d}.exit.json',
                         {key: value for key, value in record.items() if key != 'ok'})
    if record['exit_code'] == 0 and not record['timed_out']:
        require(stdout_path.stat().st_size <= 65536, 'health response exceeds limit')
        payload = json.loads(stdout_path.read_text(encoding='utf-8'))
        require(payload.get('ok') is True and len(payload.get('health', [])) == len(databases),
                'invalid health response; inspect retained output')
        require(all(row.get('read_only') is True and row.get('quick_check') == 'ok'
                    for row in payload['health']), 'invalid health result')
        record['ok'] = True
        record['health'] = payload['health']
    return record


def observe(databases, until, output, interval=60, timeout=5):
    require(isinstance(until, dt.datetime) and until.tzinfo is not None, 'deadline needs a timezone')
    remaining = (until - dt.datetime.now(dt.timezone.utc)).total_seconds()
    require(0 < remaining <= 28800, 'deadline must be in the next eight hours')
    end = time.monotonic() + remaining
    require(math.isfinite(interval) and 1 <= interval <= 3600, 'interval must be 1..3600 seconds')
    require(math.isfinite(timeout) and 0.1 <= timeout <= 60, 'probe timeout must be 0.1..60 seconds')
    sources = list(dict.fromkeys(str(Mailbox(path).path) for path in databases))
    require(1 <= len(sources) <= 8, 'observation requires 1..8 local mailboxes')
    output = Path(output).resolve()
    require(not str(output).startswith(('\\\\', '//')), 'observation output must be local')
    distinct_state_paths(sources, [output])
    require(all(not Path(source).is_relative_to(output) for source in sources),
            'observation output cannot contain a requested database')
    # A fresh directory prevents overwriting any existing logs, databases or sidecars.
    output.mkdir(parents=True, exist_ok=False)
    result = {'event': 'observation_finished', 'deadline': until.isoformat(),
              'pid': os.getpid(), 'exit_code': 2, 'reason': 'unexpected_error',
              'probes': 0, 'read_only': True}

    def left():
        # A backwards wall-clock adjustment cannot extend the authorized duration.
        return min(end - time.monotonic(), (until - dt.datetime.now(dt.timezone.utc)).total_seconds())

    try:
        with (output / 'lifecycle.jsonl').open('x', encoding='utf-8', newline='\n') as journal:
            def log(value):
                journal.write(canonical({'at': now(), **value}) + '\n')
                journal.flush()
                os.fsync(journal.fileno())

            log({'event': 'observation_started', 'pid': os.getpid(), 'parent_pid': os.getppid(),
                 'deadline': until.isoformat(), 'databases': sources,
                 'interval_seconds': interval, 'probe_timeout_seconds': timeout, 'read_only': True})
            while left() > 0:
                result['probes'] += 1
                started = time.monotonic()
                probe = run_probe(sources, output, result['probes'], min(timeout, max(0.001, left())))
                log({'event': 'probe_finished', **probe})
                if not probe['ok']:
                    result['reason'] = 'probe_timeout' if probe['timed_out'] else 'probe_failed'
                    break
                pause = min(max(0, interval - (time.monotonic() - started)), max(0, left()))
                while pause > 0:
                    time.sleep(min(pause, 0.25))
                    pause = min(max(0, interval - (time.monotonic() - started)), max(0, left()))
            else:
                result.update(exit_code=0 if result['probes'] else 2,
                              reason='deadline' if result['probes'] else 'deadline_before_first_probe')
            log(result)
    except KeyboardInterrupt:
        result.update(exit_code=130, reason='interrupted')
    except Exception as exc:
        result.update(exit_code=2, reason='observation_error', error_type=type(exc).__name__, error=str(exc))
    finally:
        result['finished_at'] = now()
        durable_json(output / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', required=True)
    window = parser.add_mutually_exclusive_group()
    window.add_argument('--seconds', type=float)
    window.add_argument('--until', help='explicit UTC timestamp ending Z; at most eight hours ahead')
    parser.add_argument('--output', help='new local directory for retained diagnostics')
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--_probe', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._probe:
            require(1 <= len(args.db) <= 8, 'probe requires 1..8 databases')
            print(canonical({'ok': True, 'health': [Mailbox(path).health() for path in args.db]}))
            return 0
        require(args.output is not None, '--output must name a new directory')
        if args.seconds is not None:
            require(math.isfinite(args.seconds) and 1 <= args.seconds <= 28800, 'seconds must be 1..28800')
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=args.seconds)
        else:
            require(args.until is not None and args.until.endswith('Z'), 'supply --seconds or UTC --until ending Z')
            until = dt.datetime.fromisoformat(args.until.replace('Z', '+00:00'))
        result = observe(args.db, until, args.output, args.interval, args.timeout)
        print(canonical(result))
        return result['exit_code']
    except (BridgeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(canonical({'ok': False, 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
