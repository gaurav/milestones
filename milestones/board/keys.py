"""One-key commands in the terminal the app runs in: `o` opens the page again, `q` stops the
app as Ctrl-C does, `?` lists them.

The terminal is put in cbreak mode (a key arrives without Enter, and isn't echoed) for as long
as the app runs. Signals stay on, so Ctrl-C and Ctrl-Z work as they did. Only a process in the
terminal's foreground touches it: a background one that read from it or changed its mode would
be stopped by the shell (SIGTTIN, SIGTTOU), so the reader waits until the job is in front,
and puts cbreak back then, since a shell resets the mode when a job is stopped."""

import os
import select
import signal
import sys
import termios
import threading
import tty
import webbrowser

from .log import say

HELP = "Keys: o opens the page, q stops the app (as Ctrl-C does), ? lists these."


def foreground(fd: int) -> bool:
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False


class Keys:
    """Reads keys from stdin when it is a terminal; does nothing otherwise (a pipe, a
    background job started with `&` and never brought forward, a service)."""

    def __init__(self, url: str):
        self.url = url
        self.fd = sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None
        self.saved = None
        self.stopped = threading.Event()
        # So that the reader can't put cbreak back between `restore` and the exit.
        self.lock = threading.Lock()

    def start(self) -> bool:
        """Start reading keys; False when stdin is not a terminal."""
        if self.fd is None:
            return False
        threading.Thread(target=self._read, daemon=True).start()
        return True

    def _cbreak(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            if termios.tcgetattr(self.fd)[3] & (termios.ICANON | termios.ECHO):
                if self.saved is None:
                    self.saved = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd, termios.TCSANOW)

    def restore(self) -> None:
        """Give the terminal back as it was. Before exit, and before a reload's `exec`."""
        with self.lock:
            self.stopped.set()
            if self.saved is not None and foreground(self.fd):
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def _read(self) -> None:
        while not self.stopped.is_set():
            if not foreground(self.fd):
                self.stopped.wait(0.5)
                continue
            try:
                self._cbreak()
                if not select.select([self.fd], [], [], 0.5)[0]:
                    continue
                if self.stopped.is_set():
                    return
                key = os.read(self.fd, 1).decode(errors="replace")
            except OSError:
                continue  # sent to the background between the check and the read
            if not key:
                return  # the terminal has gone
            self.handle(key)

    def handle(self, key: str) -> None:
        if key in "oO":
            say(f"Opening {self.url.split('#')[0]}")
            webbrowser.open(self.url)
        elif key in "qQ":
            # Through the same path as Ctrl-C, so the closing summary is printed and the
            # session file removed.
            os.kill(os.getpid(), signal.SIGINT)
        elif key in "?hH":
            say(HELP)
        elif key not in "\r\n ":
            say(f"No command on {key!r}. {HELP}")
