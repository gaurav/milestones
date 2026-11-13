"""Talking to a board that is already running: how `milestones suggest` — and so a coding
agent in another terminal — puts a proposal in front of the person at the board.

A running board leaves a session file saying where it is and the token to use; this reads it."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import reconcile
from .server import CLOSE_REASONS, TOKEN_HEADER, state_path


def session_path(repo: str, environ=None) -> Path:
    return state_path(repo, environ).with_suffix(".session.json")


def write_session(repo: str, url: str, token: str) -> Path:
    """Say where this board is. Mode 0600: the token in it is what lets a request write to
    GitHub through the board."""
    path = session_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps({"repo": repo, "url": url, "token": token, "pid": os.getpid()}))
    return path


def running_session(repo: str) -> dict | None:
    """The session of a board that is still running for a repo, if there is one. A file left
    by a board that was killed outright names a process that is gone, and counts for nothing."""
    try:
        session = find_session(repo)
        os.kill(int(session["pid"]), 0)
    except (SystemExit, KeyError, ValueError, TypeError, ProcessLookupError):
        return None
    except PermissionError:
        pass  # it exists, and is someone else's
    return session


def find_session(repo: str) -> dict:
    """The running board for a repo, whatever case the name was typed in."""
    wanted = session_path(repo)
    for path in wanted.parent.glob("board-*.session.json") if wanted.parent.is_dir() else []:
        try:
            session = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if str(session.get("repo", "")).lower() == repo.lower():
            return session
    raise SystemExit(f"No board is running for {repo}. Start one in another terminal: "
                     f"milestones board {repo}")


def post(session: dict, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        session["url"] + path, data=json.dumps(payload).encode(), method="POST",
        headers={TOKEN_HEADER: session["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error")
        except ValueError:
            message = None
        raise SystemExit(f"The board refused: {message or error.reason}")
    except OSError:
        raise SystemExit(f"The board for {session['repo']} is not answering at {session['url']}; "
                         f"it may have stopped. Start it again: milestones board {session['repo']}")


def proposals(issue: int, reason: str, source: str, milestone: str | None = None,
              priority: str | None = None, effort: str | None = None,
              add_labels: list[str] | None = None, remove_labels: list[str] | None = None,
              title: str | None = None, comment: str | None = None,
              close: str | None = None) -> list[dict]:
    """One suggestion per thing asked for, each carrying the same reason — so the person at
    the board can take the priority and leave the milestone. `none` clears a milestone, a
    priority or an effort; a milestone is named by title, which the board resolves."""
    def clearable(value: str):
        return None if value.strip().lower() == "none" else value.strip()

    made = []
    if milestone is not None:
        made.append(("milestone", clearable(milestone)))
    for kind, value in (("priority", priority), ("effort", effort)):
        if value is not None:
            made.append((kind, clearable(value)))
    if add_labels or remove_labels:
        made.append(("labels", {"add": list(add_labels or []), "remove": list(remove_labels or [])}))
    if title is not None:
        made.append(("title", title))
    if comment is not None:
        made.append(("comment", comment))
    if close is not None:
        if close not in CLOSE_REASONS:
            raise SystemExit(f"--close is one of: {', '.join(CLOSE_REASONS)}")
        made.append(("state", close))
    if not made:
        raise SystemExit("Nothing to suggest: give at least one of --milestone, --priority, "
                         "--effort, --add-label, --remove-label, --title, --comment, --close.")
    return [{"issue": issue, "kind": kind, "value": value, "reason": reason, "source": source}
            for kind, value in made]


BRIEF = """\
You are helping a person triage GitHub issues. They are looking at a board (`milestones
board`) of one repository's open issues and pull requests, a column per milestone. You do not
change issues yourself: you propose changes with `milestones suggest`, each one appears on
their board with your reason beside it, and they accept or reject it. Nothing you suggest is
written to GitHub until they accept.

How to work on an issue they send you
- Read the issue and its thread (`gh issue view N --comments`), and the code it is about.
  You have the repository; the issue alone rarely says enough.
- They will usually say what they think ("medium priority, medium effort, because ...").
  Check that against the code. If they have misread the issue, or the work is bigger or
  riskier than they think, or it duplicates or depends on another issue, say so plainly and
  say why, before suggesting anything. Disagreeing usefully is the job.
- When you and they agree, or when you are confident, suggest. One command can carry several
  changes; each becomes its own suggestion, so they can take some and leave others.
- Keep --reason to a sentence or two that would make sense to them next month: what you found,
  with a file or an issue number where it helps.

The command
  milestones suggest REF [what to change] --reason "..." [--source NAME]

  REF is OWNER/NAME#N, an issue URL, or just N from inside the repository's checkout.
    --milestone TITLE         move it there ("none" takes it off its milestone)
    --priority P              %(priority)s, or "none" to clear
    --effort E                %(effort)s, or "none" to clear
    --add-label L             repeatable
    --remove-label L          repeatable
    --title "..."             retitle, when the title misdescribes the issue
    --comment "..."           post a comment (--comment-file PATH for a long one)
    --close completed|not_planned
    --source NAME             who is suggesting: your model's name. Default: $MILESTONES_SOURCE,
                              else "agent". Use it, so two sessions' views can be told apart.

  milestones suggest --json < file    a batch: a JSON list of
                                      {"ref": "...", "kind": ..., "value": ..., "reason": ...}

What the milestones mean in this repository
%(buckets)s
- Any other milestone is a release or a deadline: an issue belongs there only if it is needed
  for that release.

Rules
- Suggest a milestone only when the right one is clear; otherwise say what you found in the
  reason of another suggestion or in a comment suggestion, and leave the milestone alone.
- Don't suggest moving an issue onto or off "Needs triage": that bucket is a person's call.
- Priority is how much it matters; effort is how much work it is. Judge effort from the code,
  not from the length of the issue. Both are labels, which --priority and --effort set: don't
  suggest one with --add-label.
%(scales)s
- Suggest closing only what is done, duplicated or will not be done, and say which.
"""

# MILESTONES.md's table, word for word: `test_brief_buckets_match_milestones_md` keeps the two
# in step, so an agent is never told something the conventions don't say.
BUCKET_MEANINGS = {
    "Critical": "Must go into the next release, whatever else happens.",
    "Needed soon": "Not being worked on now, but should be next.",
    "Needed later": "Wanted, but can wait until the soon work is done.",
    "Not urgent": "Worth doing some day. Nobody is waiting on it.",
    "Upstream": "Belongs to an upstream data or code source. Finalise it here, then report it there.",
    "Needs triage": "Seen, but can't be placed yet: needs a reproduction, tests, or an "
                    "investigation first.",
}


def brief(buckets: list[str]) -> str:
    """What a coding agent needs to know to be useful at a board, as text to hand it."""
    return BRIEF % {
        **{kind: ", ".join(spec["options"]) for kind, spec in reconcile.KINDS.items()},
        # LABELS.md's tables, which `test_labels_md_matches_the_label_table` holds to this.
        "scales": "\n".join(f"  - {spec['field']} {option}: {spec['meanings'][option]}"
                            for spec in reconcile.KINDS.values() for option in spec["options"]),
        "buckets": "\n".join(f"- {b}: {BUCKET_MEANINGS.get(b, 'A standing bucket of this repository.')}"
                             for b in buckets),
    }
