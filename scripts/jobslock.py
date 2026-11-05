"""One cross-process lock for every writer of jobs.json and raw.json.

    with jobs_lock(root):
        ...read, change, write...

Blocking, exclusive (fcntl.flock on <root>/.jobs.lock). Not reentrant: never nest.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def jobs_lock(root):
    path = Path(root) / ".jobs.lock"
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class AlreadyRunning(Exception):
    pass


@contextmanager
def worker_lock(root):
    """Held by the worker for its whole life, so a second worker cannot start on the same project."""
    path = Path(root) / ".worker.lock"
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("another worker is already running (lock: %s)" % path)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
