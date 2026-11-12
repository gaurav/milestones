"""A read-only session's changes, kept as a plan.

With MILESTONES_PERMISSIONS=read nothing is written to GitHub, so what you do on the board
would be lost when it stops. Instead it is saved here as it is made: the net change to each
card, in a file a person or a coding agent can read. A read-only board picks it up again on its
next start, and a writable one offers it back as suggestions to accept or reject."""

import json


def net(actions: list[dict]) -> list[dict]:
    """What a session's unwritten actions come to: one entry per card and kind, from what
    the card held before the first to what the last left it with — and nothing at all for a
    change that was undone. Comments don't replace each other, so each stands."""
    spans: dict[tuple, dict] = {}
    comments = []
    for action in actions:
        if action["status"] != "would":
            continue
        if action["kind"] == "comment":
            comments.append({k: action[k] for k in ("issue", "kind", "value", "previous")})
            continue
        key = (action["issue"], action["kind"])
        if key in spans:
            spans[key]["value"] = action["value"]
        else:
            spans[key] = {k: action[k] for k in ("issue", "kind", "value", "previous")}
    return [e for e in spans.values() if e["value"] != e["previous"]] + comments


def show(kind: str, value, titles: dict[int, str]) -> str:
    """A value in words: a milestone by its title, labels as a list, nothing as "none"."""
    if kind == "milestone":
        return titles.get(value, f"milestone {value}") if value is not None else "none"
    if kind == "labels":
        return ", ".join(value) if value else "none"
    if kind == "state":
        return f"closed as {value}" if value else "open"
    return "none" if value is None else str(value)


def summary(entry: dict, titles: dict[int, str]) -> str:
    """One line a person can read: "#62: milestone → Needed soon (was none)"."""
    if entry["kind"] == "comment":
        return f"#{entry['issue']}: comment: {entry['value']}"
    return (f"#{entry['issue']}: {entry['kind']} → {show(entry['kind'], entry['value'], titles)} "
            f"(was {show(entry['kind'], entry.get('previous'), titles)})")


def document(repo: str, entries: list[dict], cards: dict[int, dict], titles: dict[int, str],
             saved: str) -> dict:
    """The plan file: the entries, each with the issue's title and a line saying what it
    does, so that it reads without the board."""
    return {
        "about": "Changes made on a read-only milestones board. Nothing here has been "
                 "written to GitHub. Start a writable board for this repo to review them.",
        "repo": repo,
        "saved": saved,
        "changes": [{**entry, "title": cards.get(entry["issue"], {}).get("title", ""),
                     "summary": summary(entry, titles)} for entry in entries],
    }


def read(path) -> dict:
    """The plan at `path`, or an empty one: a missing or unreadable file is no plan."""
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"changes": []}
    changes = found.get("changes") if isinstance(found, dict) else None
    ok = isinstance(changes, list) and all(
        isinstance(c, dict) and {"issue", "kind", "value"} <= set(c) for c in changes)
    return {**found, "changes": changes} if ok else {"changes": []}


def as_suggestion(entry: dict, current, saved: str, titles: dict[int, str]) -> dict | None:
    """A planned change as a suggestion for a writable board, given what the card holds now.
    None when the card already has it. When the card has moved on since the plan was made,
    the reason says so, since the plan was made looking at something else."""
    if entry["kind"] != "comment" and current == entry["value"]:
        return None
    value = entry["value"]
    if entry["kind"] == "labels":
        # As a change, so that labels added since the plan are not swept away with it.
        before = entry.get("previous") or []
        value = {"add": [n for n in entry["value"] if n not in before],
                 "remove": [n for n in before if n not in entry["value"]]}
    reason = f"Planned in a read-only session{f' on {saved[:10]}' if saved else ''}."
    if entry["kind"] != "comment" and "previous" in entry and current != entry["previous"]:
        reason += (f" It was {show(entry['kind'], entry['previous'], titles)} then and is "
                   f"{show(entry['kind'], current, titles)} now, so "
                   "check it still makes sense.")
    return {"issue": entry["issue"], "kind": entry["kind"], "value": value,
            "reason": reason, "source": "plan"}
