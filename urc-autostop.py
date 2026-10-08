#!/usr/bin/env python3
"""Stop an auto-started router after the last urc client disappears."""

import fcntl
import os
import signal
import sys
import time


def main():
    if len(sys.argv) != 3:
        return 2
    lock_path, process_group = sys.argv[1], int(sys.argv[2])
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return 0
            time.sleep(0.1)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
