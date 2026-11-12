"""The board's backend: holds one repo's cards in memory, writes each change to GitHub as it
is made, and tells every open page what happened. `docs/BoardAPI.md` is the contract; a
front end needs nothing from this file but that."""

import json
import mimetypes
import queue
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import gh
from ..cli import TRIAGE_BUCKET
from . import data

STATIC = Path(__file__).parent / "static"
TOKEN_HEADER = "X-Milestones-Token"
# Statuses an action can be left in when the page or the server goes away with work undone.
UNSETTLED = ("pending", "failed")

NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>milestones board</title>
<body style="font: 16px system-ui; max-width: 40em; margin: 4em auto">
<h1>The board's front end hasn't been built</h1>
<p>From the top of the <code>milestones</code> checkout, run:</p>
<pre>npm install
npm run build</pre>
<p>then reload this page. The API is up either way: see <code>docs/BoardAPI.md</code>.</p>"""


class Refused(Exception):
    """A request the board won't carry out, with the HTTP status to say so."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Board:
    """One repo's cards and milestones, and the log of what this session did to them.

    Every change is applied to the cards here first and written to GitHub after, by one
    worker thread in the order the changes were made — so the page never waits on the
    network, and two moves of one issue can't arrive out of order."""

    def __init__(self, repo: str, buckets: list[str], sort_key, fetch=data.fetch_board,
                 write=None):
        self.requested, self.buckets, self.sort_key = repo, buckets, sort_key
        self.fetch, self.write = fetch, write or self._write
        self.lock = threading.RLock()
        self.repo, self.cards, self.milestones = repo, {}, {}
        self.actions: dict[int, dict] = {}
        self.todo: queue.Queue = queue.Queue()
        self.listeners: list[queue.Queue] = []
        self.read_only = gh.read_only()

    # --- state ---------------------------------------------------------------------------

    def load(self, progress=lambda *a: None) -> None:
        fetched = self.fetch(self.requested, progress)
        with self.lock:
            self.repo = fetched["repo"]
            self.milestones = {m["number"]: m for m in fetched["milestones"]}
            self.cards = {c["number"]: c for c in fetched["cards"]}
            # What this session changed and GitHub hasn't confirmed is still the truth
            # here: a refetch must not put a card back where a failed write left it.
            for action in self.actions.values():
                if action["status"] in (*UNSETTLED, "would") and action["issue"] in self.cards:
                    self.cards[action["issue"]][action["kind"]] = action["value"]

    def parked(self) -> int | None:
        """The open Needs triage milestone's number, if the config uses that bucket."""
        if TRIAGE_BUCKET not in self.buckets:
            return None
        return next((m["number"] for m in self.milestones.values()
                     if m["title"] == TRIAGE_BUCKET and m["state"] == "open"), None)

    def snapshot(self) -> dict:
        with self.lock:
            milestones = list(self.milestones.values())
            return {
                "repo": self.repo,
                "permissions": "read" if self.read_only else "write",
                "buckets": self.buckets,
                "milestones": milestones,
                "columns": data.column_order(milestones, self.buckets, self.sort_key),
                "cards": list(self.cards.values()),
                "actions": list(self.actions.values()),
            }

    def publish(self, event: str, payload: dict) -> None:
        for listener in list(self.listeners):
            listener.put((event, payload))

    # --- actions -------------------------------------------------------------------------

    def _new_action(self, issue: int, kind: str, value, carried_by: int | None = None) -> dict:
        card = self.cards[issue]
        action = {"id": len(self.actions) + 1, "issue": issue, "kind": kind, "value": value,
                  "previous": card[kind], "carriedBy": carried_by, "error": None,
                  "status": "would" if self.read_only else "pending"}
        self.actions[action["id"]] = action
        card[kind] = value
        return action

    def submit(self, requests: list[dict]) -> list[dict]:
        """Apply a batch of changes to the cards and queue their writes. All or none: one
        bad request refuses the batch before anything has moved."""
        with self.lock:
            for request in requests:
                if request.get("kind") != "milestone":
                    raise Refused(400, f"unknown action kind: {request.get('kind')!r}")
                if request.get("issue") not in self.cards:
                    raise Refused(404, f"no open issue or pull request #{request.get('issue')}")
                value = request.get("value")
                if value is not None:
                    milestone = self.milestones.get(value)
                    if milestone is None:
                        raise Refused(404, f"no milestone number {value!r}")
                    if milestone["state"] != "open":
                        raise Refused(409, f"milestone '{milestone['title']}' is closed; "
                                           "reopen it first")
            made = []
            moving = {r["issue"] for r in requests}
            for request in requests:
                card = self.cards[request["issue"]]
                if card["milestone"] == request["value"]:
                    continue  # already there: nothing to write, as in the triage walk
                made.append(self._new_action(card["number"], "milestone", request["value"]))
                if card["kind"] == "pr" and request["value"] is not None:
                    # A pull request is triaged for the issues it closes too.
                    for number in data.carried_by(card, self._issues(), self.parked()):
                        if number not in moving:
                            made.append(self._new_action(number, "milestone", request["value"],
                                                         carried_by=card["number"]))
            for action in made:
                if action["status"] == "pending":
                    self.todo.put(action["id"])
            return made

    def _issues(self) -> dict:
        return {("issue", n): c for n, c in self.cards.items() if c["kind"] == "issue"}

    def retry(self, action_id: int) -> dict:
        with self.lock:
            action = self._action(action_id)
            if action["status"] != "failed":
                raise Refused(409, f"action {action_id} is {action['status']}, not failed")
            action.update(status="pending", error=None)
            self.todo.put(action_id)
            return action

    def revert(self, action_id: int) -> dict:
        """Give up on a failed write: the card goes back to what GitHub still has."""
        with self.lock:
            action = self._action(action_id)
            if action["status"] != "failed":
                raise Refused(409, f"action {action_id} is {action['status']}, not failed")
            action["status"] = "reverted"
            later = [a for a in self.actions.values() if a["issue"] == action["issue"]
                     and a["kind"] == action["kind"] and a["id"] > action_id
                     and a["status"] != "reverted"]
            if not later:
                self.cards[action["issue"]][action["kind"]] = action["previous"]
            return action

    def _action(self, action_id: int) -> dict:
        if action_id not in self.actions:
            raise Refused(404, f"no action {action_id}")
        return self.actions[action_id]

    def _write(self, action: dict) -> None:
        """Make one action's write. Raises SystemExit (gh's failure) if it didn't take."""
        answer = gh.api(f"repos/{self.repo}/issues/{action['issue']}", method="PATCH",
                        body={"milestone": action["value"]})
        # GitHub's answer rather than its status: a PATCH can succeed and leave the
        # milestone as it was, for a token that may edit the issue but not triage it.
        got = (answer.get("milestone") or {}).get("number")
        if got != action["value"]:
            raise SystemExit(f"GitHub kept #{action['issue']} on milestone {got!r}")

    def work(self) -> None:
        """The writer thread: one write at a time, in the order the changes were made."""
        while True:
            action_id = self.todo.get()
            with self.lock:
                action = self.actions[action_id]
                if action["status"] != "pending":
                    continue
            try:
                self.write(action)
                outcome = {"status": "done"}
            except SystemExit as exit:
                outcome = {"status": "failed", "error": str(exit)}
            except Exception as error:  # a bug here must not kill the only writer
                outcome = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
            with self.lock:
                action.update(outcome)
            self.publish("action", action)
            self.todo.task_done()

    def unsettled(self) -> list[dict]:
        with self.lock:
            return [a for a in self.actions.values() if a["status"] in UNSETTLED]

    # --- milestones ----------------------------------------------------------------------

    def _refuse_read_only(self, what: str) -> None:
        if self.read_only:
            raise Refused(403, f"read-only: would {what}")

    def _milestone_write(self, call) -> dict:
        try:
            milestone = data.norm_milestone(call())
        except SystemExit as exit:
            raise Refused(502, str(exit))
        with self.lock:
            self.milestones[milestone["number"]] = milestone
        self.publish("milestone", milestone)
        return milestone

    def ensure_milestone(self, title: str, reopen: bool = False) -> dict:
        """The open milestone of that title: made if there is none, reopened if it is closed
        and `reopen` says so. Never a second one of the same name."""
        title = (title or "").strip()
        if not title:
            raise Refused(400, "a milestone needs a title")
        with self.lock:
            existing = next((m for m in self.milestones.values()
                             if m["title"].lower() == title.lower()), None)
        if existing and existing["state"] == "open":
            return existing
        if existing:
            if not reopen:
                raise Refused(409, f"milestone '{existing['title']}' is closed; "
                                   "send reopen: true to reopen it")
            self._refuse_read_only(f"reopen milestone '{existing['title']}'")
            return self._milestone_write(lambda: gh.api(
                f"repos/{self.repo}/milestones/{existing['number']}", method="PATCH",
                body={"state": "open"}))
        self._refuse_read_only(f"create milestone '{title}'")
        return self._milestone_write(lambda: gh.api(
            f"repos/{self.repo}/milestones", method="POST", body={"title": title}))

    def set_due(self, number: int, day: str | None) -> dict:
        if number not in self.milestones:
            raise Refused(404, f"no milestone number {number}")
        if day is not None:
            try:
                time.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError):
                raise Refused(400, f"due must be YYYY-MM-DD or null, not {day!r}")
        self._refuse_read_only(f"set milestone {number}'s due date to {day}")
        return self._milestone_write(lambda: gh.api(
            f"repos/{self.repo}/milestones/{number}", method="PATCH",
            body={"due_on": data.due_instant(day)}))


def allowed_host(host: str | None, port: int) -> bool:
    """A request must name this server as it is reached on loopback. Anything else is a
    page on another site trying a rebound DNS name against a server that can write to
    GitHub."""
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def make_handler(board: Board, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _guard(self, url) -> bool:
            if not allowed_host(self.headers.get("Host"), self.server.server_address[1]):
                self._json(403, {"error": "bad Host"})
                return False
            if url.path.startswith("/api/"):
                # EventSource can't set a header, so the event stream alone may carry the
                # token in its query string.
                given = self.headers.get(TOKEN_HEADER)
                if given is None and url.path == "/api/v1/events":
                    given = (parse_qs(url.query).get("token") or [None])[0]
                if given is None or not secrets.compare_digest(given, token):
                    self._json(401, {"error": "missing or wrong token"})
                    return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise Refused(400, "the request body is not JSON")
            if not isinstance(body, dict):
                raise Refused(400, "the request body must be a JSON object")
            return body

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            if not self._guard(url):
                return
            parts = [p for p in url.path.split("/") if p]
            try:
                if parts[:2] != ["api", "v1"]:
                    if method != "GET":
                        raise Refused(404, "not found")
                    return self._static(url.path)
                rest = parts[2:]
                if method == "GET" and rest == ["board"]:
                    return self._json(200, board.snapshot())
                if method == "GET" and rest == ["events"]:
                    return self._events()
                if method == "POST" and rest == ["actions"]:
                    requests = self._body().get("actions")
                    if not isinstance(requests, list):
                        raise Refused(400, "send {actions: [{issue, kind, value}, ...]}")
                    made = board.submit(requests)
                    for action in made:
                        board.publish("action", action)
                    return self._json(200, {"actions": made})
                if (method == "POST" and len(rest) == 3 and rest[0] == "actions"
                        and rest[1].isdigit() and rest[2] in ("retry", "revert")):
                    action = getattr(board, rest[2])(int(rest[1]))
                    board.publish("action", action)
                    return self._json(200, {"action": action})
                if method == "POST" and rest == ["milestones"]:
                    body = self._body()
                    return self._json(200, {"milestone": board.ensure_milestone(
                        body.get("title"), bool(body.get("reopen")))})
                if (method == "PATCH" and len(rest) == 2 and rest[0] == "milestones"
                        and rest[1].isdigit()):
                    body = self._body()
                    if "due" not in body:
                        raise Refused(400, "send {due: 'YYYY-MM-DD'} or {due: null}")
                    return self._json(200, {"milestone": board.set_due(int(rest[1]),
                                                                        body["due"])})
                if method == "POST" and rest == ["refresh"]:
                    try:
                        board.load()
                    except SystemExit as exit:
                        raise Refused(502, str(exit))
                    board.publish("reload", {})
                    return self._json(200, board.snapshot())
                raise Refused(404, "not found")
            except Refused as refused:
                self._json(refused.status, {"error": str(refused)})

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / name).resolve()
            if not (STATIC / "index.html").exists():
                return self._send(200, NOT_BUILT.encode(), "text/html; charset=utf-8")
            if STATIC.resolve() not in target.parents or not target.is_file():
                raise Refused(404, "not found")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind.endswith("javascript"):
                kind += "; charset=utf-8"
            self._send(200, target.read_bytes(), kind)

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            listener: queue.Queue = queue.Queue()
            board.listeners.append(listener)
            try:
                while True:
                    try:
                        event, payload = listener.get(timeout=15)
                        chunk = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                    except queue.Empty:
                        chunk = ": keep-alive\n\n"  # also how a closed tab gets noticed
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                board.listeners.remove(listener)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

    return Handler


def run(repo: str, buckets: list[str], sort_key, port: int = 0, open_browser: bool = True,
        shown: list[str] | None = None) -> None:
    board = Board(repo, buckets, sort_key)

    def progress(what: str, seen: int, total: int) -> None:
        label = "issues" if what == "issues" else "pull requests"
        print(f"\r  {label}: {seen}/{total}   ", end="", file=sys.stderr, flush=True)

    print(f"Reading {repo} …", file=sys.stderr)
    board.load(progress)
    print(file=sys.stderr)
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(board, token))
    server.daemon_threads = True
    threading.Thread(target=board.work, daemon=True).start()
    # The token rides in the fragment, which a browser never sends to a server or leaves
    # in a Referer; the page reads it and sends it back as a header.
    fragment = f"t={token}" + "".join(f"&from={quote_title(t)}" for t in shown or [])
    url = f"http://127.0.0.1:{server.server_address[1]}/#{fragment}"
    mode = "  (read-only: nothing will be written)" if board.read_only else ""
    print(f"{board.repo}: {len(board.cards)} open issues and pull requests{mode}\n{url}\n"
          "Ctrl-C to stop.", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # A write already handed to gh finishes or fails within its own timeout; give the
        # queue a moment, then say what never reached GitHub.
        deadline = time.time() + 10
        while time.time() < deadline and any(a["status"] == "pending" for a in board.unsettled()):
            time.sleep(0.2)
        print("\n".join(closing_lines(board.snapshot())), file=sys.stderr)


def quote_title(title: str) -> str:
    from urllib.parse import quote
    return quote(title, safe="")


def closing_lines(snapshot: dict) -> list[str]:
    """What the session wrote, and — the part that matters — what it didn't."""
    titles = {m["number"]: m["title"] for m in snapshot["milestones"]}
    actions = snapshot["actions"]
    lines = ["", f"{sum(a['status'] == 'done' for a in actions)} written to {snapshot['repo']}."]
    for status, label in (("failed", "NOT written (failed)"),
                          ("pending", "NOT written (still queued)"),
                          ("would", "not written (read-only)")):
        left = [a for a in actions if a["status"] == status]
        if left:
            lines.append(f"{len(left)} {label}:")
            lines += [f"  #{a['issue']} → {titles.get(a['value'], 'no milestone')}"
                      + (f"  — {a['error'].splitlines()[0]}" if a["error"] else "")
                      for a in left]
    return lines
