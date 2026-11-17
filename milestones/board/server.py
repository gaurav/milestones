"""The web app's backend. A `Board` holds one repo's cards in memory, writes each change to
GitHub as it is made, and tells every open page what happened; the `App` holds a board for
each repo that has been opened, read from GitHub the first time its page is visited.
`docs/BoardAPI.md` is the contract; a front end needs nothing from this file but that."""

import datetime
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from .. import gh
from ..core import TRIAGE_BUCKET
from .. import core
from . import build, cache, data, plan, reconcile
from .keys import Keys
from .log import plural, say, seconds

STATIC = build.STATIC
TOKEN_HEADER = "X-Milestones-Token"
# Statuses an action can be left in when the page or the server goes away with work undone.
UNSETTLED = ("pending", "failed")

# The port the app asks for when none is named. A fixed one, so that the address — and with it
# the browser's history and anything bookmarked — is the same from one run to the next; if it
# is taken, any free port does.
DEFAULT_PORT = 8377

BROKEN_BUILD = """<!doctype html><meta charset="utf-8"><title>milestones: not built</title>
<body style="font: 16px system-ui; max-width: 44em; margin: 4em auto; padding: 0 16px">
<h1>The app's pages can't be served</h1>
<p>The front end in <code>milestones/board/static/</code> isn't a whole build:</p>
<ul>{problems}</ul>
<p>To fix it:</p>
<ol>{steps}</ol>
<p>The terminal running <code>milestones</code> says the same. The API is up either way:
see <code>docs/BoardAPI.md</code>.</p>"""


class Refused(Exception):
    """A request the board won't carry out, with the HTTP status to say so."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


KINDS = ("milestone", "priority", "effort", "labels", "title", "state", "comment",
         "priority-field", "effort-field")
# The organisation issue field a `*-field` kind clears. Priority and effort themselves are
# labels: asking for one becomes a `labels` action in `Board.normalise`.
FIELD_KINDS = {f"{kind}-field": spec["field"] for kind, spec in reconcile.KINDS.items()}
CLOSE_REASONS = ("completed", "not_planned")
# A label GitHub has no colour for yet (one this session is about to create) is shown grey.
NO_COLOUR = "ededed"


def card_value(card: dict, kind: str):
    """What a card holds for an action kind — the shape an action's `value` and `previous`
    take. A comment is not state: there is nothing to read back or put back."""
    if kind in FIELD_KINDS:
        return (card.get("fields") or {}).get(FIELD_KINDS[kind])
    if kind in reconcile.KINDS:
        # No action is of these kinds (`Board.normalise` makes them `labels`), but a
        # suggestion or a saved plan is, and is compared with what the card says.
        return next(iter(reconcile.reconcile(kind, card)["standard"]), None)
    if kind == "labels":
        return [l["name"] for l in card["labels"]]
    if kind == "state":
        return card.get("closed")
    if kind == "comment":
        return None
    return card[kind]


def set_card_value(card: dict, kind: str, value, colours: dict[str, str]) -> None:
    if kind in FIELD_KINDS:
        fields = card.setdefault("fields", {})
        if value is None:
            fields.pop(FIELD_KINDS[kind], None)
        else:  # only ever to put back what a clearing took away
            fields[FIELD_KINDS[kind]] = value
    elif kind == "labels":
        card["labels"] = [{"name": n, "color": colours.get(n, NO_COLOUR)} for n in value]
    elif kind == "state":
        card["closed"] = value
    elif kind != "comment":
        card[kind] = value


def state_path(repo: str, environ=None) -> Path:
    """Where a repo's undecided suggestions are kept between runs. Decisions live on GitHub;
    this is only what hasn't been decided yet, and what was turned down."""
    return state_dir(environ) / f"board-{repo.replace('/', '-')}.json"


def plan_path(repo: str, environ=None) -> Path:
    """Where a repo's read-only changes are kept: beside its suggestions."""
    return state_path(repo, environ).with_suffix(".plan.json")


def suggestion_key(s: dict) -> str:
    return json.dumps([s["issue"], s["kind"], s["value"], s["source"]], sort_keys=True)


class Board:
    """One repo's cards and milestones, and the log of what this session did to them.

    Every change is applied to the cards here first and written to GitHub after, by one
    worker thread in the order the changes were made — so the page never waits on the
    network, and two moves of one issue can't arrive out of order."""

    def __init__(self, repo: str, buckets: list[str], sort_key, fetch=data.fetch_frame,
                 pages=data.card_pages, write=None, fetch_cards=data.fetch_cards, state_file: Path | None = None, plan_file: Path | None = None,
                 aliases: dict[str, str] | None = None, first_id: int = 1):
        self.requested, self.buckets, self.sort_key = repo, buckets, sort_key
        # Action ids carry on from a reloaded board's, so an undo from a page that wasn't
        # refreshed finds no action rather than the wrong one.
        self.first_id = first_id
        # The repo's own labels that say a priority or effort, lower-cased name -> the
        # standard label it stands for (the config's `[labels]`). Read, never written.
        self.aliases = aliases or {}
        self.state_file = state_file
        self.plan_file = plan_file
        self.labels: list[dict] = []
        self.fields: dict = {}
        self.suggestions: dict[int, dict] = {}
        self.rejected: set[str] = set()
        # How the board was last looked at: what the columns were, the grouping, and which
        # milestones were ticked. The page's business; kept here because each run is on a new
        # port, which a browser treats as a new site with empty storage.
        self.view: dict = {}
        self._restored: list[dict] = []
        self._read_state()
        self.fetch, self.pages, self.write = fetch, pages, write or self._write
        self.fetch_cards = fetch_cards
        self.lock = threading.RLock()
        self.repo, self.cards, self.milestones = repo, {}, {}
        self.actions: dict[int, dict] = {}
        self.todo: queue.Queue = queue.Queue()
        self.listeners: list[queue.Queue] = []
        self.read_only = gh.read_only()
        # "new" until something reads the repo; `start` reads it in the background. Until its
        # milestones are read the snapshot says only that it is "loading", or why it "failed";
        # after that the board is "ready", and its cards arrive a page at a time.
        self.status, self.error = "new", None
        # While cards are arriving, how far the read has got: {what, seen, total}; else None.
        self.filling: dict | None = None
        # What the last read couldn't get, each {what, error, seen, total}. A board with gaps
        # shows what it has, removes nothing, and holds back what needs every card (below).
        self.gaps: list[dict] = []
        # When each write landed (time.monotonic()), by action id: a page read before a write
        # landed must not put the card back as it was.
        self.landed: dict[int, float] = {}
        # Set once the first read has finished, well or badly; `milestones suggest` waits on it.
        self.ready = threading.Event()
        self.reading, self.again = False, False
        self.on_ready = lambda board: None
        self.plan_restored = False
        # What `restore_plan` picked up and left out, for whoever started the board to say.
        self.plan_notes: tuple[int, list[str]] = (0, [])

    def start(self, on_ready=lambda board: None) -> None:
        """Start the writer, and read the repo without making anyone wait for it: a page can
        open on the board at once and watch it fill. `on_ready` is called when the first read
        ends, well or badly."""
        threading.Thread(target=self.work, daemon=True).start()
        self.status, self.on_ready = "loading", on_ready
        self.refresh()

    def refresh(self) -> None:
        """Read the whole repo again in the background; if a read is already under way, read
        again once it ends, since it may have passed a card before whatever prompted this."""
        with self.lock:
            if self.reading:
                self.again = True
                return
            self.reading = True

        def read():
            while True:
                self.load()
                with self.lock:
                    if not self.again:
                        self.reading = False
                        return
                    self.again = False
        threading.Thread(target=read, daemon=True).start()

    def summary(self) -> str:
        with self.lock:
            prs = sum(c["kind"] == "pr" for c in self.cards.values())
            open_milestones = sum(m["state"] == "open" for m in self.milestones.values())
        return (f"{plural(len(self.cards) - prs, 'open issue')}, "
                f"{plural(prs, 'open pull request')}, "
                f"{plural(open_milestones, 'open milestone')}")

    # --- state ---------------------------------------------------------------------------

    def load(self) -> None:
        """One read of the repo, in this thread: its milestones, labels and fields first, after
        which the board is ready to show, then its cards a page at a time, each sent to the
        pages as a `cards` event. Never raises: the outcome is the board's status and gaps,
        and a `reload` event at the start and the end."""
        began = time.monotonic()
        first = not self.ready.is_set()
        say(f"Reading the issues and pull requests in {self.requested} …")
        try:
            frame = self.fetch(self.requested)
        except (SystemExit, Exception) as error:  # SystemExit is gh's failure; anything else, a bug
            why = str(error) if isinstance(error, SystemExit) else f"{type(error).__name__}: {error}"
            with self.lock:
                if self.status == "ready":
                    # A refresh: keep showing what there is, and say it wasn't read again.
                    self.gaps = [{"what": "the repo", "error": why.strip().splitlines()[-1],
                                  "seen": 0, "total": None}]
                else:
                    self.status, self.error = "failed", why
            say(f"{self.requested} could not be read: {why}", "error")
            self._finished(first)
            return
        with self.lock:
            self.repo = frame["repo"]
            self.milestones = {m["number"]: m for m in frame["milestones"]}
            self.labels = frame.get("labels", [])
            self.fields = frame.get("fields", {})
            self.status, self.error, self.gaps = "ready", None, []
            self.filling = {"what": "issues", "seen": 0, "total": None}
        self.publish("reload", {})
        seen: set[int] = set()
        try:
            for page in self.pages(self.repo):
                if "gap" in page:
                    gap = {k: page[k] for k in ("what", "seen", "total")} | {"error": page["gap"]}
                    with self.lock:
                        self.gaps.append(gap)
                    say(f"{self.repo}: stopped reading {page['what']} after {page['seen']} of "
                        f"{page['total']}: {page['gap']}", "error")
                    continue
                self._take_page(page, seen, began)
        except (SystemExit, Exception) as error:
            why = str(error) if isinstance(error, SystemExit) else f"{type(error).__name__}: {error}"
            with self.lock:
                self.gaps.append({"what": "the rest", "error": why.strip().splitlines()[-1],
                                  "seen": len(seen), "total": None})
            say(f"{self.repo}: stopped reading: {why}", "error")
        with self.lock:
            # Only a kind read to the end says which cards have closed since the last read.
            unread = {g["what"] for g in self.gaps}
            complete = {"issue": "issues" not in unread, "pr": "pullRequests" not in unread}
            if "the rest" in unread:
                complete = {"issue": False, "pr": False}
            for number in [n for n, c in self.cards.items()
                           if n not in seen and complete[c["kind"]]]:
                del self.cards[number]
            if not self.gaps:
                # Kept suggestions for issues that have closed since they were made.
                self._restored = []
                self._save_state()
            self.filling = None
        if not self.gaps and not self.plan_restored:
            # Only now: a plan restored onto a board missing cards would drop their changes.
            self.plan_notes = self.restore_plan()
            self.plan_restored = True
        say(f"{self.repo}: {self.summary()}  ({seconds(began)})"
            + ("; incomplete, Refresh to try again" if self.gaps else ""),
            "warn" if self.gaps else None)
        self._finished(first)

    def _take_page(self, page: dict, seen: set[int], began: float) -> None:
        """Put one page of cards on the board and tell the pages."""
        with self.lock:
            fresh = {c["number"]: c for c in page["cards"]}
            self._reapply(fresh, since=began)
            self.cards.update(fresh)
            seen.update(fresh)
            # Suggestions kept from the last run, for the issues on this page.
            matched = [s for s in self._restored if s["issue"] in fresh]
            self._restored = [s for s in self._restored if s["issue"] not in fresh]
            changed = self._add_suggestions(matched) + self._rules(fresh)
            self.filling = {"what": page["what"], "seen": page["seen"], "total": page["total"]}
            payload = {"cards": list(fresh.values()), "filling": dict(self.filling),
                       "suggestions": [s for s in changed if s["status"] != "rejected"]}
        self.publish("cards", payload)

    def _finished(self, first: bool) -> None:
        self.ready.set()
        self.publish("reload", {})
        if first:
            self.on_ready(self)

    def _reapply(self, cards: dict[int, dict], since: float | None = None) -> None:
        """What this session changed and GitHub hasn't confirmed is still the truth here: a
        refetch must not put a card back where a failed write left it. Nor where a write that
        landed after `since` (when the read began) found it: the page may have been read
        before the write."""
        for action in self.actions.values():
            landed = since is not None and self.landed.get(action["id"], -1.0) >= since
            if (action["status"] in (*UNSETTLED, "would") or landed) and action["issue"] in cards:
                set_card_value(cards[action["issue"]], action["kind"], action["value"],
                               self._colours())

    def refresh_cards(self, numbers: list[int]) -> list[int]:
        """Read some cards again, and the milestones with them, without the rest of the
        repo: for a change made on GitHub, or a pull request that has merged. A card that
        is no longer open leaves the board; returns the numbers that did."""
        fetched = self.fetch_cards(self.repo, numbers)
        with self.lock:
            self.milestones = {m["number"]: m for m in fetched["milestones"]}
            fresh = {n: c for n, c in fetched["cards"].items() if c is not None}
            gone = [n for n, c in fetched["cards"].items() if c is None and n in self.cards]
            for n in gone:
                del self.cards[n]
            self._reapply(fresh)
            self.cards.update(fresh)
            self._rules(fresh)
        return gone

    def refresh_milestones(self) -> None:
        """Read the milestones again and nothing else: one was changed from another page."""
        fetched = data.fetch_milestones(self.repo)
        with self.lock:
            self.milestones = {m["number"]: m for m in fetched}

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
            if self.status in ("loading", "failed"):
                # Nothing to show until the milestones are read, which takes a second or two.
                # A page asks again until `loading` is gone.
                return {"repo": self.repo, "permissions": "read" if self.read_only else "write",
                        "loading": {}, "error": self.error}
            milestones = list(self.milestones.values())
            return {
                "repo": self.repo,
                # How far a read of the cards has got, while one is under way; and what the
                # last one couldn't read.
                "filling": self.filling,
                "gaps": list(self.gaps),
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
                # The one table of what a label means, so a front end that sorts cards by
                # priority reads "Priority: Critical" as the rules here do.
                "labelValues": {
                    kind: {**spec, "aliases": {
                        name: reconcile.STANDARD[label.lower()][1]
                        for name, label in self.aliases.items()
                        if reconcile.STANDARD[label.lower()][0] == kind}}
                    for kind, spec in reconcile.KINDS.items()},
                "view": self.view,
                "plan": self.plan_info(),
            }

    def publish(self, event: str, payload: dict) -> None:
        for listener in list(self.listeners):
            listener.put((event, payload))

    # --- actions -------------------------------------------------------------------------

    def _new_action(self, issue: int, kind: str, value, carried_by: int | None = None) -> dict:
        card = self.cards[issue]
        action = {"id": self.first_id + len(self.actions), "issue": issue, "kind": kind, "value": value,
                  "previous": card_value(card, kind), "carriedBy": carried_by, "error": None,
                  "status": "would" if self.read_only else "pending"}
        self.actions[action["id"]] = action
        set_card_value(card, kind, value, self._colours())
        return action

    def normalise(self, request: dict) -> tuple[str, object]:
        """Check one requested change and return the (kind, value) to act on, which is not
        always the pair asked for: a priority or an effort is a label, so it becomes the
        label set with the standard one in place of whatever said it before, and a label
        change given as {add, remove} becomes the set that results. Raises Refused for
        anything that can't be done."""
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
            label = None
            if value is not None:
                names = {name.lower(): name for name in spec["options"]}
                if not isinstance(value, str) or value.strip().lower() not in names:
                    raise Refused(400, f"{kind} is one of {', '.join(spec['options'])}"
                                       f", or null; not {value!r}")
                label = reconcile.standard_label(kind, names[value.strip().lower()])
            keep = [n for n in card_value(card, "labels")
                    if n not in reconcile.labels_about(kind, card)]
            return "labels", keep + ([label] if label else [])
        elif kind in FIELD_KINDS:
            # To clear it (null): the field is read, and what it says moves to a label. An
            # option's name is taken too, which is how undoing a clearing puts it back.
            name = FIELD_KINDS[kind]
            if card["kind"] != "issue" or name not in self.fields:
                raise Refused(400, f"#{card['number']} has no {name} field")
            if value is not None and value not in self.fields[name]["options"]:
                raise Refused(400, f"{kind} is null to clear the field, or one of "
                                   f"{', '.join(self.fields[name]['options'])} to put it back")
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
                field = f"{request['kind']}-field"
                if (field in FIELD_KINDS and request["value"] is None
                        and FIELD_KINDS[field] in self.fields and card_value(card, field)):
                    # Clearing a priority clears the organisation field with the label:
                    # the field is still read, so the card would go on showing its value.
                    made.append(self._new_action(issue, field, None))
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
                else:
                    say(f"{self.repo}: read-only, so kept in the plan: "
                        f"{action_text(action, self._titles())}")
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
            say(f"{self.repo}: gave up on {action_text(action, self._titles())}")
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
        elif kind in FIELD_KINDS:
            name = FIELD_KINDS[kind]
            field = self.fields[name]
            # `previous` is an option's name: undoing a clearing puts it back.
            change = (f'singleSelectOptionId: "{field["options"][value]}"' if value is not None
                      else "delete: true")
            gh.mutate(f"set #{number}'s {name} field to {value}",
                      f'mutation {{ setIssueFieldValue(input: {{issueId: '
                      f'"{self.cards[number]["id"]}", issueFields: [{{fieldId: "{field["id"]}", '
                      f"{change}}}]}}) {{ clientMutationId }} }}", owner)
        elif kind == "labels":
            # A standard label the repo hasn't got is made first, in its colour: the PUT
            # below would make it too, but grey and with no description.
            for body in reconcile.missing_standard(value, list(self._colours())):
                try:
                    gh.api(f"repos/{self.repo}/labels", method="POST", body=body)
                except SystemExit:
                    pass  # made since this board loaded: the PUT settles it either way
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
                if outcome["status"] == "done":
                    self.landed[action["id"]] = time.monotonic()
                said = action_text(action, self._titles())
            if outcome["status"] == "done":
                say(f"{self.repo}: wrote {said}")
            else:
                say(f"{self.repo}: could not write {said}: "
                    f"{outcome['error'].splitlines()[-1]}", "error")
            self.publish("action", action)
            self.todo.task_done()

    def unsettled(self) -> list[dict]:
        with self.lock:
            return [a for a in self.actions.values() if a["status"] in UNSETTLED]

    # --- the plan: a read-only session's changes --------------------------------------------

    def _titles(self) -> dict[int, str]:
        return {n: m["title"] for n, m in self.milestones.items()}

    def plan_info(self) -> dict:
        """What the page needs to say about the plan: where it is and how much is in it. On
        a read-only board that is this session's changes; on a writable one, what an earlier
        read-only session left to be reviewed."""
        with self.lock:
            if self.read_only:
                count = len(plan.net(list(self.actions.values())))
            else:
                count = len(plan.read(self.plan_file)["changes"]) if self.plan_file else 0
            return {"path": str(self.plan_file) if self.plan_file else None, "count": count}

    def save_plan(self) -> None:
        """Write this read-only session's net changes out, or remove the file when there
        are none left. Called after every change: a board that is killed loses nothing."""
        if not self.read_only or self.plan_file is None:
            return
        with self.lock:
            entries = plan.net(list(self.actions.values()))
            try:
                if not entries:
                    self.plan_file.unlink(missing_ok=True)
                    return
                self.plan_file.parent.mkdir(parents=True, exist_ok=True)
                saved = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                self.plan_file.write_text(json.dumps(
                    plan.document(self.repo, entries, self.cards, self._titles(), saved),
                    indent=2, ensure_ascii=False))
            except OSError as error:
                say(f"could not save the plan to {self.plan_file}: {error}", "warn")

    def restore_plan(self) -> tuple[int, list[str]]:
        """Pick a saved plan up again on a read-only board: (changes restored, the ones that
        no longer can be and why). A card that has since closed, or a milestone that has,
        is left out rather than stopping the rest."""
        if not self.read_only or self.plan_file is None:
            return 0, []
        restored, skipped = 0, []
        for entry in plan.read(self.plan_file)["changes"]:
            try:
                restored += len(self.submit([{k: entry[k] for k in ("issue", "kind", "value")}]))
            except Refused as refused:
                skipped.append(f"{plan.summary(entry, self._titles())}"
                               f" — {refused}")
        self.save_plan()
        return restored, skipped

    def review_plan(self) -> tuple[list[dict], int]:
        """On a writable board, turn a saved plan into suggestions to accept or reject:
        (the suggestions added, how many changes were dropped as already made or no longer
        possible). The plan file is used up by this; the suggestions are what persists."""
        if self.read_only:
            raise Refused(409, "this board is read-only: its changes are the plan. Start a "
                               "writable board to review them.")
        if self.plan_file is None:
            return [], 0
        with self.lock:
            # Each change is checked against its card, and the file is used up after: a card
            # not read yet would count as gone, and its change be lost with the file.
            if self.filling:
                raise Refused(409, "the board is still reading the repo; review the plan once "
                                   "every card is in")
            if self.gaps:
                raise Refused(409, "the board couldn't read every card; Refresh, and review "
                                   "the plan once it has")
            saved = plan.read(self.plan_file)
            proposed, dropped = [], 0
            for entry in saved["changes"]:
                card = self.cards.get(entry["issue"])
                made = card and plan.as_suggestion(
                    entry, card_value(card, entry["kind"]), str(saved.get("saved") or ""),
                    self._titles())
                try:
                    if not made:
                        raise Refused(404, "gone, or already so")
                    self.normalise(made)
                    proposed.append(made)
                except Refused:
                    dropped += 1
            added = self._add_suggestions(proposed)
            self._save_state()
            self.plan_file.unlink(missing_ok=True)
            return added, dropped + len(proposed) - len(added)

    def discard_plan(self) -> None:
        if self.read_only:
            raise Refused(409, "on a read-only board the plan is what you have done here; "
                               "undo the changes (z) to take them out of it")
        if self.plan_file is not None:
            self.plan_file.unlink(missing_ok=True)

    # --- suggestions ---------------------------------------------------------------------

    def _read_state(self) -> None:
        if self.state_file is None:
            return
        try:
            kept = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.rejected = set(kept.get("rejected") or [])
        self.view = kept["view"] if isinstance(kept.get("view"), dict) else {}
        self._restored = [s for s in kept.get("open") or [] if isinstance(s, dict)]

    def _save_state(self) -> None:
        """Keep what is undecided and what was turned down. A rule's suggestion is not kept
        while open — the rule will make it again — only remembered once rejected."""
        if self.state_file is None:
            return
        kept = {"view": self.view, "rejected": sorted(self.rejected),
                "open": [{k: s[k] for k in ("issue", "kind", "value", "reason", "source")}
                         for s in self.suggestions.values()
                         if s["status"] == "open" and not s["source"].startswith("rule:")]
                # Kept ones whose cards haven't been read yet, or couldn't be: saving the
                # view mid-read must not lose them.
                + [{k: s.get(k) for k in ("issue", "kind", "value", "reason", "source")}
                   for s in self._restored]}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(kept, indent=2))
        except OSError as error:
            say(f"could not save suggestions to {self.state_file}: {error}", "warn")

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
                    for p in reconcile.rule_suggestions(card, self.aliases)]
        return changed + self._add_suggestions(proposed)

    def set_view(self, view: dict) -> dict:
        """Remember how the board is being looked at, for the next run."""
        dims = ("milestone", "priority", "effort")
        by, group, shown = view.get("by"), view.get("group"), view.get("shown")
        if (by not in dims or not isinstance(group, list) or any(g not in dims for g in group)
                or not isinstance(shown, list)
                or any(isinstance(n, bool) or not isinstance(n, int) for n in shown)):
            raise Refused(400, "a view is {by: milestone|priority|effort, group: [...], "
                               "shown: [milestone numbers, 0 for none]}")
        detail = view.get("detail", True)
        if not isinstance(detail, bool):
            raise Refused(400, "a view's detail is true (the card panel is open) or false")
        with self.lock:
            self.view = {"by": by, "group": group, "shown": shown, "detail": detail}
            self._save_state()
            return self.view

    def suggest(self, proposed: list[dict]) -> list[dict]:
        """Take suggestions from outside — a coding agent, a script. Each is checked as an
        action would be, so one that could never be accepted is refused now, with why."""
        with self.lock:
            if not all(isinstance(p, dict) for p in proposed):
                raise Refused(400, "each suggestion is {issue, kind, value, reason, source}")
            for p in proposed:
                if p.get("kind") == "milestone" and isinstance(p.get("value"), str):
                    # By title, for a caller that has no way to know the numbers.
                    found = [m for m in self.milestones.values()
                             if m["title"].lower() == p["value"].strip().lower()]
                    if not found:
                        raise Refused(404, f"no milestone called {p['value']!r}; the open "
                                           "ones are: " + ", ".join(sorted(
                                               m["title"] for m in self.milestones.values()
                                               if m["state"] == "open")))
                    # An open one first: a closed namesake is not where anything should go.
                    p["value"] = min(found, key=lambda m: m["state"] != "open")["number"]
                self.normalise(p)
                if str(p.get("source") or "").startswith("rule:"):
                    raise Refused(400, "a source starting 'rule:' is the board's own")
            added = self._add_suggestions(proposed)
            self._save_state()
            sources = sorted({str(p.get("source") or "someone") for p in proposed})
            say(f"{self.repo}: {plural(len(added), 'new suggestion')} from {', '.join(sources)}"
                + (f" ({len(proposed) - len(added)} already made or turned down)"
                   if len(added) < len(proposed) else ""))
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


def state_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "milestones"


REPO_PART = re.compile(r"[\w.-]+")
# Whose pull requests the PRs page lists besides the ones you wrote.
PR_FLAGS = ("assigned", "review_requested", "mentions")


class App:
    """Every board this run has opened. A board is made, and its repo read, the first time
    something asks for it; after that it stays for as long as the app runs, so going back to
    a repo costs nothing."""

    def __init__(self, config: dict, next_ids: dict[str, int] | None = None, make_board=None,
                 state: Path | None | bool = True):
        # Where the last answers are kept between runs; None keeps nothing (the tests).
        state = state_dir() if state is True else state
        self.config = config
        self.read_only = gh.read_only()
        self.lock = threading.Lock()
        # Boards by GitHub's own name for the repo, lower-cased; and every spelling that has
        # been asked for, to that name. Two spellings of one repo must never get two boards:
        # each would hold its own idea of the cards and overwrite the other's saved state.
        self.boards: dict[str, Board] = {}
        self.names: dict[str, str] = {}
        # Where each repo's action ids carry on from, after a `--reload`.
        self.next_ids = next_ids or {}
        self.make_board = make_board or self._make_board
        self.listeners: list[queue.Queue] = []
        # The pages that are not a board: each a slow read, shown from its last answer first.
        self.sections = {
            "overview": cache.Section("overview", state and state / "overview.json",
                                      self._overview, self.publish, describe_overview,
                                      "the tracked repositories' milestones"),
            "prs": cache.Section("prs", state and state / "prs.json", self._prs, self.publish,
                                 describe_prs, "your pull requests"),
        }

    def publish(self, event: str, payload: dict) -> None:
        for listener in list(self.listeners):
            listener.put((event, payload))

    def _overview(self, params: dict):
        """Every tracked repo's milestones and what is wrong with them, then — the slow half,
        a search — how much in each repo is on no milestone."""
        # The config as it is now: `milestones add` in another terminal shows up on a refresh.
        self.config = core.load_config(needs_repos=False)
        fetched = core.fetch_all_milestones(self.config)

        def view(untriaged_items):
            return {**core.status_data(self.config, fetched, untriaged_items),
                    "findings": core.collect_findings(self.config, fetched),
                    "kinds": core.KINDS, "buckets": self.config["buckets"],
                    "repos": list(self.config["repos"]),
                    "today": datetime.date.today().isoformat()}
        kept = self.sections["overview"].data or {}
        # Part-way: the milestones are new, the untriaged counts still the last ones known.
        yield {**view([]), "untriaged": kept.get("untriaged", []), "untriaged_fresh": False}
        items = core.fetch_untriaged(self.config, None, kind=None) if self.config["repos"] else []
        yield {**view(items), "untriaged_fresh": True}

    def _prs(self, params: dict):
        self.config = core.load_config(needs_repos=False)
        yield {"prs": core.prs_data(self.config, **{k: bool(params.get(k)) for k in PR_FLAGS})}

    def _make_board(self, repo: str) -> Board:
        return Board(repo, self.config["buckets"], core.sort_key, state_file=state_path(repo),
                     plan_file=plan_path(repo),
                     aliases=self.config["labels"].get(repo.lower()),
                     first_id=self.next_ids.get(repo.lower(), 1))

    def canonical(self, repo: str) -> str:
        """GitHub's name for a repo: it follows a rename, settles the case, and is where a
        mistyped name is found out."""
        try:
            return gh.api(f"repos/{repo}")["full_name"]
        except SystemExit as exit:
            raise Refused(404, f"can't read {repo}: {str(exit).splitlines()[-1]}")

    def board(self, repo: str) -> Board:
        owner, _, name = repo.partition("/")
        if not (REPO_PART.fullmatch(owner) and REPO_PART.fullmatch(name)):
            raise Refused(404, f"not a repo: {repo!r}")
        with self.lock:
            if repo.lower() in self.names:
                return self.boards[self.names[repo.lower()]]
        found = self.canonical(repo)
        with self.lock:
            key = found.lower()
            if key not in self.boards:
                self.boards[key] = self.make_board(found)
                self.boards[key].start(self.loaded)
            self.names[repo.lower()] = self.names[key] = key
            return self.boards[key]

    def loaded(self, board: Board) -> None:
        """Say in the terminal what became of a plan left by a read-only run. (The board says
        what its read came to.)"""
        if board.status == "failed":
            return
        restored, skipped = board.plan_notes
        if restored or skipped:
            say(f"{board.repo}: picked up {plural(restored, 'change')} from the plan left by "
                f"an earlier read-only run ({board.plan_file})."
                + "".join(f"\n  left out: {line}" for line in skipped))
        elif not board.read_only and board.plan_info()["count"]:
            say(f"{board.repo}: a read-only run left a plan of "
                f"{plural(board.plan_info()['count'], 'change')} ({board.plan_file}); "
                "the board will offer to review it.")

    # --- milestones, from the home page -----------------------------------------------------

    def _milestone(self, repo: str, number: int) -> dict:
        """GitHub's record of a milestone, read now: a fix is never decided from a kept copy."""
        owner, _, name = repo.partition("/")
        if not (REPO_PART.fullmatch(owner) and REPO_PART.fullmatch(name)):
            raise Refused(404, f"not a repo: {repo!r}")
        try:
            return gh.api(f"repos/{repo}/milestones/{number}")
        except SystemExit as exit:
            raise Refused(404, f"can't read milestone {number} of {repo}: "
                               f"{str(exit).splitlines()[-1]}")

    def _fixed(self, repo: str, cards: bool = False) -> None:
        """After a milestone changed: read the overview again, and tell that repo's board if
        one is open — its milestones, and its cards too if they were moved."""
        self.sections["overview"].refresh()
        with self.lock:
            board = self.boards.get(self.names.get(repo.lower(), ""))
        if board is not None and board.status == "ready":
            if cards:
                board.refresh()
                return

            def read():
                try:
                    board.refresh_milestones()
                except SystemExit:
                    return  # the board's own Refresh will say what is wrong
                board.publish("reload", {})
            threading.Thread(target=read, daemon=True).start()

    def _write(self, what: str, call):
        if self.read_only:
            raise Refused(403, f"read-only: would {what}")
        try:
            done = call()
        except SystemExit as exit:
            raise Refused(502, f"could not {what}: {exit}")
        say(f"Done: {what}")
        return done

    def fix_milestone(self, repo: str, number: int, change: dict) -> dict:
        milestone = self._milestone(repo, number)
        body = checked_fix(milestone, change, self.config["buckets"])
        said = ", ".join(f"{k} {v}" for k, v in change.items())
        changed = self._write(f"set milestone '{milestone['title']}' in {repo}: {said}",
                              lambda: gh.api(f"repos/{repo}/milestones/{number}",
                                             method="PATCH", body=body))
        self._fixed(repo)
        return data.norm_milestone(changed)

    def delete_milestone(self, repo: str, number: int) -> None:
        milestone = self._milestone(repo, number)
        checked_delete(milestone, self.config["buckets"])
        self._write(f"delete milestone '{milestone['title']}' from {repo}",
                    lambda: gh.api(f"repos/{repo}/milestones/{number}", method="DELETE"))
        self._fixed(repo)

    def rollover(self, repo: str, number: int, to: str, close: bool, dry: bool) -> dict:
        """Move the open issues and pull requests on a milestone onto the open one called
        `to`, and close the first if asked. `dry` only says what would move."""
        milestone = self._milestone(repo, number)
        if not isinstance(to, str) or not to.strip():
            raise Refused(400, "send {to: <the title of an open milestone>}")
        try:
            src, dst, items = core.rollover_plan(repo, milestone["title"], to.strip(), close,
                                                 self.config["buckets"])
        except SystemExit as exit:
            raise Refused(409, str(exit))
        moving = [{"number": i["number"], "title": i["title"], "pr": "pull_request" in i}
                  for i in items]
        if not dry:
            self._write(f"move {len(items)} from '{src['title']}' to '{dst['title']}' in {repo}"
                        + (", and close the first" if close else ""),
                        lambda: core.rollover_move(repo, src, dst, items, close))
            self._fixed(repo, cards=True)
        return {"from": src["title"], "to": dst["title"], "items": moving, "moved": not dry,
                "closed": close and not dry}

    def info(self) -> dict:
        with self.lock:
            return {"permissions": "read" if self.read_only else "write",
                    "repos": list(self.config["repos"]), "focus": list(self.config["focus"]),
                    "config": str(core.config_path()),
                    "boards": [{"repo": b.repo, "status": b.status} for b in self.boards.values()]}

    def pending(self) -> bool:
        with self.lock:
            boards = list(self.boards.values())
        return any(a["status"] == "pending" for b in boards for a in b.unsettled())


def checked_fix(milestone: dict, change: dict, buckets: list[str]) -> dict:
    """The PATCH body for a change to a milestone made from the home page — `title`, `due`
    (YYYY-MM-DD or null) and `state`, any of them — or Refused. `milestone` is GitHub's own
    record of it, read just now. Closing a standing bucket is refused here as everywhere:
    a closed bucket is invisible to the overview and to triage."""
    unknown = set(change) - {"title", "due", "state"}
    if unknown or not change:
        raise Refused(400, "send any of {title, due: 'YYYY-MM-DD' | null, state: 'open' | 'closed'}")
    body = {}
    if "title" in change:
        if not isinstance(change["title"], str) or not change["title"].strip():
            raise Refused(400, "a milestone needs a title")
        body["title"] = change["title"].strip()
    if "due" in change:
        if change["due"] is not None:
            try:
                time.strptime(change["due"], "%Y-%m-%d")
            except (TypeError, ValueError):
                raise Refused(400, f"due must be YYYY-MM-DD or null, not {change['due']!r}")
        body["due_on"] = data.due_instant(change["due"])
    if "state" in change:
        if change["state"] not in ("open", "closed"):
            raise Refused(400, "state is open or closed")
        if change["state"] == "closed" and milestone["title"] in buckets:
            raise Refused(409, f"'{milestone['title']}' is a standing bucket, so it outlives "
                               "the issues on it; closing it would hide it")
        body["state"] = change["state"]
    return body


def checked_delete(milestone: dict, buckets: list[str]) -> None:
    """Refuse to delete a milestone that is a standing bucket, or that anything was ever
    filed on: deleting is for the empty ones, and takes every issue off the rest."""
    if milestone["title"] in buckets:
        raise Refused(409, f"'{milestone['title']}' is a standing bucket; it is not deleted")
    # GitHub's counts here take in pull requests, which is what is wanted.
    filed = milestone["open_issues"] + milestone["closed_issues"]
    if filed:
        raise Refused(409, f"'{milestone['title']}' has {filed} issues and pull requests on "
                           "it, open or closed; only an empty milestone is deleted")


def allowed_host(host: str | None, port: int) -> bool:
    """A request must name this server as it is reached on loopback. Anything else is a
    page on another site trying a rebound DNS name against a server that can write to
    GitHub."""
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


# What a built page loads: an asset missing from the build is worth saying in the terminal,
# while a stray request (a favicon the browser guessed at) is not.
REFERENCED = re.compile(r"/(?:[\w-]+/)?[\w.-]+\.(?:js|css)$")
_said_broken: list[str] = []


def build_check() -> str | None:
    """None if the built pages can be served; otherwise the page to serve instead, saying
    what is wrong and what to do, which the terminal is told too (once for each problem)."""
    problems = build.problems()
    if not problems:
        _said_broken.clear()
        return None
    steps = build.remedy(build.watchers())
    if problems != _said_broken:
        _said_broken[:] = problems
        say("The app's pages can't be served:\n" + "\n".join(f"  {p}" for p in problems)
            + "\nTo fix it:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1)),
            "error")
    def items(lines):
        return "".join("<li>" + re.sub(r"`([^`]*)`", r"<code>\1</code>", html.escape(line))
                       + "</li>" for line in lines)
    return BROKEN_BUILD.format(problems=items(problems), steps=items(steps))


def page_for(path: str) -> str | None:
    """The built file a page's address is served from, relative to the static folder; None
    for an address that is a file of its own. `/r/OWNER/NAME/` is the board, whichever repo."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "index.html"
    if parts[0] == "r" and len(parts) == 3:
        return "board/index.html"
    if len(parts) == 1 and "." not in parts[0]:
        return f"{parts[0]}/index.html"
    return None


def make_handler(app: App, token: str):
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
                # EventSource can't set a header, so an event stream alone may carry the
                # token in its query string.
                given = self.headers.get(TOKEN_HEADER)
                if given is None and url.path.endswith("/events"):
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
            parts = [unquote(p) for p in url.path.split("/") if p]
            try:
                if parts[:2] != ["api", "v1"]:
                    if method != "GET":
                        raise Refused(404, "not found")
                    return self._static(url.path)
                rest = parts[2:]
                if rest[:1] == ["repos"] and len(rest) >= 4:
                    return self._board(method, app.board(f"{rest[1]}/{rest[2]}"), rest[3:])
                if method == "GET" and rest == ["app"]:
                    return self._json(200, app.info())
                if method == "GET" and rest == ["events"]:
                    return self._events(app.listeners)
                if method == "GET" and rest == ["overview"]:
                    return self._json(200, app.sections["overview"].get())
                if method == "GET" and rest == ["prs"]:
                    asked = parse_qs(url.query)
                    return self._json(200, app.sections["prs"].get(
                        {k: True for k in PR_FLAGS if asked.get(k, [""])[0] == "1"}))
                if rest[:1] == ["milestones"] and len(rest) in (4, 5) and rest[3].isdigit():
                    repo, number = f"{rest[1]}/{rest[2]}", int(rest[3])
                    if method == "PATCH" and len(rest) == 4:
                        return self._json(200, {"milestone": app.fix_milestone(
                            repo, number, self._body())})
                    if method == "DELETE" and len(rest) == 4:
                        app.delete_milestone(repo, number)
                        return self._json(200, {"deleted": number})
                    if method == "POST" and rest[4:] == ["rollover"]:
                        body = self._body()
                        return self._json(200, app.rollover(
                            repo, number, body.get("to"), bool(body.get("close")),
                            bool(body.get("dry"))))
                if method == "POST" and rest == ["refresh"]:
                    names = self._body().get("sections") or list(app.sections)
                    if not isinstance(names, list) or any(n not in app.sections for n in names):
                        raise Refused(400, f"sections is a list of: {', '.join(app.sections)}")
                    for name in names:
                        app.sections[name].refresh()
                    return self._json(200, {name: app.sections[name].snapshot() for name in names})
                raise Refused(404, "not found")
            except Refused as refused:
                if parts[:2] == ["api", "v1"]:
                    say(f"Refused {method} {url.path}: {refused}", "warn")
                self._json(refused.status, {"error": str(refused)})

        def _board(self, method: str, board: Board, rest: list[str]) -> None:
            """One repo's board: everything under /api/v1/repos/OWNER/NAME/."""
            if method == "GET" and rest == ["board"]:
                return self._json(200, board.snapshot())
            if method == "GET" and rest == ["events"]:
                return self._events(board.listeners)
            if method == "POST" and rest == ["refresh"] and board.status == "failed":
                board.status = "loading"
                board.refresh()
                return self._json(200, {**board.snapshot(), "gone": []})
            if method == "POST" and rest == ["suggestions"]:
                # From `milestones suggest`, which may be the first thing to name this repo:
                # it waits for the read rather than being told to come back.
                board.ready.wait(timeout=600)
            if board.status != "ready":
                raise Refused(503, f"{board.repo} could not be read: {board.error}"
                              if board.status == "failed"
                              else f"still reading {board.repo} from GitHub")
            if method == "POST" and rest == ["actions"]:
                requests = self._body().get("actions")
                if not isinstance(requests, list):
                    raise Refused(400, "send {actions: [{issue, kind, value}, ...]}")
                made = board.submit(requests)
                board.save_plan()
                for action in made:
                    board.publish("action", action)
                board.publish("plan", board.plan_info())
                return self._json(200, {"actions": made})
            if method == "POST" and rest == ["plan", "review"]:
                added, dropped = board.review_plan()
                for suggestion in added:
                    board.publish("suggestion", suggestion)
                board.publish("plan", board.plan_info())
                return self._json(200, {"suggestions": added, "dropped": dropped})
            if method == "POST" and rest == ["plan", "discard"]:
                board.discard_plan()
                board.publish("plan", board.plan_info())
                return self._json(200, {"plan": board.plan_info()})
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
                board.save_plan()
                board.publish("plan", board.plan_info())
                for action in actions:
                    board.publish("action", action)
                for suggestion in changed:
                    board.publish("suggestion", suggestion)
                return self._json(200, {"suggestions": changed, "actions": actions})
            if method == "POST" and rest == ["view"]:
                return self._json(200, {"view": board.set_view(self._body())})
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
                # With {issues: [N, …]}, just those cards; otherwise the whole repo.
                numbers = self._body().get("issues")
                if numbers is not None and (not isinstance(numbers, list) or not numbers
                                            or not all(isinstance(n, int) for n in numbers)):
                    raise Refused(400, "send {issues: [number, …]}, or no body")
                if not numbers:
                    # The whole repo is read in the background, its cards arriving as events.
                    board.refresh()
                    return self._json(200, {**board.snapshot(), "gone": []})
                try:
                    gone = board.refresh_cards(numbers)
                except SystemExit as exit:
                    raise Refused(502, str(exit))
                board.publish("reload", {})
                return self._json(200, {**board.snapshot(), "gone": gone})
            raise Refused(404, "not found")

        def _static(self, path: str) -> None:
            page = page_for(path)
            name = page or path.lstrip("/")
            target = (STATIC / name).resolve()
            if page:
                if broken := build_check():
                    return self._send(500, broken.encode(), "text/html; charset=utf-8")
                say(f"Opened {page_name(page, path)}")
            if STATIC.resolve() not in target.parents or not target.is_file():
                if not page and REFERENCED.match(path):
                    say(f"A page asked for {path}, which the build doesn't have; "
                        "`npm run build` makes it", "warn")
                raise Refused(404, "not found")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind.endswith("javascript"):
                kind += "; charset=utf-8"
            self._send(200, target.read_bytes(), kind)

        def _events(self, listeners: list) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            listener: queue.Queue = queue.Queue()
            listeners.append(listener)
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
                listeners.remove(listener)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


# Set for the process a `--reload` app replaces itself with: the listening socket's fd, the
# token and each board's next action id, as JSON.
HANDOFF = "MILESTONES_BOARD_HANDOFF"
SOURCE = Path(__file__).resolve().parents[1]


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        """A page that goes away before its answer is written (a reload, a closed tab) is not
        worth a traceback in the app's log; anything else still gets one."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def listen(port: int, handler, fd: int | None = None) -> ThreadingHTTPServer:
    """The app's server: on a new socket, or on the one a reloaded app handed over. Taking
    it over rather than binding the port again means a request made while the new process is
    still starting waits in the socket's backlog instead of being refused."""
    if fd is None:
        server = Server(("127.0.0.1", port), handler)
    else:
        server = Server(("127.0.0.1", 0), handler, bind_and_activate=False)
        server.socket.close()
        server.socket = socket.socket(fileno=fd)
        server.server_address = server.socket.getsockname()
    server.daemon_threads = True
    return server


def source_stamp(root: Path = SOURCE) -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in root.rglob("*.py")}


def importable() -> str | None:
    """None if the package as it now stands on disk imports and can still reload, else what
    went wrong: an app must not replace itself with code that dies at its first line, nor
    with code from before `--reload` (a branch lower in a stack, checked out to restack it),
    which would refuse the flag on its own command line and stop."""
    found = subprocess.run([sys.executable, "-c",
                            "import inspect, milestones.cli, milestones.board.client\n"
                            "from milestones.board import server\n"
                            "assert 'reload' in inspect.signature(server.run).parameters, "
                            "'this code has no --reload'"], capture_output=True, text=True)
    return None if found.returncode == 0 else (found.stderr.strip() or "import failed")


def watch_sources(server: ThreadingHTTPServer, app: App, reloading: threading.Event,
                  last: dict[str, int], interval: float = 1.0) -> None:
    """`--reload`: once the package's Python has changed and then stayed put for a moment (a
    checkout or a rebase writes many files), and no write is queued, stop serving so that `run`
    can replace the process. A change that doesn't import is reported and waited out.

    `last` is the source as it was when this process imported it, not as it is when the
    watching starts: a change saved in between would otherwise never be noticed."""
    while True:
        time.sleep(interval)
        now = source_stamp()
        if now == last:
            continue
        while True:
            time.sleep(interval * 2)
            settled = source_stamp()
            if settled == now:
                break
            now = settled
        last = now
        problem = importable()
        if problem:
            say(f"The app's source changed but doesn't import, so it is not reloading:\n"
                f"{problem.splitlines()[-1]}", "warn")
            continue
        while app.pending():
            time.sleep(0.2)
        say("The app's source changed: reloading …")
        reloading.set()
        server.shutdown()
        return


def run(config: dict, path: str = "/", query: list[tuple[str, str]] | None = None,
        port: int | None = None, open_browser: bool = True, reload: bool = False) -> None:
    """Start the app and open `path` in it — or, if it is already running, open `path` in the
    one that is: there is only ever one, so that a repo only ever has one board."""
    # Imported here, as client imports this module.
    from .client import running_session, write_session
    handoff = json.loads(os.environ.pop(HANDOFF, "null"))
    # Before anything is read: the code running is what was on disk about now. See watch_sources.
    imported = source_stamp() if reload else {}
    mode = "read" if gh.read_only() else "write"
    other = running_session()
    # After a reload the session file names this very process, which is still the same pid.
    if other and not (handoff and int(other["pid"]) == os.getpid()):
        if other.get("permissions", mode) != mode:
            # The one thing the running app can't be asked to change: someone who asked for
            # a read-only look must not be handed a page that writes.
            raise SystemExit(
                f"milestones is already running at {other['url']} with {other['permissions']} "
                f"permissions, and this run asks for {mode}. Stop that one first (Ctrl-C "
                f"there, or kill {other['pid']}).")
        url = page_url(other["url"], other["token"], path, query)
        print(f"milestones is already running (pid {other['pid']}); opening there:\n{url}",
              file=sys.stderr)
        if open_browser:
            webbrowser.open(url)
        return
    app = App(config, next_ids=handoff["next_ids"] if handoff else None)
    token = handoff["token"] if handoff else secrets.token_urlsafe(24)
    handler = make_handler(app, token)
    if handoff:
        server = listen(0, handler, handoff["fd"])
    else:
        try:
            server = listen(DEFAULT_PORT if port is None else port, handler)
        except OSError as error:
            if port is not None:
                raise SystemExit(f"Can't listen on port {port}: {error.strerror}")
            server = listen(0, handler)  # the usual port is taken: any free one
    base = f"http://127.0.0.1:{server.server_address[1]}"
    url = page_url(base, token, path, query)
    # Where `milestones suggest`, and the next `milestones`, find this app.
    session = write_session(base, token, mode)
    keys = Keys(url)
    print(("milestones  (read-only: nothing will be written)" if app.read_only else "milestones")
          + f"\n{url}\n"
          + ("q or Ctrl-C to stop, o to open the page again." if keys.start() else "Ctrl-C to stop.")
          + ("  Reloads when its Python changes." if reload else "") + "\n",
          file=sys.stderr, flush=True)
    if not handoff:
        repos = len(config["repos"])
        say(f"Tracking {plural(repos, 'repository', 'repositories')}, as {core.config_path()} "
            "says" if repos else f"No repositories tracked yet: `milestones add OWNER/NAME`, "
            f"or edit {core.config_path()}")
    if build_check() is None and (stale := build.warnings()):
        say("The app's pages are older than the code checked out, so they may not match it:\n"
            + "\n".join(f"  {w}" for w in stale) + "\n"
            + "\n".join(f"  {s}" for s in build.remedy(build.watchers())), "warn")
    if open_browser and not handoff:
        webbrowser.open(url)
    reloading = threading.Event()
    if reload:
        threading.Thread(target=watch_sources, args=(server, app, reloading, imported),
                         daemon=True).start()
    # A plain `kill` should end the app as Ctrl-C does, so that the session file goes and
    # the closing lines are printed. The first one also ignores the rest: `pkill -f` reaches
    # both `uv run` and this process, and uv passes its own on, so two arrive together and the
    # second used to land before the clean-up below could ignore it.
    def stop(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        keys.restore()
        # `uv run` passes a SIGTERM on to its child, so a `kill` aimed at both arrives
        # twice; the second must not cut the clean-up short.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not reloading.is_set():
            session.unlink(missing_ok=True)
            # Every open page switches itself off on this (ui/shared/alive.ts) rather than
            # finding out at its next click. A reload sends nothing: it keeps the socket, and
            # the pages never notice.
            app.publish("stopped", {})
            for board in list(app.boards.values()):
                board.publish("stopped", {})
        # A write already handed to gh finishes or fails within its own timeout; give the
        # queues a moment, then say what never reached GitHub.
        deadline = time.time() + 10
        while time.time() < deadline and app.pending():
            time.sleep(0.2)
        for board in list(app.boards.values()):
            if board.actions:
                print("\n".join(closing_lines(board.snapshot())), file=sys.stderr, flush=True)
        if not reloading.is_set() and (app.listeners or any(b.listeners for b in app.boards.values())):
            time.sleep(0.3)  # for the event streams' threads to send `stopped` before exit
    if reloading.is_set():
        # The same process, pid and all, on the same socket: every page's URL and token, and
        # the session file `milestones suggest` reads, stay good. What this run kept only in
        # memory goes: the boards (each is read again when its page next asks), the undo
        # history, and the record of any write that failed.
        fd = server.socket.fileno()
        os.set_inheritable(fd, True)
        os.environ[HANDOFF] = json.dumps({
            "fd": fd, "token": token,
            "next_ids": {key: b.first_id + len(b.actions) for key, b in app.boards.items()}})
        # An ignored signal stays ignored across exec, which would leave the new process
        # unkillable until it has set its own handler.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.execv(sys.executable, sys.orig_argv)


def board_path(repo: str) -> str:
    return f"/r/{repo}/"


def board_query(shown: list[str] | None = None, by: str | None = None,
                group: str | None = None, card: int | None = None) -> list[tuple[str, str]]:
    """The view a board is asked to open on — `from` once per milestone, `by`, `group` — and
    the card to open it at, as the query the page reads."""
    return ([("by", by)] * bool(by) + [("group", group)] * bool(group)
            + [("from", t) for t in shown or []] + [("card", str(card))] * bool(card))


def page_url(base: str, token: str, path: str = "/",
             query: list[tuple[str, str]] | None = None) -> str:
    """The address to open. What the page is asked to show is a query string, which the page
    reads and then keeps up to date as the view changes. The token rides in the fragment,
    which a browser never sends to a server or leaves in a Referer; the page reads it and
    sends it back as a header."""
    asked = urlencode(query or [], quote_via=quote)
    return f"{base}{path}{'?' + asked if asked else ''}#t={token}"


def closing_lines(snapshot: dict) -> list[str]:
    """What the session wrote, and — the part that matters — what it didn't."""
    titles = {m["number"]: m["title"] for m in snapshot["milestones"]}
    actions = snapshot["actions"]
    lines = ["", f"{sum(a['status'] == 'done' for a in actions)} written to {snapshot['repo']}."]
    for status, label in (("failed", "NOT written (failed)"),
                          ("pending", "NOT written (still queued)"),
                          ("would", "not written (read-only; kept in the plan)")):
        left = [a for a in actions if a["status"] == status]
        if left:
            lines.append(f"{len(left)} {label}:")
            lines += [f"  {action_text(a, titles)}"
                      + (f"  — {a['error'].splitlines()[0]}" if a["error"] else "")
                      for a in left]
    return lines


def action_text(action: dict, titles: dict[int, str]) -> str:
    """One action in a line: `#12 → Needed soon`, `#12 labels: ["bug"]`."""
    said = (f"#{action['issue']} → {titles.get(action['value'], 'no milestone')}"
            if action.get("kind", "milestone") == "milestone"
            else f"#{action['issue']} {action['kind']}: "
                 f"{json.dumps(action['value'], ensure_ascii=False)[:60]}")
    return said + (f" (with #{action['carriedBy']})" if action.get("carriedBy") else "")


def describe_overview(overview: dict) -> str:
    """The terminal's line for each part of the overview's read."""
    if not overview.get("untriaged_fresh"):
        repos = len(overview.get("repos", []))
        fixes = len(overview.get("findings", []))
        return (f"{plural(len(overview.get('milestones', [])), 'open milestone')} across "
                f"{plural(repos, 'tracked repository', 'tracked repositories')}; "
                + (f"{plural(fixes, 'thing')} to fix" if fixes else "nothing to fix"))
    rows = overview.get("untriaged", [])
    issues, prs = sum(r["issues"] for r in rows), sum(r["prs"] for r in rows)
    parked = sum(r["needs_triage"] for r in rows)
    with_any = sum(bool(r["issues"] or r["prs"]) for r in rows)
    return (f"{plural(issues, 'issue')} and {plural(prs, 'pull request')} on no milestone, "
            f"in {plural(with_any, 'repository', 'repositories')}"
            + (f"; {parked} parked on {TRIAGE_BUCKET}" if parked else ""))


def describe_prs(answer: dict) -> str:
    prs = answer.get("prs", [])
    repos = {p["repo"].lower() for p in prs}
    tracked = sum(p["group"] == "tracked" for p in prs)
    drafts = sum(bool(p["draft"]) for p in prs)
    return (f"Found {plural(len(prs), 'open pull request')} across "
            f"{plural(len(repos), 'repository', 'repositories')}: {tracked} in tracked ones"
            + (f", {plural(drafts, 'draft')}" if drafts else ""))


def page_name(page: str, path: str) -> str:
    """What the terminal calls a page when it is opened."""
    if page == "board/index.html":
        return f"the board for {'/'.join([p for p in path.split('/') if p][1:3])}"
    return {"index.html": "the home page", "prs/index.html": "your pull requests"}.get(page, path)
