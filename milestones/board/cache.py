"""What the app shows before it has asked GitHub anything: the last answer it got.

A `Section` is one read that takes seconds (every tracked repo's milestones, every pull
request of yours), kept on disk between runs. A page gets the kept copy at once, marked with
when it was read, and the fresh one as an event when it lands. The copy is for looking at:
nothing is decided from it, and the pages keep their writes switched off until `fresh`."""

import datetime
import json
import sys
import threading
from pathlib import Path

# A copy older than this is read again when a page next asks for it.
MAX_AGE = datetime.timedelta(minutes=5)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Section:
    def __init__(self, name: str, path: Path | None, fetch, publish=lambda name, payload: None):
        """`fetch(params)` yields the data, once or more than once: each yield but the last
        is a part-way answer worth showing (the quick half of a read whose other half is
        slow). `publish` is told each time what a page would now be given."""
        self.name, self.path, self.fetch, self.publish = name, path, fetch, publish
        self.lock = threading.Lock()
        self.data = None
        self.params: dict = {}
        self.fetched_at: str | None = None
        self.error: str | None = None
        self.refreshing = False
        # Whether `data` was read from GitHub by this run, start to finish.
        self.fresh = False
        # A read asked for while one was under way: that one may have looked before whatever
        # prompted this, so it is followed by another rather than taken as the answer.
        self.again = False
        self._read()

    def _read(self) -> None:
        if self.path is None:
            return
        try:
            kept = json.loads(self.path.read_text())
            self.data, self.fetched_at = kept["data"], kept["fetched_at"]
            self.params = kept.get("params") or {}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"fetched_at": self.fetched_at, "params": self.params, "data": self.data}))
        except OSError as error:
            print(f"warning: could not keep {self.path}: {error}", file=sys.stderr)

    def snapshot(self) -> dict:
        with self.lock:
            return {"data": self.data, "fetched_at": self.fetched_at, "params": self.params,
                    "fresh": self.fresh, "refreshing": self.refreshing, "error": self.error}

    def stale(self) -> bool:
        if not self.fresh or self.fetched_at is None:
            return True
        return now() - datetime.datetime.fromisoformat(self.fetched_at) > MAX_AGE

    def get(self, params: dict | None = None) -> dict:
        """What there is now, and a read started if it is none, old, or for other `params`."""
        with self.lock:
            changed = params is not None and params != self.params
            if changed:
                # The kept copy answers a different question: better nothing than that.
                self.params, self.data, self.fetched_at, self.fresh = params, None, None, False
        if changed or self.stale():
            self.refresh()
        return self.snapshot()

    def refresh(self) -> None:
        with self.lock:
            if self.refreshing:
                self.again = True
                return
            self.refreshing, self.error = True, None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        params = dict(self.params)
        try:
            data = None
            for data in self.fetch(params):
                with self.lock:
                    if params != self.params:
                        break  # asked something else meanwhile; the finally starts that read
                    self.data = data
                self.publish(self.name, self.snapshot())
            with self.lock:
                if params == self.params and data is not None and not self.again:
                    self.fresh, self.fetched_at = True, now().isoformat(timespec="seconds")
                    self._save()
        except SystemExit as exit:
            self.error = str(exit)
        except Exception as error:  # a bug in a read must not leave `refreshing` set for ever
            self.error = f"{type(error).__name__}: {error}"
        finally:
            with self.lock:
                again, self.again = self.again or params != self.params, False
                # Still reading, as far as a page is concerned, if another read follows.
                self.refreshing = again
            self.publish(self.name, self.snapshot())
            if again:
                threading.Thread(target=self._run, daemon=True).start()
