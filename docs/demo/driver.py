"""Drives the demo under asciinema.

Not expect: expect only flushes what it has read when a pattern matches, so
under asciinema every typed line arrived in one burst at end-of-line and the
recording had nine distinct frames. This owns its own timing instead — the
staged lines are written character by character, and the real `grask` runs on
its own pty so its prompt, the keypress echo, and the grading are the binary's,
not a reconstruction.

Only the two scene-setting comment lines are staged. A live `claude` run would
be non-deterministic and would record whatever is in the author's environment.
"""

from __future__ import annotations

import fcntl
import os
import pty
import random
import select
import struct
import sys
import termios
import time

COLS, ROWS = 112, 17
PROMPT = "$ "


def out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def type_out(text: str) -> None:
    for ch in text:
        out(ch)
        time.sleep(random.uniform(0.035, 0.085))


def staged(command: str, pause: float = 0.9) -> None:
    """A shell line that is typed but never run. Comments only."""
    out(PROMPT)
    type_out(command)
    out("\r\n")
    time.sleep(pause)


def run_grask(answer: str = "b", read_delay: float = 3.6) -> None:
    """Run the real binary on a pty, answering after a beat to read the options."""
    out(PROMPT)
    type_out("grask")
    out("\r\n")

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("grask", ["grask"])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    seen = b""
    answered = False
    deadline = time.time() + 60
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            os.write(1, chunk)
            seen += chunk
        if not answered and b"pick" in seen:
            time.sleep(read_delay)
            os.write(fd, answer.encode())  # echoed back by the child's tty
            time.sleep(0.7)
            os.write(fd, b"\r")
            answered = True
    os.waitpid(pid, 0)


staged("# yesterday: you shipped a webhook retry loop with Claude Code.")
staged("# the session ended. grask captured it, in the background, silently.", 2.2)

out("\x1b[3J\x1b[H\x1b[2J")  # next morning, clean screen
time.sleep(1.0)

staged("# next morning", 1.4)
run_grask()

time.sleep(5.0)
out(PROMPT)
