"""The app's terminal: one timestamped line for each thing that happened — a read started or
landed, a page opened, a write made or refused — so the window `milestones` runs in says what
the app is doing, and what went wrong when a page doesn't.

Every line goes to stderr through `say`, which holds a lock: the app's reads and writes run on
threads of their own, and two lines printed at once must not interleave."""

import os
import sys
import threading
import time

_lock = threading.Lock()
COLOR = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
CODES = {"dim": "2", "warn": "33", "error": "31", "done": "32"}


def paint(text: str, kind: str) -> str:
    return f"\x1b[{CODES[kind]}m{text}\x1b[0m" if COLOR else text


def say(message: str, kind: str | None = None) -> None:
    """Print `message` under the time. A message of several lines keeps them together, each
    indented to the first one's text. `kind` is warn, error or done, to colour it."""
    stamp = time.strftime("%H:%M:%S")
    first, *rest = message.splitlines() or [""]
    lines = [f"{paint(stamp, 'dim')}  {paint(first, kind) if kind else first}"]
    lines += [" " * (len(stamp) + 2) + line for line in rest]
    with _lock:
        print("\n".join(lines), file=sys.stderr, flush=True)


def seconds(start: float) -> str:
    """How long since `start` (a `time.monotonic()`), for the end of a line."""
    took = time.monotonic() - start
    return f"{took:.1f} s" if took < 60 else f"{int(took // 60)} min {int(took % 60)} s"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"
