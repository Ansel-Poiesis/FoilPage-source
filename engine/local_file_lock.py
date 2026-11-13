# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Short cross-process locks for bounded local metadata journals."""
from contextlib import contextmanager
from pathlib import Path
import os
import time


@contextmanager
def metadata_lock(path: Path, timeout=2.0):
    target = Path(str(path) + '.lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a+b') as stream:
        if target.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError('local metadata journal is busy')
                time.sleep(0.025)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
