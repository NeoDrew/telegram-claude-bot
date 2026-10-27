#!/usr/bin/env python3
"""with_setsid.py: tiny trampoline that calls setsid() then execvps the
target program.

Why: macOS doesn't ship `setsid(1)` in the base system, but Python's
os.setsid() does the same job. We use it to detach spawn_claude.sh from
the listener's process group at spawn time. After detach, signals sent
to the listener's group (e.g. `launchctl kickstart -k` killing the
service) don't cascade to active spawns, so a listener restart no
longer interrupts in-flight claude turns or kills the streamer pipeline.

Usage (from listener.sh):
    python3 with_setsid.py <program> [args...]

Behaviour:
  - Calls os.setsid(), new session, new process group whose leader is
    this process.
  - os.execvp() replaces this Python process with the target program.
    The target inherits the new pgid and keeps the same pid Python had
    (so callers writing busy.lock with $! still see the right pid).
"""
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("with_setsid.py: missing program to exec\n")
        return 2
    # New session detaches us from the parent's process group. The
    # session leader (us) becomes the pgid leader of the new group.
    # Errors here are fatal, there's no useful fallback if setsid fails
    # (we'd just re-introduce the "killed with the listener" bug).
    os.setsid()
    # execvp searches PATH for the program if it's not an absolute path.
    # The spawned program inherits our pid AND the new pgid.
    os.execvp(sys.argv[1], sys.argv[1:])
    # execvp doesn't return on success; if we reach here something failed.
    return 127


if __name__ == "__main__":
    sys.exit(main())
