"""Small stdlib flock subset for the Bridge's exclusive process locks."""
from __future__ import annotations

import errno
import os
import time

if os.name == "nt":
    import msvcrt

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(file, operation):
        # Windows locks a byte range, including ranges beyond EOF. All callers
        # open persistent lock files without truncation and lock the first byte.
        file.seek(0)
        if operation == LOCK_UN:
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        if operation not in (LOCK_EX, LOCK_EX | LOCK_NB):
            raise ValueError("Only exclusive locks are supported")
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(errno.EAGAIN, "Lock is held") from error
                time.sleep(0.05)
else:
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
