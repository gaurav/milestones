"""The board's backend: holds one repo's cards in memory, writes each change to GitHub as it
is made, and tells every open page what happened. `docs/BoardAPI.md` is the contract; a
front end needs nothing from this file but that."""

import json
import mimetypes
import os
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
from . import data, reconcile

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


KINDS = ("milestone", "priority", "effort", "labels", "title", "state", "comment")
CLOSE_REASONS = ("completed", "not_planned")
# A label GitHub has no colour for yet (one this session is about to create) is shown grey.
NO_COLOUR = "ededed"


def card_value(card: dict, kind: str):
    """What a card holds for an action kind — the shape an action's `value` and `previous`
    take. A comment is not state: there is nothing to read back or put back."""
    if kind in reconcile.KINDS:
        return (card.get("fields") or {}).get(reconcile.KINDS[kind]["field"])
    if kind == "labels":
        return [l["name"] for l in card["labels"]]
    if kind == "state":
        return card.get("closed")
    if kind == "comment":
        return None
    return card[kind]


def set_card_value(card: dict, kind: str, value, colours: dict[str, str]) -> None:
    if kind in reconcile.KINDS:
        fields = card.setdefault("fields", {})
        if value is None:
            fields.pop(reconcile.KINDS[kind]["field"], None)
        else:
            fields[reconcile.KINDS[kind]["field"]] = value
    elif kind == "labels":
        card["labels"] = [{"name": n, "color": colours.get(n, NO_COLOUR)} for n in value]
    elif kind == "state":
        card["closed"] = value
    elif kind != "comment":
        card[kind] = value


def state_path(repo: str, environ=None) -> Path:
    """Where a repo's undecided suggestions are kept between runs. Decisions live on GitHub;
    this is only what hasn't been decided yet, and what was turned down."""
    environ = os.environ if environ is None else environ
    home = Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return home / "milestones" / f"board-{repo.replace('/', '-')}.json"


def suggestion_key(s: dict) -> str:
    return json.dumps([s["issue"], s["kind"], s["value"], s["source"]], sort_keys=True)


class Board:
    """One repo's cards and milestones, and the log of what this session did to them.

    Every change is applied to the cards here first and written to GitHub after, by one
    worker thread in the order the changes were made — so the page never waits on the
    network, and two moves of one issue can't arrive out of order."""

    def __init__(self, repo: str, buckets: list[str], sort_key, fetch=data.fetch_board,
                 write=None, state_file: Path | None = None):
        self.requested, self.buckets, self.sort_key = repo, buckets, sort_key
        self.state_file = state_file
        self.labels: list[dict] = []
        self.fields: dict = {}
        self.suggestions: dict[int, dict] = {}
        self.rejected: set[str] = set()
        self._restored: list[dict] = []
        self._read_state()
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
            self.labels = fetched.get("labels", [])
            self.fields = fetched.get("fields", {})
            # What this session changed and GitHub hasn't confirmed is still the truth
            # here: a refetch must not put a card back where a failed write left it.
            for action in self.actions.values():
                if action["status"] in (*UNSETTLED, "would") and action["issue"] in self.cards:
                    set_card_value(self.cards[action["issue"]], action["kind"], action["value"],
                                   self._colours())
            # Suggestions kept from the last run, for the issues that are still open.
            restored, self._restored = self._restored, []
            self._add_suggestions([s for s in restored if s["issue"] in self.cards])
            self._rules(self.cards)

    def _colours(self) -> dict[str, str]:
        return {l["name"]: l["color"] for l in self.labels}

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
                "labels": self.labels,
                "fields": {name: list(field["options"]) for name, field in self.fields.items()},
                "suggestions": [s for s in self.suggestions.values()
                                if s["status"] != "rejected"],
            }

    def publish(self, event: str, payload: dict) -> None:
        for listener in list(self.listeners):
            listener.put((event, payload))

    # --- actions -------------------------------------------------------------------------

    def _new_action(self, issue: int, kind: str, value, carried_by: int | None = None) -> dict:
        card = self.cards[issue]
        action = {"id": len(self.actions) + 1, "issue": issue, "kind": kind, "value": value,
                  "previous": card_value(card, kind), "carriedBy": carried_by, "error": None,
                  "status": "would" if self.read_only else "pending"}
        self.actions[action["id"]] = action
        set_card_value(card, kind, value, self._colours())
        return action

    def normalise(self, request: dict) -> tuple[str, object]:
        """Check one requested change and return the (kind, value) to act on, which is not
        always the pair asked for: a priority goes to a label where the owner has no
        Priority field, and a label change given as {add, remove} becomes the set that
        results. Raises Refused for anything that can't be done."""
        kind, value = request.get("kind"), request.get("value")
        if kind not in KINDS:
            raise Refused(400, f"unknown action kind: {kind!r}")
        card = self.cards.get(request.get("issue"))
        if card is None:
            raise Refused(404, f"no open issue or pull request #{request.get('issue')}")
        if kind == "milestone":
            if value is not None:
                milestone = self.milestones.get(value)
                if milestone is None:
                    raise Refused(404, f"no milestone number {value!r}")
                if milestone["state"] != "open":
                    raise Refused(409, f"milestone '{milestone['title']}' is closed; "
                                       "reopen it first")
        elif kind in reconcile.KINDS:
            spec = reconcile.KINDS[kind]
            field = self.fields.get(spec["field"])
            if field is None:
                # No such field for this owner (a user account, or an organisation that
                # hasn't made one): the label is the fallback, and the only record.
                if value is not None and not isinstance(value, str):
                    raise Refused(400, f"{kind} must be a name or null")
                keep = [n for n in card_value(card, "labels")
                        if n not in reconcile.labels_about(kind, card)]
                label = f"{spec['field']}: {value.strip().capitalize()}" if value else None
                return "labels", keep + ([label] if label else [])
            if card["kind"] != "issue":
                raise Refused(400, f"#{card['number']} is a pull request, which has no "
                                   f"{spec['field']} field")
            if value is not None:
                names = {name.lower(): name for name in field["options"]}
                if not isinstance(value, str) or value.lower() not in names:
                    raise Refused(400, f"{spec['field']} is one of {', '.join(field['options'])}"
                                       f", or null; not {value!r}")
                value = names[value.lower()]
        elif kind == "labels":
            current = card_value(card, "labels")
            if isinstance(value, dict):
                remove = set(value.get("remove") or [])
                value = [n for n in current if n not in remove] + [
                    n for n in value.get("add") or [] if n not in current]
            if not isinstance(value, list) or not all(isinstance(n, str) and n for n in value):
                raise Refused(400, "labels is a list of names, or {add: [...], remove: [...]}")
            value = list(dict.fromkeys(value))
        elif kind in ("title", "comment"):
            if not isinstance(value, str) or not value.strip():
                raise Refused(400, f"a {kind} needs some text")
            value = value.strip()
        elif kind == "state":
            if value is not None and value not in CLOSE_REASONS:
                raise Refused(400, f"state is one of {', '.join(CLOSE_REASONS)} to close, "
                                   "or null to reopen")
        return kind, value

    def submit(self, requests: list[dict]) -> list[dict]:
        """Apply a batch of changes to the cards and queue their writes. All or none: one
        bad request refuses the batch before anything has moved."""
        with self.lock:
            if not all(isinstance(r, dict) for r in requests):
                raise Refused(400, "each action is {issue, kind, value}")
            for request in requests:
                self.normalise(request)
            made = []
            moving = {r["issue"] for r in requests if r["kind"] == "milestone"}
            for request in requests:
                # Normalised again, against the card as the earlier requests in this batch
                # left it: a priority that becomes a label, then a label change, must not
                # each start from the labels the card had before either.
                issue, (kind, value) = request["issue"], self.normalise(request)
                card = self.cards[issue]
                if kind != "comment" and card_value(card, kind) == value:
                    continue  # already so: nothing to write, as in the triage walk
                made.append(self._new_action(issue, kind, value))
                if kind == "milestone" and card["kind"] == "pr" and value is not None:
                    # A pull request is triaged for the issues it closes too.
                    for number in data.carried_by(card, self._issues(), self.parked()):
                        if number not in moving:
                            made.append(self._new_action(number, "milestone", value,
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
                set_card_value(self.cards[action["issue"]], action["kind"], action["previous"],
                               self._colours())
            return action

    def _action(self, action_id: int) -> dict:
        if action_id not in self.actions:
            raise Refused(404, f"no action {action_id}")
        return self.actions[action_id]

    def _write(self, action: dict) -> None:
        """Make one action's write. Raises SystemExit (gh's failure) if it didn't take."""
        kind, value, number = action["kind"], action["value"], action["issue"]
        issue = f"repos/{self.repo}/issues/{number}"
        owner = self.repo.split("/")[0]
        if kind == "milestone":
            answer = gh.api(issue, method="PATCH", body={"milestone": value})
            # GitHub's answer rather than its status: a PATCH can succeed and leave the
            # milestone as it was, for a token that may edit the issue but not triage it.
            got = (answer.get("milestone") or {}).get("number")
            if got != value:
                raise SystemExit(f"GitHub kept #{number} on milestone {got!r}")
        elif kind in reconcile.KINDS:
            name = reconcile.KINDS[kind]["field"]
            field = self.fields[name]
            change = (f'singleSelectOptionId: "{field["options"][value]}"' if value is not None
                      else "delete: true")
            gh.mutate(f"set #{number}'s {name} field to {value}",
                      f'mutation {{ setIssueFieldValue(input: {{issueId: '
                      f'"{self.cards[number]["id"]}", issueFields: [{{fieldId: "{field["id"]}", '
                      f"{change}}}]}}) {{ clientMutationId }} }}", owner)
        elif kind == "labels":
            # PUT replaces the set, and makes any label the repo hasn't got.
            answer = gh.api(f"{issue}/labels", method="PUT", body={"labels": value})
            got = sorted(l["name"].lower() for l in answer)
            if got != sorted(n.lower() for n in value):
                raise SystemExit(f"GitHub left #{number} with the labels {', '.join(got)}")
            with self.lock:
                known = self._colours()
                self.labels += [{"name": l["name"], "color": l["color"]} for l in answer
                                if l["name"] not in known]
                set_card_value(self.cards[number], "labels", [l["name"] for l in answer],
                               self._colours())
        elif kind == "title":
            gh.api(issue, method="PATCH", body={"title": value})
        elif kind == "state":
            gh.api(issue, method="PATCH", body={"state": "open"} if value is None
                   else {"state": "closed", "state_reason": value})
        elif kind == "comment":
            gh.api(f"{issue}/comments", method="POST", body={"body": value})

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

    # --- suggestions ---------------------------------------------------------------------

    def _read_state(self) -> None:
        if self.state_file is None:
            return
        try:
            kept = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.rejected = set(kept.get("rejected") or [])
        self._restored = [s for s in kept.get("open") or [] if isinstance(s, dict)]

    def _save_state(self) -> None:
        """Keep what is undecided and what was turned down. A rule's suggestion is not kept
        while open — the rule will make it again — only remembered once rejected."""
        if self.state_file is None:
            return
        kept = {"rejected": sorted(self.rejected),
                "open": [{k: s[k] for k in ("issue", "kind", "value", "reason", "source")}
                         for s in self.suggestions.values()
                         if s["status"] == "open" and not s["source"].startswith("rule:")]}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(kept, indent=2))
        except OSError as error:
            print(f"warning: could not save suggestions to {self.state_file}: {error}",
                  file=sys.stderr)

    def _add_suggestions(self, proposed: list[dict]) -> list[dict]:
        """Record suggestions, skipping one already open and one already turned down."""
        added = []
        standing = {suggestion_key(s) for s in self.suggestions.values() if s["status"] == "open"}
        for p in proposed:
            suggestion = {"id": len(self.suggestions) + 1, "issue": p["issue"],
                          "kind": p["kind"], "value": p["value"],
                          "reason": str(p.get("reason") or ""),
                          "source": str(p.get("source") or "someone"),
                          "status": "open", "action": None}
            key = suggestion_key(suggestion)
            if key in standing or key in self.rejected:
                continue
            standing.add(key)
            self.suggestions[suggestion["id"]] = suggestion
            added.append(suggestion)
        return added

    def _rules(self, cards: dict[int, dict]) -> list[dict]:
        """Bring the board's own suggestions for these cards up to date: the open ones are
        dropped and made again from the cards as they now stand."""
        changed = []
        for s in self.suggestions.values():
            if s["status"] == "open" and s["source"].startswith("rule:") and s["issue"] in cards:
                s["status"] = "withdrawn"
                changed.append(s)
        self.suggestions = {i: s for i, s in self.suggestions.items()
                            if s["status"] != "withdrawn"}
        proposed = [p for card in cards.values() if not card.get("closed")
                    for p in reconcile.rule_suggestions(card, self.fields)]
        return changed + self._add_suggestions(proposed)

    def suggest(self, proposed: list[dict]) -> list[dict]:
        """Take suggestions from outside — a coding agent, a script. Each is checked as an
        action would be, so one that could never be accepted is refused now, with why."""
        with self.lock:
            if not all(isinstance(p, dict) for p in proposed):
                raise Refused(400, "each suggestion is {issue, kind, value, reason, source}")
            for p in proposed:
                self.normalise(p)
                if str(p.get("source") or "").startswith("rule:"):
                    raise Refused(400, "a source starting 'rule:' is the board's own")
            added = self._add_suggestions(proposed)
            self._save_state()
            return added

    def resolve(self, ids: list[int], decision: str) -> tuple[list[dict], list[dict]]:
        """Accept or reject suggestions: (the suggestions that changed, the actions made).
        Accepting makes the ordinary action, so it is written, retried and undone like one."""
        if decision not in ("accept", "reject"):
            raise Refused(400, "decision is accept or reject")
        with self.lock:
            chosen = []
            for i in ids:
                if i not in self.suggestions:
                    raise Refused(404, f"no suggestion {i}")
                if self.suggestions[i]["status"] == "open":
                    chosen.append(self.suggestions[i])
            actions = []
            if decision == "accept":
                actions = self.submit([{k: s[k] for k in ("issue", "kind", "value")}
                                       for s in chosen])
                made = {(a["issue"], a["kind"]): a["id"] for a in actions if not a["carriedBy"]}
                for s in chosen:
                    kind = self.normalise(s)[0] if s["issue"] in self.cards else s["kind"]
                    s.update(status="accepted", action=made.get((s["issue"], kind)))
            else:
                for s in chosen:
                    s["status"] = "rejected"
                    self.rejected.add(suggestion_key(s))
            # Accepting one can make another moot, or due: a field just filled from a label
            # is what makes that label redundant.
            touched = {s["issue"]: self.cards[s["issue"]] for s in chosen
                       if s["issue"] in self.cards}
            changed = chosen + (self._rules(touched) if decision == "accept" else [])
            self._save_state()
            return changed, actions

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
                if method == "POST" and rest == ["suggestions"]:
                    proposed = self._body().get("suggestions")
                    if not isinstance(proposed, list):
                        raise Refused(400, "send {suggestions: [{issue, kind, value, reason, "
                                           "source}, ...]}")
                    added = board.suggest(proposed)
                    for suggestion in added:
                        board.publish("suggestion", suggestion)
                    return self._json(200, {"suggestions": added})
                if method == "POST" and rest == ["suggestions", "resolve"]:
                    body = self._body()
                    if not isinstance(body.get("ids"), list):
                        raise Refused(400, "send {ids: [...], decision: 'accept' | 'reject'}")
                    changed, actions = board.resolve(body["ids"], body.get("decision"))
                    for action in actions:
                        board.publish("action", action)
                    for suggestion in changed:
                        board.publish("suggestion", suggestion)
                    return self._json(200, {"suggestions": changed, "actions": actions})
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
    board = Board(repo, buckets, sort_key, state_file=state_path(repo))

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
            lines += [f"  #{a['issue']} " + (
                          f"→ {titles.get(a['value'], 'no milestone')}"
                          if a.get("kind", "milestone") == "milestone"
                          else f"{a['kind']}: {json.dumps(a['value'], ensure_ascii=False)[:60]}")
                      + (f"  — {a['error'].splitlines()[0]}" if a["error"] else "")
                      for a in left]
    return lines
