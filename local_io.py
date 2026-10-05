"""Local state guards. No mailbox writes or process startup."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path

from bridge import BridgeError, require


def distinct_state_paths(databases, outputs):
    """Protect SQLite sidecars as well as database files, including existing aliases."""
    protected = [Path(str(db) + suffix).resolve() for db in databases
                 for suffix in ('', '-wal', '-shm', '-journal')]
    state = [Path(path).resolve() for path in outputs]
    for index, path in enumerate(state):
        for other in protected + state[:index]:
            require(path != other and not (path.exists() and other.exists()
                    and path.samefile(other)), 'state paths must be distinct from databases, sidecars, and each other')


@contextmanager
def exclusive_state(paths):
    """OS locks release on crash. Persistent lock files must never be deleted to unlock."""
    with ExitStack() as stack:
        for path in sorted(set(map(Path, paths))):
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open('a+b'))
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BridgeError('watch state is already in use or cannot be locked') from exc
        yield
