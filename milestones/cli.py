"""milestones: view and manage GitHub milestones across all my repositories."""

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import termios
import textwrap
import tty
import webbrowser
from urllib.parse import quote
from collections import Counter
from itertools import cycle
from pathlib import Path

from . import env, gh
# The table of priority and effort labels: pure, and the board's as much as the CLI's.
from .board import reconcile
from .board.server import DEFAULT_PORT
# The config, the conventions and the cross-repo reads, which the web app uses too.
from .core import (  # noqa: F401 — re-exported: tests and older callers import them from here
    COLOR_NAMES, DATE_RE, DEFAULT_BUCKETS, EXAMPLE_CONFIG, ISSUE_RE, KINDS, OPTIONAL_BUCKETS,
    REPO_RE, TRIAGE_BUCKET, VERSION_RE, _milestones_by_title, _norm_issue, after,
    build_search_queries, collect_findings, config_path, fetch_milestones, fetch_untriaged,
    free_buckets, is_focused, is_ignored, load_config, milestone_problems, parse_color,
    parse_issue_ref, parse_repo, pr_group, prs_data, prs_query, repo_url, rollover_move,
    rollover_plan, sort_key, standard_labels, status_data, triage_order, untriaged_counts,
)


_repo_labels: dict[str, list[dict]] = {}


def repo_labels(repo: str) -> list[dict]:
    """A repo's labels, read once a run."""
    if repo not in _repo_labels:
        _repo_labels[repo] = gh.api(f"repos/{repo}/labels?per_page=100", paginate=True)
    return _repo_labels[repo]


def set_label_value(repo: str, number: int, kind: str, value: str, labels: list[str]) -> None:
    """Put an issue's or a pull request's priority or effort where it is kept: the standard
    label, in place of whatever labels said it before, made in its colour if the repo
    hasn't got it. `labels` is what the item carries now."""
    old = reconcile.labels_about(kind, {"labels": [{"name": n} for n in labels]})
    wanted = [n for n in labels if n not in old] + [reconcile.standard_label(kind, value)]
    for body in reconcile.missing_standard(wanted, [l["name"] for l in repo_labels(repo)]):
        repo_labels(repo).append(gh.api(f"repos/{repo}/labels", method="POST", body=body))
    gh.api(f"repos/{repo}/issues/{number}/labels", method="PUT", body={"labels": wanted})


def label_plan(existing: list[dict]) -> tuple[list[tuple[str, dict]], list[dict], list[tuple[str, str, str]]]:
    """What `setup` does to a repo's labels so the standard ones are all there, from the
    labels it has: `(renames, creates, recolours)`. A standard label that is missing takes
    over an old one that means the same (`Size: M` becomes `Effort: M`, which keeps it on
    every issue) and is created otherwise; one that is there in another colour is listed
    with (name, its colour, the standard one). A rename keeps the old label's colour and its
    description, if it has one — somebody chose them — so a renamed label in another colour
    is listed for recolouring too, under its new name."""
    by_name = {l["name"].lower(): l for l in existing}
    renames, creates, recolours = [], [], []
    for kind, spec in reconcile.KINDS.items():
        for option in spec["options"]:
            name = reconcile.standard_label(kind, option)
            body = {"new_name": name, "color": spec["colors"][option],
                    "description": spec["meanings"][option]}
            have = by_name.get(name.lower())
            if have:
                if have["color"].lower() != spec["colors"][option]:
                    recolours.append((have["name"], have["color"], spec["colors"][option]))
                continue
            old = [l for l in existing if reconcile.label_value(kind, l["name"]) == option]
            if old:
                keeps = {} if old[0].get("description") else {"description": body["description"]}
                renames.append((old[0]["name"], {"new_name": name, **keeps}))
                if old[0]["color"].lower() != body["color"]:
                    recolours.append((name, old[0]["color"], body["color"]))
            else:
                creates.append({"name": name, **{k: body[k] for k in ("color", "description")}})
    return renames, creates, recolours


def parse_ignore(text: str) -> str:
    """One ignore-list entry: a repo, or `OWNER/*` for everything under one owner.

    A bare owner means the same as `OWNER/*`, which is what you can actually type —
    an unquoted `owner/*` is a "no matches found" error in zsh. The config keeps the
    explicit form, where a bare name would read like a repo someone mistyped.
    """
    owner, _, name = text.strip().rstrip("/").partition("/")
    if owner and name in ("", "*"):
        return f"{owner}/*"
    return parse_repo(text)


def write_repo_list(path: Path, key: str, repos: list[str]) -> None:
    """Rewrite just one OWNER/NAME list, leaving the rest of the config alone."""
    # ponytail: comments *inside* the list are dropped; nothing else is touched.
    block = f"{key} = [\n" + "".join(f'  "{r}",\n' for r in repos) + "]"
    # A literal replacement would have its backslashes interpreted; a lambda hands the
    # block over as-is.
    text, count = re.subn(rf"^{key}\s*=\s*\[[^\]]*\]", lambda _: block, path.read_text(),
                          count=1, flags=re.M)
    if count != 1:
        # `ignore` is optional, so not finding it can mean it was never written — but it
        # can also mean the list is there in a shape the pattern can't rewrite, and
        # appending a second one would then be silent corruption. `repos` is required by
        # load_config, so it only ever takes the second branch.
        if re.search(rf"^{key}\s*=", text, re.M):
            sys.exit(f"Can't find a `{key} = [...]` list to edit in {path}; edit it by hand.")
        # A top-level key has to go above the first `[table]` header, not at the end of the
        # file: everything after a header belongs to that table, so appending `ignore` under
        # `[colors]` would quietly turn it into `colors.ignore`. Above the comment block
        # that introduces the table, too — landing between a comment and the thing it
        # describes leaves the comment looking like it explains the new key.
        header = re.search(r"(?:^[ \t]*#[^\n]*\n)*^\[", text, re.M)
        at = header.start() if header else len(text)
        text = f"{text[:at].rstrip(chr(10))}\n\n{block}\n\n{text[at:].lstrip(chr(10))}".rstrip(
            "\n") + "\n"
    path.write_text(text)


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")


# A walk answer: a menu number or `c`, then at most one modifier — "2", "12!", "c-".
ANSWER_RE = re.compile(r"(\d+|c)([\^$!+-]?)")

# What each modifier means. The order ones are parsed so the grammar is settled, but
# GitHub has no API that writes a milestone's manual order, so they only say so.
PRIORITY_KEYS = {"!": "Urgent", "+": "High", "-": "Low"}
ORDER_KEYS = {"^": "top", "$": "bottom"}


def parse_answer(text: str) -> tuple[str, str] | None:
    """(choice, suffix) from a walk answer, or None for anything that isn't an assignment."""
    match = ANSWER_RE.fullmatch(text.strip().lower())
    return (match[1], match[2]) if match else None


def complete_titles(titles: list[str], text: str) -> list[str]:
    """The titles that start with what has been typed so far, ignoring case, in order."""
    return [t for t in titles if t.lower().startswith(text.lower())]


def ask_completing(prompt: str, titles: list[str]) -> str:
    """`ask`, with Tab completing over `titles` for as long as the prompt is up.

    readline only takes effect on a terminal; a piped answer is read as it is.
    """
    try:
        import readline
    except ImportError:  # pragma: no cover — no readline, no completion, same prompt
        return ask(prompt)
    matches: list[str] = []

    def completer(text, state):
        nonlocal matches
        if state == 0:
            matches = complete_titles(titles, readline.get_line_buffer())
        # Completing the whole line: a title has spaces, and readline would otherwise
        # treat "Needed" and "soon" as two words.
        return matches[state] if state < len(matches) else None

    saved = readline.get_completer()
    saved_delims = readline.get_completer_delims()
    readline.set_completer_delims("")
    readline.set_completer(completer)
    # macOS ships libedit under the readline name, and it wants its own binding.
    readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "")
                            else "tab: complete")
    try:
        return ask(prompt)
    finally:
        readline.set_completer(saved)
        readline.set_completer_delims(saved_delims)


def owners_of(repos: list[str]) -> list[str]:
    return sorted({r.split("/")[0] for r in repos})


def excerpt(body: str | None, width: int = 200) -> str:
    return textwrap.shorten(body or "", width, placeholder=" …")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Colour is for a person reading a terminal; a pipe, a file or NO_COLOR gets plain text.
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# Whole SGR parameters, not bare colour numbers: `38;5;N` is 256-colour foreground N, where
# a bare N would be one of the sixteen ANSI codes and mean something else entirely (39 is
# "default foreground", 44 is a blue *background*). Bold is plain `1`.
def fg(n: int) -> str:
    return f"38;5;{n}"


# How late a milestone is: red, orange, yellow, then grey once it is far enough out to stop
# being news.
LATE, SOON, AHEAD, DISTANT = (fg(n) for n in (196, 208, 226, 244))

# How far along it is, as an upward-biased green ramp: grey until halfway, then lightening
# green. Being at 10% is not bad news — it is a milestone somebody just filed — so nothing
# down there is coloured as a warning; the ramp is there to pick out the ones near the end.
PCT_SCALE = ((50, 244), (65, 151), (80, 114), (95, 77), (101, 46))

# A standing bucket's title — coloured only while it holds work, so an empty "Needed soon"
# doesn't look like an alarm. The urgency levels run down the same ramp as due dates, red to
# orange to green, and then grey for work nobody is waiting on. The other two are states, not
# levels, so they sit off that ramp: teal for work that is someone else's to finish, magenta
# for work nobody has been able to place yet.
BUCKET_COLORS = {"Critical": "1;" + LATE, "Needed soon": SOON, "Needed later": fg(151),
                 "Not urgent": DISTANT, "Upstream": fg(74), TRIAGE_BUCKET: fg(170)}


# The cycle for owners the config says nothing about.
ORG_PALETTE = [fg(COLOR_NAMES[n]) for n in ("blue", "magenta", "cyan", "violet", "indigo",
                                            "rose", "teal", "purple")]


def visible(cell) -> int:
    """Printed width of a cell: escape sequences take no columns."""
    return len(ANSI_RE.sub("", str(cell)))


def paint(text: str, code: str | None) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if code and COLOR else text


# U+2726 BLACK FOUR POINTED STAR, gold. A Dingbats star rather than the obvious U+2605:
# that one is East Asian Ambiguous, so a CJK-configured terminal draws it two columns wide
# and knocks the row out of line. This one is Neutral width, and is not in the emoji set
# either, so no font can decide to render it as a double-width colour glyph. Keep both
# properties if you ever swap it — test_focus_marker_is_one_column_wide checks the first.
FOCUS_MARK = "\u2726"
FOCUS_COLOR = "1;" + fg(COLOR_NAMES["gold"])


def star(focused: bool) -> str:
    """The focus marker for a row, in a column of its own so the names stay aligned.

    The glyph carries the meaning and the colour only makes it easier to find, so a pipe
    or NO_COLOR loses nothing.
    """
    return paint(FOCUS_MARK, FOCUS_COLOR) if focused else ""


def org_colors(repos: list[str], configured: dict[str, str] | None = None) -> dict[str, str]:
    """A colour for each owner the config names, and for each one that owns more than one
    of these repos.

    Colouring a one-off owner would say "these rows go together" about a single row — but
    a colour someone has chosen by hand is worth honouring either way, and two owners can
    share one to tie sibling organisations together.

    The rest are assigned in alphabetical order rather than in the order the rows happen to
    arrive: a caller sorts its rows by due date, so first-appearance order would repaint an
    owner every time a milestone came or went. Adding an owner to the config can still shift
    the ones after it, which is at least something you did on purpose.
    """
    configured = {o.lower(): c for o, c in (configured or {}).items()}
    owners = {r.split("/")[0].lower(): r.split("/")[0] for r in repos}
    counts = Counter(r.split("/")[0].lower() for r in repos)
    colors, palette = {}, cycle(ORG_PALETTE)
    for key in sorted(owners):
        if key in configured:
            colors[owners[key]] = fg(configured[key])
        elif counts[key] > 1:
            colors[owners[key]] = next(palette)
    return colors


def bucket_color(title: str, open_issues: int) -> str | None:
    return BUCKET_COLORS.get(title) if open_issues else None


def due_color(due: str | None, today: str) -> str | None:
    """Overdue, this week, this month, later — undated milestones stay plain."""
    if not due:
        return None
    days = (datetime.date.fromisoformat(due) - datetime.date.fromisoformat(today)).days
    if days < 0:
        return LATE
    return SOON if days <= 7 else AHEAD if days <= 30 else DISTANT


def pct_color(closed: int, total: int) -> str | None:
    """How far along, so nearly-finished milestones catch the eye."""
    if not total:  # nothing ever filed against it; the cell is a dash anyway
        return None
    pct = 100 * closed / total
    return next(fg(n) for limit, n in PCT_SCALE if pct < limit)


def print_table(rows: list[tuple], headers: tuple, right: tuple = ()):
    """`right` names the headers whose column is right-aligned; the rest go left.

    Padded on the visible width, so a cell may carry colour of its own — `str.ljust`
    counts escape sequences and would knock the column out of line.
    """
    widths = [max(visible(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    # A column with nothing in any row takes no space at all, header or no header, rather
    # than indenting everything past it: the flags column with nothing flagged, the focus
    # column with nothing focused, the PRS column when no milestone has any.
    keep = [i for i, width in enumerate(widths)
            if width and (not rows or any(visible(r[i]) for r in rows))]
    for row in [headers, *rows]:
        cells = []
        for i in keep:
            pad = " " * (widths[i] - visible(row[i]))
            cells.append(pad + str(row[i]) if headers[i] in right else str(row[i]) + pad)
        print("  ".join(cells).rstrip())


# --- commands ---------------------------------------------------------------


def cmd_status(config, args):
    today = datetime.date.today().isoformat()
    data = status_data(config)
    records, quiet = data["milestones"], [r["repo"] for r in data["quiet_repos"]]
    stranded, untriaged = data["stranded"], data["untriaged"]
    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        return

    orgs = org_colors([r["repo"] for r in records], config.get("colors"))
    rows = []
    for r in records:
        owner, _, name = r["repo"].partition("/")
        flags = " ".join(flag for kind, flag in (("overdue", "!OVERDUE"),
                                                 ("empty", "(empty)"), ("done", "(done)"))
                         if kind in r["flags"])
        rows.append((star(r["focus"]),
                     f"{paint(owner, orgs.get(owner))}/{name}",
                     paint(VERSION_RE.sub(lambda m: paint(m[0], "1"), r["title"]),
                           bucket_color(r["title"], r["open"] + r["open_prs"])),
                     paint(r["due"], due_color(r["due"], today)) if r["due"] else "—",
                     r["open"], r["closed"], r["open_prs"] or "",
                     paint(f"{r['percent']}%",
                           pct_color(r["closed"] + r["closed_prs"],
                                     r["open"] + r["closed"] + r["open_prs"] + r["closed_prs"]))
                     if r["percent"] is not None else "—",
                     flags, r["url"]))
    if rows:
        # PRS is open pull requests, blank for none; print_table drops the column when no
        # milestone has any.
        print_table(rows, ("", "REPO", "MILESTONE", "DUE", "OPEN", "DONE", "PRS", "%", "", "URL"),
                    right=("OPEN", "DONE", "PRS", "%"))
    if quiet:
        if rows:
            print()
        print(f"{len(quiet)} tracked repo{'s' if len(quiet) != 1 else ''} "
              f"with no open milestones:")
        print_table([(star(is_focused(r, config["focus"])), r, repo_url(r)) for r in quiet],
                    ("", "REPO", "URL"))
    elif not rows:
        print("No open milestones in any configured repo.")
    if stranded:
        # One line, not rows: `check` owns the fix, and a closed milestone is not a state
        # of the work so much as a place it has been lost.
        items = sum(s["open"] + s["open_prs"] for s in stranded)
        print(f"\n{len(stranded)} closed milestone{'s' if len(stranded) != 1 else ''} still "
              f"hold{'s' if len(stranded) == 1 else ''} {items} open "
              f"item{'s' if items != 1 else ''} — run: milestones check")
    if not untriaged:
        print("\nEverything open in every tracked repo is on a milestone.")
        return
    issues, prs = sum(r["issues"] for r in untriaged), sum(r["prs"] for r in untriaged)
    waiting = sum(r["needs_triage"] for r in untriaged)
    on_bucket = f"{waiting} sit{'s' if waiting == 1 else ''} on {TRIAGE_BUCKET}" if waiting else ""
    if not issues and not prs:
        # Placed, every one — but a parked issue is placed only in the sense that somebody
        # has looked at it, and it is still the walk's to finish.
        print(f"\nEverything open in every tracked repo is on a milestone, but {on_bucket}:")
    else:
        print(f"\n{issues} untriaged issue{'s' if issues != 1 else ''} and {prs} pull "
              f"request{'s' if prs != 1 else ''} (no milestone)"
              + (f", and {on_bucket}" if waiting else "") + ":")
    # A zero prints as blank so a column reads as "which repos have PRs waiting", and a
    # column nobody has anything in is dropped altogether.
    print_table([(star(r["focus"]), r["repo"], r["issues"] or "", r["prs"] or "",
                  r["needs_triage"] or "") for r in untriaged],
                ("", "REPO", "ISSUES", "PRS", "NEEDS TRIAGE"),
                right=("ISSUES", "PRS", "NEEDS TRIAGE"))
    print("run: milestones triage --prs" + ("  (then: milestones triage)" if issues else "")
          if prs else "run: milestones triage")


def _ensure_bucket(repo: str, title: str, existing: dict[str, dict]) -> dict:
    """The open milestone called `title`, creating or reopening it as needed."""
    milestone = existing.get(title)
    if gh.read_only() and (milestone is None or milestone["state"] != "open"):
        # No number: nothing can be put on a milestone that wasn't made.
        print(f"would {'create' if milestone is None else 'reopen'}: {title}")
        return {"title": title, "number": None, "html_url": "", "due_on": None,
                "open_issues": 0}
    if milestone is None:
        milestone = gh.api(f"repos/{repo}/milestones", method="POST", title=title)
        print(f"created:  {title}")
    elif milestone["state"] != "open":
        # A closed bucket is invisible to status and the triage menu, so
        # "it exists" is not good enough for a repair command.
        milestone = gh.api(f"repos/{repo}/milestones/{milestone['number']}", method="PATCH",
                           state="open")
        print(f"reopened: {title}")
    else:
        print(f"exists:   {title}")
    return milestone


def cmd_setup(config, args):
    existing = _milestones_by_title(args.repo)
    for bucket in config["buckets"]:
        # An optional bucket waits until triage needs it, unless it was made once already.
        if bucket in OPTIONAL_BUCKETS and bucket not in existing:
            print(f"later:    {bucket} (made the first time triage picks it)")
            continue
        _ensure_bucket(args.repo, bucket, existing)
    renames, creates, recolours = label_plan(repo_labels(args.repo))
    labels = f"repos/{args.repo}/labels"
    if gh.read_only():
        for old, body in renames:
            print(f"would rename label: {old} → {body['new_name']}")
        for body in creates:
            print(f"would create label: {body['name']}")
        for name, colour, standard in recolours:
            print(f"would offer to recolour: {name} (#{colour} → #{standard})")
        return
    for body in creates:
        gh.api(labels, method="POST", body=body)
        print(f"created label:  {body['name']}")
    # A rename changes what every issue carrying the label shows, and a colour is somebody's
    # choice: both are asked about, where a new label is nobody's business yet.
    declined = set()
    for old, body in renames:
        if ask(f"Rename the label '{old}' to '{body['new_name']}' in {args.repo}, on every "
               "issue that has it? [y/N] ").strip().lower() == "y":
            gh.api(f"{labels}/{quote(old)}", method="PATCH", body=body)
            print(f"renamed label:  {old} → {body['new_name']}")
        else:
            declined.add(body["new_name"])
    for name, colour, standard in recolours:
        if name in declined:
            continue  # there is no label of that name to recolour
        if ask(f"Recolour '{name}' from #{colour} to the standard #{standard}? "
               "[y/N] ").strip().lower() == "y":
            gh.api(f"{labels}/{quote(name)}", method="PATCH", body={"color": standard})
            print(f"recoloured:     {name}")


def cmd_rollover(config, args):
    src, dst, items = rollover_plan(args.repo, args.src, args.dst, args.close, config["buckets"])
    if items:
        for item in items:
            print(f"  #{item['number']} {item['title']}"
                  f"{' (PR)' if 'pull_request' in item else ''}")
        if gh.read_only():
            print(f"read-only: would move these {len(items)} from '{args.src}' to '{args.dst}'"
                  + (f", and close '{args.src}'" if args.close else "") + ". Nothing written.")
            return
        answer = ask(f"Move {len(items)} open issues and PRs from '{args.src}' to '{args.dst}' "
                     f"in {args.repo}? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Aborted.")
    else:
        print(f"Nothing open in '{args.src}'.")
    rollover_move(args.repo, src, dst, items, args.close,
                  moved=lambda item: print(f"moved #{item['number']}"))
    if args.close:
        print(f"closed '{args.src}'")


def item_marker(item: dict) -> str:
    """"(PR, draft)", "(PR)", or nothing for an issue — what a walk or a list shows after the ref."""
    if not item["pr"]:
        return ""
    return "(PR, draft)" if item["draft"] else "(PR)"


def triage_sources(config: dict, given: list[str] | None) -> list[str | None]:
    """Where a triage walk takes its items from, in walk order: milestone titles, with None
    for "no milestone". By default that is the items with no milestone and then, where the
    config has the bucket, the ones parked on Needs triage. `--from` replaces the default
    with exactly what was asked for; its literal `none` is the no-milestone source."""
    if given is None:
        return [None] + ([TRIAGE_BUCKET] if TRIAGE_BUCKET in config["buckets"] else [])
    sources = list(dict.fromkeys(None if t.lower() == "none" else t for t in given))
    quoted = [t for t in sources if t and '"' in t]
    if quoted:
        # The search qualifier is milestone:"title", and nothing escapes a quote inside it.
        sys.exit(f"Can't search for a milestone with a quote in its title: {', '.join(quoted)}")
    return sources


def fetch_closing_issues(items: list[dict]) -> None:
    """Give each pull request in `items` a `closes` list: the issues its body closes, as
    {id, number, title, state, milestone, repo}, milestone being a title or None and repo
    the issue's own, since a PR can close an issue in another repository.

    One aliased GraphQL round trip per fifty PRs, whatever repos they span. A PR that has
    vanished since the search comes back null with a partial error; it gets no `closes`.
    The first hundred closing issues are fetched, with a warning for a PR that has more.
    An item marked `pr` that turns out to be an issue — `assign` can't tell from a ref —
    gets an empty `closes`, since `issueOrPullRequest` answers for either.
    """
    prs = [i for i in items if i["pr"]]
    for start in range(0, len(prs), 50):
        batch = prs[start:start + 50]
        by_repo: dict[str, list[dict]] = {}
        for item in batch:
            by_repo.setdefault(item["repo"], []).append(item)
        aliases = " ".join(
            f'r{ri}: repository(owner: "{repo.split("/")[0]}", name: "{repo.split("/")[1]}") {{ '
            + " ".join(f"p{item['number']}: issueOrPullRequest(number: {item['number']}) {{ "
                       "... on PullRequest { closingIssuesReferences(first: 100) { totalCount "
                       "nodes { id number title state milestone { title } "
                       "repository { nameWithOwner } } } } }"
                       for item in repo_items)
            + " }"
            for ri, (repo, repo_items) in enumerate(by_repo.items()))
        data = gh.graphql(f"query {{ {aliases} }}")
        for ri, (repo, repo_items) in enumerate(by_repo.items()):
            node = data.get(f"r{ri}") or {}
            for item in repo_items:
                refs = (node.get(f"p{item['number']}") or {}).get("closingIssuesReferences")
                if refs and refs["totalCount"] > len(refs["nodes"]):
                    print(f"warning: {item['repo']}#{item['number']} closes "
                          f"{refs['totalCount']} issues; only the first {len(refs['nodes'])} "
                          "are shown or carried", file=sys.stderr)
                item["closes"] = [
                    {"id": c["id"], "number": c["number"], "title": c["title"],
                     "state": c["state"], "milestone": c["milestone"] and c["milestone"]["title"],
                     "repo": c["repository"]["nameWithOwner"]}
                    for c in refs["nodes"]] if refs else []


def carries(closed: dict, repo: str, parked: str | None = None) -> bool:
    """Whether a PR in `repo` takes this issue it closes onto its own milestone: open, on no
    milestone yet — or on `parked`, the bucket that means nobody could place it — and in the
    same repo, since milestone numbers are per repo, so one elsewhere is shown but left
    alone."""
    return (closed["state"] == "OPEN"
            and (not closed["milestone"] or closed["milestone"] == parked)
            and closed["repo"].lower() == repo.lower())


def closes_line(closes: list[dict], repo: str) -> str:
    """"closes #12 (no milestone), #7 (v1.2), #3 (closed)" for a PR in `repo`, with an issue
    elsewhere given its repo, "o/other#5 (…)"; nothing for a PR closing none."""
    if not closes:
        return ""
    def where(c):
        if c["state"] != "OPEN":
            return "closed"
        return c["milestone"] or "no milestone"
    def ref(c):
        same = c["repo"].lower() == repo.lower()
        return f"#{c['number']}" if same else f"{c['repo']}#{c['number']}"
    return "closes " + ", ".join(f"{ref(c)} ({where(c)})" for c in closes)


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def triage_scope(items: list[dict], noun: str, repo: str | None, tracked: int,
                 sources: list[str | None] = (None,)) -> tuple[str, str]:
    """What a walk covers, as (the phrase the summary repeats, the per-repo counts only the
    opening line adds): ("11 PRs without a milestone in https://github.com/a/b", ""). With
    several sources, each is counted in turn — "7 issues without a milestone and none on
    Needs triage" — so a source that turned up nothing is still seen to have been asked."""
    def where(source):
        return "without a milestone" if source is None else f"on {source}"
    if not items:
        what = f"no {noun}s " + " or ".join(where(s) for s in sources)
    else:
        counts = Counter(i.get("source") for i in items)
        parts = [f"{plural(counts[s], noun) if counts[s] else 'no ' + noun + 's'} {where(s)}"
                 if k == 0 else f"{counts[s] or 'none'} {where(s)}"
                 for k, s in enumerate(sources)]
        what = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    if repo:
        return f"{what} in {repo_url(repo)}", ""
    counts = Counter(i["repo"] for i in items)  # first-seen order, which is the walk's
    if not counts:
        return f"{what} in {plural(tracked, 'tracked repo')}", ""
    return (f"{what} in {len(counts)} of {plural(tracked, 'tracked repo')}",
            ", ".join(f"{r} ({c})" for r, c in counts.items()))


def by_title(entries: list[tuple[str, str]]) -> str:
    """" to Needed soon" for (ref, title) pairs on one milestone, ": 4 to Needed soon, 2 to
    v1.2" for several, most first — what follows "Assigned 6"."""
    counts = Counter(title for _, title in entries).most_common()
    if len(counts) == 1:
        return f" to {counts[0][0]}"
    return ": " + ", ".join(f"{c} to {t}" for t, c in counts)


def triage_summary(scope: str, total: int, done: dict[str, list], buckets: list[str],
                   rerun: str, still: str = "without a milestone") -> list[str]:
    """The lines a walk ends on, however it ends: what it covered, what it wrote, and what
    is still to do. `done` is the walk's log, a list per kind of thing that happened;
    `still` is what the items not dealt with are, once a walk reads from a milestone too."""
    lines = [f"Of {scope}:"]
    assigned = done["assigned"]
    lines.append(f"Assigned {len(assigned)}{by_title(assigned)}" if assigned else "Assigned none")
    if done["carried"]:
        lines.append(f"Also assigned {plural(len(done['carried']), 'issue')} they close"
                     f"{by_title(done['carried'])}")
    if done["passed"]:
        lines.append(f"Passed over {len(done['passed'])} already assigned with the PR "
                     "that closes them")
    if done["priority"]:
        counts = Counter(name for _, name in done["priority"])
        lines.append(f"Set priority on {len(done['priority'])}: " + ", ".join(
            f"{counts[name]} {name}" for name in PRIORITY_KEYS.values() if name in counts))
    shown = 5
    if done["skipped"]:
        refs = done["skipped"]
        lines.append(f"Skipped {len(refs)}: " + ", ".join(refs[:shown])
                     + (f" and {len(refs) - shown} more" if len(refs) > shown else ""))
    decided = len(assigned) + len(done["skipped"]) + len(done["passed"])
    if decided < total:
        lines.append(f"Stopped at {decided + 1} of {total}, "
                     f"with {total - decided} left undecided")
    for repo, title in done["created"]:
        # A bucket needs no date; anything else created here will be `check`'s `undated`.
        undated = "" if title in buckets else " (undated: `milestones check` will ask for a date)"
        lines.append(f"Created {title} in {repo}{undated}")
    for repo, title in done["reopened"]:
        lines.append(f"Reopened {title} in {repo}")
    for (title, url, where), count in Counter(done["unordered"]).items():
        lines.append(f"Not moved {count} to the {where} of {title}: GitHub has no API for "
                     f"that (gaurav/milestones#25), so drag {'it' if count == 1 else 'them'} "
                     f"at {url}")
    lines += [f"Priority failed on {ref}: {message}" for ref, message in done["failed"]]
    left = total - len(assigned) - len(done["passed"])
    if left:
        # Listings lag writes by up to ~10s, so a rerun straight away can offer again
        # what this one just assigned.
        lag = ", after ~10s for GitHub to catch up" if assigned else ""
        lines.append(f"{left} still {still}: `{rerun}` walks them again{lag}")
    return [lines[0]] + [f"  - {line}" for line in lines[1:]]


def cmd_triage(config, args):
    sources = triage_sources(config, args.sources)
    issues = fetch_untriaged(config, args.repo, "pr" if args.prs else "issue", sources)
    if args.prs:
        fetch_closing_issues(issues)
    if args.json:
        json.dump({"issues": issues}, sys.stdout, indent=2)
        print()
        return
    if args.list:
        # One ref per line, first, so a line can be piped straight into `assign`.
        for i in issues:
            marker = f"  {item_marker(i)}" if i["pr"] else ""
            # Only an item that has a milestone says so, so a no-milestone line is what it
            # always was for whatever is parsing it downstream.
            on = f"  (on {i['milestone']})" if i["milestone"] else ""
            labels = f"  [{', '.join(i['labels'])}]" if i["labels"] else ""
            closes = f"  {closes_line(i['closes'], i['repo'])}" if i.get("closes") else ""
            print(f"{i['repo']}#{i['number']}  {i['title']}{marker}{on}{labels}{closes}")
        return

    noun = "PR" if args.prs else "issue"
    scope, per_repo = triage_scope(issues, noun, args.repo, len(config["repos"]), sources)
    if not issues:
        print(f"Nothing to triage: {scope}.")
        return
    print(f"Found {scope}" + (f": {per_repo}" if per_repo else ""))
    rerun = "milestones triage" + (" --prs" if args.prs else "") + (
        f" --repo {args.repo}" if args.repo else "") + (
        "".join(f" --from {shlex.quote(s or 'none')}" for s in sources)
        if args.sources is not None else "")
    # The one milestone an issue a PR closes may be moved off along with the PR: parked
    # there means nobody could place it, which the PR now does.
    parked = TRIAGE_BUCKET if TRIAGE_BUCKET in config["buckets"] else None
    # Everything the walk does, for the summary it ends on: (ref, milestone title) for
    # "assigned", "carried" and "passed", (ref, priority name) for "priority", (ref, why)
    # for "failed", (repo, title) for "created" and "reopened", and
    # (title, url, "top" or "bottom") for "unordered"; "skipped" is bare refs.
    done: dict[str, list] = {key: [] for key in (
        "assigned", "carried", "passed", "priority", "failed", "skipped",
        "created", "reopened", "unordered")}
    menus: dict[str, list[dict]] = {}
    known: dict[str, dict[str, dict]] = {}  # repo -> every milestone by title, open or closed
    # (repo, number) -> (milestone title, PR number) for issues a PR carried with it this
    # run: the issue walk fetched its list before those writes, so it would offer them again.
    carried: dict[tuple[str, int], tuple[str, int]] = {}

    def set_priorities(issue: dict, key: str) -> None:
        """Priority is a label, so the item itself takes it, issue or pull request."""
        name = PRIORITY_KEYS[key]
        ref = f"{issue['repo']}#{issue['number']}"
        try:
            set_label_value(issue["repo"], issue["number"], "priority", name, issue["labels"])
        except SystemExit as exit:
            print(f"  {exit}")
            # Its first line: the rest is gh's stderr, already printed above.
            done["failed"].append((ref, str(exit).splitlines()[0]))
            return
        print(f"  → priority {name}")
        done["priority"].append((ref, name))

    def custom(repo: str, choices: list[dict]) -> dict | None:
        """A milestone typed by name: an existing one, a closed one reopened, or a new one
        created — each after asking — and on this repo's menu for the rest of the run."""
        if repo not in known:
            known[repo] = _milestones_by_title(repo)
        title = ask_completing("  milestone (Tab completes, blank to skip): ",
                               sorted(known[repo], key=str.lower)).strip()
        if not title:
            return None
        milestone = known[repo].get(title)
        if milestone is None:
            if ask(f"  no milestone '{title}' in {repo}; create it? [y/N] ").strip().lower() != "y":
                return None
            milestone = gh.api(f"repos/{repo}/milestones", method="POST", title=title)
            # A bucket is undated by design; anything else will be `check`'s `undated`.
            print(f"  created:  {title}" + ("" if title in config["buckets"]
                                            else "  (undated — check will say so)"))
            done["created"].append((repo, title))
        elif milestone["state"] != "open":
            if ask(f"  '{title}' is closed; reopen it? [y/N] ").strip().lower() != "y":
                return None
            milestone = gh.api(f"repos/{repo}/milestones/{milestone['number']}",
                               method="PATCH", state="open")
            print(f"  reopened: {title}")
            done["reopened"].append((repo, title))
        known[repo][title] = milestone
        for i, m in enumerate(choices):
            if m["title"] == title:
                choices[i] = milestone  # a (new) placeholder, or the same one re-picked
                break
        else:
            choices.append(milestone)
        return milestone
    # try/finally, so the summary comes however the walk ends: q, ^C or ^D in `ask`, or a
    # failed write — the last is when knowing what did get written matters most.
    try:
        for n, issue in enumerate(issues, 1):
            repo = issue["repo"]
            if (repo, issue["number"]) in carried:
                title, pr = carried[repo, issue["number"]]
                print(f"\n[{n}/{len(issues)}] {repo}#{issue['number']}  already on {title}, "
                      f"with PR #{pr} that closes it")
                done["passed"].append((f"{repo}#{issue['number']}", title))
                continue
            if repo not in menus:
                menus[repo] = sorted(
                    _milestones_by_title(repo, state="open").values(),
                    key=lambda m: sort_key(m["title"], m["due_on"], config["buckets"]))
                # An optional bucket the repo hasn't got yet is offered anyway, with no number
                # until it's picked and made — but only where the repo uses buckets at all.
                open_titles = {m["title"] for m in menus[repo]}
                menus[repo] += [{"title": b, "number": None}
                                for b in free_buckets(config["buckets"], open_titles)
                                if b in OPTIONAL_BUCKETS]
            choices = menus[repo]

            marker = f"  {item_marker(issue)}" if issue["pr"] else ""
            on = f"on {issue['milestone']}, " if issue["milestone"] else ""
            print(f"\n[{n}/{len(issues)}] {repo}#{issue['number']}{marker}"
                  f"  ({on}updated {issue['updated'][:10]})")
            print(f"  {issue['title']}")
            if issue.get("closes"):
                print(f"  {closes_line(issue['closes'], repo)}")
            if issue["labels"]:
                print(f"  labels: {', '.join(issue['labels'])}")
            if issue["body"]:
                print(f"  > {excerpt(issue['body'])}")
            for i, m in enumerate(choices, 1):
                if m["number"] is None:
                    print(f"  {i}) {m['title']} (new)")
                    continue
                due = f", due {m['due_on'][:10]}" if m["due_on"] else ""
                # REST open_issues counts PRs as well as issues, which is what we want here:
                # both are work sitting on that milestone.
                title = paint(m["title"], bucket_color(m["title"], m["open_issues"]))
                current = "  (current)" if m["title"] == issue["milestone"] else ""
                print(f"  {i}) {title} ({m['open_issues']} open{due}){current}")
            if not choices:
                print(f"  (no open milestones in {repo} — run: milestones setup {repo})")
            print("  priority: " + "  ".join(f"{k} {name}" for k, name in PRIORITY_KEYS.items()))

            assign = f"[1-{len(choices)}] assign, " if choices else ""
            hint = "  (add ! + - for priority)"
            while True:
                answer = ask(f"  {assign}c custom, s skip, o open, q quit:{hint} ").strip().lower()
                if answer == "q":
                    return
                if answer == "s" or (answer == "" and not choices):
                    done["skipped"].append(f"{repo}#{issue['number']}")
                    break
                if answer == "o":
                    webbrowser.open(issue["url"])
                    continue
                chosen = None
                parsed = parse_answer(answer)
                choice, suffix = parsed if parsed else ("", "")
                if choice == "c":
                    chosen = custom(repo, choices)
                    if chosen is None:
                        continue
                elif choice.isdigit() and 1 <= int(choice) <= len(choices):
                    chosen = choices[int(choice) - 1]
                    if chosen["number"] is None:
                        # Swap in the real milestone, so the next issue here sees it as one. A
                        # closed one of that name is reopened rather than duplicated.
                        existing = _milestones_by_title(repo)
                        choices[int(choice) - 1] = chosen = _ensure_bucket(
                            repo, chosen["title"], existing)
                        done["reopened" if chosen["title"] in existing else "created"].append(
                            (repo, chosen["title"]))
                if chosen is not None and chosen["title"] == issue["milestone"]:
                    # Where it already is: nothing to write, and it is still to triage.
                    print(f"  (already on {chosen['title']}; skipped)")
                    done["skipped"].append(f"{repo}#{issue['number']}")
                    break
                if chosen is not None and gh.read_only():
                    print(f"  read-only: would go on {chosen['title']}")
                    done["skipped"].append(f"{repo}#{issue['number']}")
                    break
                if chosen is not None:
                    gh.api(f"repos/{repo}/issues/{issue['number']}", method="PATCH",
                           milestone=chosen["number"])
                    print(f"  → {chosen['title']}")
                    done["assigned"].append((f"{repo}#{issue['number']}", chosen["title"]))
                    if suffix in ORDER_KEYS:
                        # Parsed on purpose, so the grammar is settled before the API exists.
                        print(f"  (not moved to the {ORDER_KEYS[suffix]}: GitHub has no API for "
                              f"a milestone's order, gaurav/milestones#25 — drag it at "
                              f"{chosen['html_url']})")
                        done["unordered"].append(
                            (chosen["title"], chosen["html_url"], ORDER_KEYS[suffix]))
                    if suffix in PRIORITY_KEYS:
                        # After the milestone, so a failed label write can't lose the assignment.
                        set_priorities(issue, suffix)
                    # A PR is triaged for the issues it closes too: an open one with no
                    # milestone goes where the PR goes, and one already placed is left alone.
                    for c in issue.get("closes") or []:
                        if carries(c, repo, parked):
                            gh.api(f"repos/{repo}/issues/{c['number']}", method="PATCH",
                                   milestone=chosen["number"])
                            print(f"  → also #{c['number']} → {chosen['title']} "
                                  "(closed by this PR)")
                            c["milestone"] = chosen["title"]
                            carried[repo, c["number"]] = (chosen["title"], issue["number"])
                            done["carried"].append((f"{repo}#{c['number']}", chosen["title"]))
                    break
                print(f"  Not one of: {assign}c, s, o, q.")
    finally:
        print()
        print("\n".join(triage_summary(
            scope, len(issues), done, config["buckets"], rerun,
            "without a milestone" if sources == [None] else "to triage")))


def cmd_assign(config, args):
    """Put issues on the milestone of one title, resolved in each of their repos."""
    if not args.refs and sys.stdin.isatty():
        sys.exit("Give OWNER/NAME#N refs, or pipe `milestones triage --list` lines in.")
    texts = args.refs or [line for line in sys.stdin.read().splitlines() if line.strip()]
    refs = list(dict.fromkeys(parse_issue_ref(t) for t in texts))  # dedupe, keep order
    by_repo: dict[str, list[int]] = {}
    for repo, number in refs:
        by_repo.setdefault(repo, []).append(number)
    # One milestones call per repo, all before any write: a mistyped repo 404s here and
    # nothing has been touched. A repo that lacks the title is skipped, not fatal — the
    # standing buckets are opt-in per repo, so a mixed pipeline is the normal case.
    targets = {}
    for repo, numbers in by_repo.items():
        milestone = _milestones_by_title(repo, state="open").get(args.milestone)
        if milestone is None:
            print(f"skipping {repo}: no open milestone '{args.milestone}' "
                  f"({len(numbers)} issue{'s' if len(numbers) != 1 else ''})")
        else:
            targets[repo] = milestone["number"]
    todo = [(r, n) for r, n in refs if r in targets]
    if not todo:
        sys.exit("Nothing to assign.")
    # --priority is the walk's ! + - for a pipeline: the `Priority:` label, on each item.
    name = args.priority and args.priority.capitalize()
    # A PR stands in for the issues it closes, as in the walk: `carries` says which go onto
    # its milestone with it. Its priority is its own: a label, which a PR carries.
    # Resolved before the prompt, so the count asked about is the count written.
    items = [{"repo": r, "number": n, "pr": True} for r, n in todo]
    fetch_closing_issues(items)
    closes = {(i["repo"], i["number"]): i["closes"] for i in items}
    carry: dict[tuple[str, int], int] = {}  # (repo, issue) -> the PR that closes it
    parked = TRIAGE_BUCKET if TRIAGE_BUCKET in config["buckets"] else None
    for (repo, number), linked in closes.items():
        for c in linked:
            if carries(c, repo, parked) and (repo, c["number"]) not in closes:
                carry.setdefault((repo, c["number"]), number)
    print(f"{len(todo)} issue{'s' if len(todo) != 1 else ''} across {len(targets)} "
          f"repo{'s' if len(targets) != 1 else ''} → '{args.milestone}'"
          + (f", and {plural(len(carry), 'issue')} their PRs close" if carry else "")
          + (f", priority {name}" if name else ""))
    # ponytail: a pipe is the confirmation — the refs were picked in fzf or listed by a
    # script, there is no tty to answer from, and a milestone is a reversible field that
    # never closes or deletes anything.
    if gh.read_only():
        print("read-only: nothing written.")
        return
    if sys.stdin.isatty() and ask("Proceed? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")
    for repo, number in todo:
        issue = gh.api(f"repos/{repo}/issues/{number}", method="PATCH", milestone=targets[repo])
        if name:
            # After the milestone, so a failed label write can't lose the assignment.
            set_label_value(repo, number, "priority", name, [l["name"] for l in issue["labels"]])
        print(f"  {repo}#{number} → {args.milestone}" + (f", priority {name}" if name else ""))
    for (repo, number), pr in carry.items():
        gh.api(f"repos/{repo}/issues/{number}", method="PATCH", milestone=targets[repo])
        print(f"  {repo}#{number} → {args.milestone} (closed by #{pr})")


def remote_repo(url: str) -> str | None:
    """OWNER/NAME from a git remote's URL, in either spelling git uses for GitHub; None for
    a remote that is somewhere else."""
    match = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https?://github\.com/)"
                         r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url.strip())
    return f"{match[1]}/{match[2]}" if match else None


def app_target(text: str | None) -> tuple[str, int | None]:
    """(repo, issue or pull request number) for what the app is asked to open: a repo as
    OWNER/NAME or its URL, or an issue or a pull request as OWNER/NAME#N or its URL."""
    if ISSUE_RE.fullmatch(text.strip()):
        return parse_issue_ref(text)
    return parse_repo(text), None


def cmd_home(config, args):
    """Open the app on its home page: every tracked repo's milestones."""
    from .board import server
    server.run(config, port=args.port, open_browser=not args.no_open, reload=args.reload)


def cmd_board(config, args):
    """Open the board for one repo: the one named, the one an issue or a pull request named
    is in, or the checkout you are in. The repo need not be in the config."""
    # Imported here: the app is the one command with a server and a front end behind it.
    from .board import client, server
    if args.brief:
        print(client.brief(config["buckets"]))
        return
    if args.repo:
        repo, card = app_target(args.repo)
    else:
        repo, card = cwd_repo(), None
        if repo is None:
            sys.exit("Name a repo (milestones OWNER/NAME), or run this from a checkout "
                     "whose origin is on GitHub.")
    server.run(config, server.board_path(repo),
               server.board_query(args.sources, args.by, args.group, card), port=args.port,
               open_browser=not args.no_open, reload=args.reload)


def cwd_repo() -> str | None:
    """The GitHub repo the current directory is a checkout of, by its origin."""
    found = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
    return remote_repo(found.stdout) if found.returncode == 0 else None


def suggest_ref(text: str, here: str | None) -> tuple[str, int]:
    """(repo, number) for a ref given to `suggest`: OWNER/NAME#N or a URL anywhere, and a
    bare N or #N inside a checkout, which is how an agent working in the repo names an issue."""
    bare = re.fullmatch(r"#?(\d+)", text.strip())
    if bare:
        if here is None:
            sys.exit(f"{text!r} names no repo, and this is not a checkout of one on GitHub. "
                     "Give OWNER/NAME#N.")
        return here, int(bare[1])
    return parse_issue_ref(text)


def cmd_suggest(config, args):
    """Propose changes to an issue on a running board, for the person there to accept or
    reject. Writes nothing to GitHub."""
    from .board import client
    source = args.source or os.environ.get("MILESTONES_SOURCE") or "agent"
    here = cwd_repo()
    by_repo: dict[str, list[dict]] = {}
    if args.json:
        try:
            batch = json.load(sys.stdin)
        except ValueError as error:
            sys.exit(f"--json reads a JSON list from stdin: {error}")
        if not isinstance(batch, list) or not all(isinstance(b, dict) and "ref" in b for b in batch):
            sys.exit('--json reads a list of {"ref", "kind", "value", "reason"} from stdin.')
        for b in batch:
            repo, number = suggest_ref(str(b["ref"]), here)
            by_repo.setdefault(repo, []).append(
                {"issue": number, "kind": b.get("kind"), "value": b.get("value"),
                 "reason": b.get("reason") or "", "source": b.get("source") or source})
    else:
        if not args.ref:
            sys.exit("Give an issue: milestones suggest OWNER/NAME#N --priority high --reason ...")
        if not args.reason:
            sys.exit("Give a --reason: it is what the person at the board reads beside the "
                     "suggestion.")
        comment = args.comment
        if args.comment_file:
            comment = Path(args.comment_file).read_text()
        repo, number = suggest_ref(args.ref, here)
        by_repo[repo] = client.proposals(
            number, args.reason, source, milestone=args.milestone, priority=args.priority,
            effort=args.effort, add_labels=args.add_label, remove_labels=args.remove_label,
            title=args.title, comment=comment, close=args.close)
    for repo, proposed in by_repo.items():
        added = client.post(client.find_session(), f"/api/v1/repos/{repo}/suggestions",
                            {"suggestions": proposed})["suggestions"]
        for s in added:
            print(f"suggested for {repo}#{s['issue']}: {s['kind']} → "
                  f"{json.dumps(s['value'], ensure_ascii=False)}")
        if len(added) < len(proposed):
            print(f"({len(proposed) - len(added)} already suggested or already turned down; "
                  "not repeated)")
    print("Waiting on the person at the board to accept or reject.")


def token_kind(scopes: str | None) -> str:
    """What `doctor` says about a token, from the X-OAuth-Scopes header of a call made with
    it. Only a classic token sends that header; a fine-grained one sends none, and what it
    may do can only be found by trying."""
    if scopes is None:
        return "fine-grained or app token (permissions are per repository; probed below)"
    names = [s.strip() for s in scopes.split(",") if s.strip()]
    return f"classic token, scopes: {', '.join(names) or 'none'}"


def doctor_lines(settings: dict, sources: dict, owners: list[str]) -> list[str]:
    """The settings half of `doctor`: the mode, and which token each owner's calls use.
    Never a token itself — a report gets pasted into issues."""
    mode = env.permissions(settings)
    lines = [f"permissions: {mode}"
             + (f"  ({env.PERMISSIONS} from {sources[env.PERMISSIONS]})"
                if env.PERMISSIONS in sources else "  (default)")
             + ("" if mode == "write" else " — every command says what it would do, and writes nothing")]
    for owner in owners:
        key = env.token_key(settings, owner)
        lines.append(f"{owner}: " + (f"{key} from {sources[key]}" if key else "gh's own login"))
    return lines


def cmd_doctor(config, args):
    """Which token and permissions are in effect, and what that token can see."""
    settings, sources = gh.settings()
    owners = sorted({r.split("/")[0] for r in config["repos"]}, key=str.lower)
    print("env files, nearest first: " + ", ".join(
        f"{path}{'' if path.exists() else ' (none)'}" for path in env.files()))
    print("\n".join(doctor_lines(settings, sources, owners)))
    seen = {}
    for owner in owners:
        key = env.token_key(settings, owner)
        if key not in seen:
            # -i puts the response headers ahead of the body, a blank line between them.
            head, _, body = gh._run(["api", "-i", "user"], owner=owner).replace(
                "\r", "").partition("\n\n")
            scopes = next((line.split(":", 1)[1].strip() for line in head.splitlines()
                           if line.lower().startswith("x-oauth-scopes:")), None)
            seen[key] = (json.loads(body)["login"], scopes)
            print(f"\n{key or 'gh login'}: signed in as {seen[key][0]}; {token_kind(scopes)}")
        kind = "organization" if gh.api(f"users/{owner}")["type"] == "Organization" else "user"
        checks = {"projects": f'{kind}(login: "{owner}") {{ projectsV2(first: 1) '
                              "{ nodes { title } } }"}
        if kind == "organization":
            checks["issue fields"] = (f'organization(login: "{owner}") {{ issueFields(first: 1) '
                                      "{ nodes { __typename } } }")
        for name, query in checks.items():
            errors = gh.probe(f"query {{ {query} }}", owner)
            print(f"  {owner} {name}: " + ("readable" if not errors else
                                           f"not readable — {errors[0].splitlines()[0]}"))
        if kind == "user":
            print(f"  {owner} issue fields: none (an organisation feature)")


def cmd_prs(config, args):
    prs = prs_data(config, args.assigned, args.review_requested, args.mentions)
    if args.json:
        json.dump({"prs": prs}, sys.stdout, indent=2)
        print()
        return
    if not prs:
        print("No open pull requests.")
        return

    def table(rows, heading):
        # By repo, freshest first within it: the question is per repo, not per PR.
        rows = sorted(rows, key=lambda p: p["updated"], reverse=True)
        rows.sort(key=lambda p: p["repo"].lower())
        print_table([(p["repo"], f"#{p['number']}", excerpt(p["title"], 60),
                      "draft" if p["draft"] else "", p["updated"][:10], p["url"])
                     for p in rows],
                    (heading, "PR", "TITLE", "DRAFT", "UPDATED", "URL"), right=("PR",))

    by = {g: [p for p in prs if p["group"] == g] for g in ("tracked", "untracked", "ignored")}
    # Tracked repos only show what needs doing; a PR already on a milestone is in `status`.
    todo = [p for p in by["tracked"] if not p["milestone"]]
    done = len(by["tracked"]) - len(todo)
    if todo:
        print(f"{len(todo)} PR{'s' if len(todo) != 1 else ''} without a milestone in tracked "
              f"repos ({done} more already {'have' if done != 1 else 'has'} one).")
        table(todo, "TRACKED REPO")
        print("run: milestones triage --prs")
    elif by["tracked"]:
        print(f"Every PR in a tracked repo is on a milestone ({done}).")
    # ponytail: no split between repos you could track and upstream ones you can't set a
    # milestone in. author_association would tell them apart for free on author:@me results,
    # but it is the *author's* association, so it lies under --assigned and friends. Add
    # when this list gets too long to eyeball.
    if by["untracked"]:
        print()
        table(by["untracked"], "REPO NOT IN CONFIG")
        print("`milestones add REPO` to track a repo; otherwise these are for your TODO list.")
    if by["ignored"]:
        print()
        table(by["ignored"], "IGNORED REPO")


def issue_count(issues: int, prs: int = 0) -> str:
    """"(3 issues)", "(1 issue)", "(3 issues, 2 PRs)" — PRs only when there are any."""
    count = f"{issues} issue{'' if issues == 1 else 's'}"
    if prs:
        count += f", {prs} PR{'' if prs == 1 else 's'}"
    return f"({count})"


def print_findings(findings: list[dict]) -> None:
    repos = len({f["repo"] for f in findings})
    print(f"{len(findings)} fixes across {repos} repo{'s' if repos != 1 else ''}.")
    for kind, heading in KINDS.items():
        group = [f for f in findings if f["kind"] == kind]
        if not group:
            continue
        print(f"\n{heading}  ({len(group)})")
        # Repo and title get their own column so a repeat offender is obvious at a glance.
        repo_w = max(len(f["repo"]) for f in group)
        title_w = max(len(f["title"]) for f in group)
        counts = [issue_count(f["issues"], f["prs"]) for f in group]
        count_w = max(len(c) for c in counts)
        for count, f in zip(counts, group):
            line = "  • %s  %s  %s  %s" % (f["repo"].ljust(repo_w), f["title"].ljust(title_w),
                                           count.ljust(count_w), f["detail"])
            print("%s\n    %s" % (line.rstrip(), f["url"]))


def cmd_check(config, args):
    findings = collect_findings(config)
    if args.json:
        # `kind` keys into KINDS, which says what the fix is; the rest of a finding is
        # already the flat shape it is printed from.
        json.dump({"findings": findings, "kinds": KINDS}, sys.stdout, indent=2)
        print()
        return
    if not findings:
        print("Nothing to fix — every milestone is named, dated and closed sensibly.")
        return
    print_findings(findings)
    if args.interactive:
        walk_findings(config, findings)


def favourite_date(typed: list[datetime.date]) -> datetime.date | None:
    """The date typed most often this session; the most recent one wins a tie."""
    # max keeps the first of equal keys, so counting backwards breaks a tie in
    # favour of the date typed most recently.
    return max(reversed(typed), key=Counter(typed).__getitem__, default=None)


def date_choices(today: datetime.date,
                 favourite: datetime.date | None = None) -> list[tuple[str, str, datetime.date]]:
    """The keyed dates on offer. The fourth slot is the far-off default until a
    session types a date of its own, after which it offers that date back — the
    same handful of milestones usually want the same day."""
    day = datetime.timedelta(days=1)
    next_month = (today.replace(day=28) + day * 4).replace(day=1)
    return [("t", "today", today),
            ("m", "tomorrow", today + day),
            ("n", "next Monday", today + day * (7 - today.weekday())),
            ("x", "start of next month", next_month) if favourite is None
            else ("x", f"again, {favourite:%a}", favourite)]


def set_due(repo: str, number: int, date: datetime.date) -> None:
    # Midday UTC: GitHub stores the instant, and midnight can read back as the day before.
    gh.api(f"repos/{repo}/milestones/{number}", method="PATCH",
           due_on=f"{date.isoformat()}T12:00:00Z")
    print(f"  → due {date.isoformat()}")


def read_key(prompt: str) -> str:
    """One keypress, no Enter. Falls back to a whole line when stdin isn't a terminal."""
    print(prompt, end="", flush=True)
    if not sys.stdin.isatty():  # ponytail: also how the scripted tests drive this
        line = sys.stdin.readline()
        if not line:
            sys.exit("\nAborted.")
        print(line.strip())
        return line.strip()[:1]
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        key = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if key in ("\x03", "\x04"):  # Ctrl-C, Ctrl-D: raw mode swallows the usual signal
        sys.exit("\nAborted.")
    print(key)
    return key


STAY, QUIT = "stay", "quit"  # what an option's action asks the walk to do next


def walk_findings(config, findings: list[dict]) -> None:
    today = datetime.date.today()
    typed: list[datetime.date] = []  # session-only; the "again" slot follows these
    gone: set[tuple[str, int]] = set()  # deleted, closed, or now a standing bucket
    for n, f in enumerate(findings, 1):
        dates = date_choices(today, favourite_date(typed))
        repo, number, kind = f["repo"], f["number"], f["kind"]
        if (repo, number) in gone:
            continue
        detail = f"  ({f['detail']})" if f["detail"] else ""
        count = issue_count(f["issues"], f["prs"])
        print(f"\n[{n}/{len(findings)}] {kind}: {repo}  {f['title']}"
              f"{'  ' + count if count else ''}{detail}")
        print(f"  {f['url']}")

        def patch(**fields):
            gh.api(f"repos/{repo}/milestones/{number}", method="PATCH", **fields)

        def rename(new: str) -> None:
            # Findings were collected up front and one milestone can raise several,
            # so carry the new title across to the rest — and drop them altogether
            # once it has become a standing bucket, where they no longer apply.
            patch(title=new)
            print(f"  → renamed to '{new}'")
            for other in findings:
                if (other["repo"], other["number"]) == (repo, number):
                    other["title"] = new
            if new in config["buckets"]:
                gone.add((repo, number))

        # Anything needing more than a keypress asks a second question; blank skips.
        def rename_typed():
            title = ask("  new title (blank to skip): ").strip()
            if title:
                rename(title)

        def roll_over():
            dst = ask("  roll its open issues onto which milestone? (blank to skip) ").strip()
            if dst:
                # rollover is also a top-level command, so it exits on a bad title or a
                # declined confirmation; the walk below turns that back into one failed
                # answer rather than the end of the run.
                cmd_rollover(config, argparse.Namespace(repo=repo, src=f["title"],
                                                        dst=dst, close=False))

        def type_date():
            answer = ask("  due date, YYYY-MM-DD (blank to skip): ").strip()
            if not answer:
                return None
            try:
                chosen = datetime.date.fromisoformat(answer)
            except ValueError:
                print("  Not a YYYY-MM-DD date.")
                return STAY
            typed.append(chosen)
            set_due(repo, number, chosen)

        def delete_it():
            if ask(f"  delete '{f['title']}' from {repo}? [y/N] ").strip().lower() != "y":
                return STAY
            gh.api(f"repos/{repo}/milestones/{number}", method="DELETE")
            print("  → deleted")
            gone.add((repo, number))

        def close_it():
            patch(state="closed")
            print("  → closed")
            gone.add((repo, number))

        def reopen_it():
            # Reopening is the one state change that can't hide anything, so a bucket
            # gets it too; setup would have done the same.
            patch(state="open")
            print("  → reopened" + (" (setup would also have done this)"
                                   if f["title"] in config["buckets"] else ""))

        def open_it():
            webbrowser.open(f["url"])
            return STAY

        # (key, label, what it does). One table, so a key can't be offered without a
        # handler or handled without being offered; an action returns STAY to ask
        # again and QUIT to leave the walk, and anything else moves on.
        options = []
        if kind == "rename":
            # The repo's own missing buckets, not every configured one: renaming onto a
            # title it already has is a duplicate-title 422, and a repo using none of
            # them has opted out, so it is offered a typed title and nothing else.
            options += [(str(i), f"→ {b}", lambda b=b: rename(b))
                        for i, b in enumerate(f["free_buckets"], 1)]
            options.append(("r", "rename to…", rename_typed))
        if kind in ("undated", "overdue"):
            options += [(key, f"{label} {date.isoformat()}", lambda d=date: set_due(repo, number, d))
                        for key, label, date in dates]
            options.append(("e", "another date…", type_date))
        if kind == "overdue":
            options.append(("r", "roll its issues over…", roll_over))
        if kind == "stranded":
            # rollover takes a closed source as it is; only the destination must be open.
            options += [("r", "roll its work over…", roll_over), ("p", "reopen it", reopen_it)]
        if kind == "done":
            options.append(("c", "close it", close_it))
        if kind == "empty":
            options.append(("d", "delete it", delete_it))
        options += [("o", "open", open_it), ("s", "skip", lambda: None),
                    ("q", "quit", lambda: QUIT)]

        actions = {key: action for key, _, action in options}
        prompt = "  " + "  ".join(f"[{key}] {label}" for key, label, _ in options) + "  "
        while True:
            action = actions.get(read_key(prompt).lower())
            if action is None:
                print("  Not one of those.")
                continue
            try:
                outcome = action()
            except SystemExit as exit:
                # Every way an action can fail arrives as a SystemExit: gh.py raises one
                # for any failed call, and `ask` raises one for a declined confirmation
                # or a Ctrl-C. None of them should cost you the findings still queued, so
                # report it and ask again — nothing was fixed either way. The Ctrl-C that
                # leaves is the one at the keypress prompt above, outside this.
                print(f"  {exit}")
                continue
            if outcome == QUIT:
                return
            if outcome != STAY:
                break


def cmd_add(config, args):
    # The API answer normalises case and follows renames, and 404s on a typo.
    repo = gh.api(f"repos/{parse_repo(args.repo)}")["full_name"]
    if repo in config["repos"]:
        print(f"already tracked: {repo}")
        return
    write_repo_list(config_path(), "repos", config["repos"] + [repo])
    print(f"tracking {repo} — `milestones setup {repo}` creates the standing buckets "
          f"there if you want them")


def cmd_remove(config, args):
    repo = parse_repo(args.repo)
    keep = [r for r in config["repos"] if r.lower() != repo.lower()]
    if len(keep) == len(config["repos"]):
        sys.exit(f"Not tracked: {repo}. Tracked: {', '.join(config['repos'])}")
    if not keep:
        sys.exit(f"{repo} is the only tracked repo; a config with none is rejected on load.")
    write_repo_list(config_path(), "repos", keep)
    print(f"stopped tracking {repo}")
    # An untracked repo has no rows to mark, so a focus entry left behind is one that can
    # never match again.
    if is_focused(repo, config["focus"]):
        write_repo_list(config_path(), "focus",
                        [r for r in config["focus"] if r.lower() != repo.lower()])
        print("and stopped focusing on it")


def cmd_focus(config, args):
    """Name the repos you're working on right now, so `status` marks their rows."""
    focus = list(config["focus"])
    if not args.repos:
        # Somewhere to look that isn't the config file.
        if focus:
            print_table([(star(True), r, repo_url(r)) for r in focus], ("", "REPO", "URL"))
        else:
            print("Not focused on anything; `milestones focus OWNER/NAME` picks a repo.")
        return
    tracked = {r.lower(): r for r in config["repos"]}
    for text in args.repos:
        repo = parse_repo(text)
        # Focusing on an untracked repo would mark nothing, since `status` only ever lists
        # the tracked ones — so say so rather than writing an entry that does nothing.
        if repo.lower() not in tracked:
            print(f"not tracked: {repo} — `milestones add {repo}` first")
        elif is_focused(repo, focus):
            print(f"already focused: {repo}")
        else:
            # The config's spelling, not the one just typed, so the list stays canonical.
            focus.append(tracked[repo.lower()])
            print(f"focusing on {tracked[repo.lower()]}")
    if focus != config["focus"]:
        write_repo_list(config_path(), "focus", focus)


def cmd_unfocus(config, args):
    focus = list(config["focus"])
    for text in args.repos:
        repo = parse_repo(text)
        if not is_focused(repo, focus):
            print(f"not focused: {repo}")
            continue
        focus = [r for r in focus if r.lower() != repo.lower()]
        print(f"no longer focusing on {repo}")
    if focus != config["focus"]:
        write_repo_list(config_path(), "focus", focus)


def cmd_ignore(config, args):
    # No API round trip to canonicalise the name, unlike `add`: ignoring is not tracking,
    # a typo costs nothing but a repo staying visible, and the first pass through
    # `discover`'s output is dozens of repos at once. Case is handled where they're
    # compared instead.
    tracked = {r.lower() for r in config["repos"]}
    ignore = list(config["ignore"])
    for text in args.repos:
        entry = parse_ignore(text)
        # An owner entry is allowed to sit over repos you track — that is the point of
        # it: add the handful you want, then ignore the rest of the owner. discover
        # checks tracked before ignored, so those keep showing up.
        if not entry.endswith("/*") and entry.lower() in tracked:
            print(f"tracked, not ignored: {entry} — `milestones remove {entry}` first")
        elif is_ignored(entry, ignore):
            print(f"already ignored: {entry}")
        elif entry.endswith("/*"):
            ignore.append(entry)
            print(f"ignoring {entry} — every repo of that owner's you don't track")
        else:
            ignore.append(entry)
            print(f"ignoring {entry}")
    if ignore != config["ignore"]:
        write_repo_list(config_path(), "ignore", ignore)


def cmd_discover(config, args):
    # Straight from the config, before any API call: it prints instantly, and it still
    # prints if the search below dies on an owner that no longer exists.
    print_table([(r, repo_url(r)) for r in config["repos"]], ("TRACKED REPO", "URL"))
    if args.tracked_only:
        return
    print()
    # Typed by hand while GitHub answers with the canonical spelling, so compare in
    # lower case — `remove` already does, and `is_ignored` does the same.
    configured = {r.lower() for r in config["repos"]}
    rows, hidden = set(), set()  # transferred repos can echo under their old owner; dedupe
    for owner in owners_of(config["repos"]):
        cursor = None
        while True:
            data = gh.graphql(
                f'query {{ repositoryOwner(login: "{owner}") {{ '
                f"repositories(first: 100, isFork: false, ownerAffiliations: OWNER, "
                f"orderBy: {{field: PUSHED_AT, direction: DESC}}{after(cursor)}) {{ "
                f"pageInfo {{ hasNextPage endCursor }} nodes {{ "
                f"nameWithOwner isArchived issues(states: OPEN) {{ totalCount }} "
                f"milestones(states: OPEN) {{ totalCount }} }} }} }} }}"
            )
            if data["repositoryOwner"] is None:
                sys.exit(f"No GitHub user or organisation '{owner}' — "
                         f"check the repos in your config.")
            repositories = data["repositoryOwner"]["repositories"]
            for r in repositories["nodes"]:
                name = r["nameWithOwner"]
                issues, ms = r["issues"]["totalCount"], r["milestones"]["totalCount"]
                if r["isArchived"] or name.lower() in configured or not (issues or ms):
                    continue
                # Tracked wins over ignored, so a repo that ends up in both lists simply
                # never reaches here and drops out of the count on its own.
                if is_ignored(name, config["ignore"]):
                    hidden.add((issues, ms, name))
                else:
                    rows.add((issues, ms, name))
            if not repositories["pageInfo"]["hasNextPage"]:
                break
            cursor = repositories["pageInfo"]["endCursor"]
    if rows:
        print_table([(name, issues, ms, repo_url(name))
                     for issues, ms, name in sorted(rows, reverse=True)],
                    ("REPO NOT IN CONFIG", "OPEN ISSUES", "OPEN MILESTONES", "URL"))
    else:
        # True of a repo left out on purpose as much as one already tracked, so this no
        # longer waits on there being nothing hidden — a swept config is all hidden.
        print("Nothing new — every repo found is already tracked or ignored.")
    if args.ignore_remaining and rows:
        # `rows` is already exactly the set to write: found, not archived, has issues or
        # milestones, not tracked, and not ignored — so no dedupe and no is_ignored call
        # here. Sorted, so the config groups them by owner.
        names = sorted(name for _, _, name in rows)
        if ask(f"\nIgnore {len(names)} suggested repos? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")
        write_repo_list(config_path(), "ignore", config["ignore"] + names)
        print(f"ignored {len(names)}; new repos under these owners will still show up.")
    if args.list_ignored and not hidden:
        print("\nNothing ignored — no repo found is on the ignore list.")
    if hidden and not args.ignore_remaining:
        # The count is of what was hidden *before* this run, so it is stale either side of
        # a sweep that just changed it; the sweep reports its own total instead.
        if rows:
            print()
        if args.list_ignored:
            # Same columns as the suggestions above, so a repo reads the same whichever
            # table it is in and `milestones add` takes the first column either way.
            print_table([(name, issues, ms, repo_url(name))
                         for issues, ms, name in sorted(hidden, reverse=True)],
                        ("IGNORED REPO", "OPEN ISSUES", "OPEN MILESTONES", "URL"))
            print()
        print(f"Found {len(hidden)} ignored repositor{'y' if len(hidden) == 1 else 'ies'}; "
              f"use `milestones add` to explicitly add them or delete them from the "
              f"ignore list at {config_path()}."
              + ("" if args.list_ignored else " Pass `--list-ignored` to see which."))


def route_argv(argv: list[str], commands: set[str]) -> list[str]:
    """The command line with its command spelt out: nothing at all, or flags alone, is `home`;
    a repo, an issue or a pull request in place of a command is `board` on it."""
    # `--port N` is the one flag here whose value is a word of its own, and not the command.
    first = next((a for i, a in enumerate(argv)
                  if not a.startswith("-") and not (i and argv[i - 1] == "--port")), None)
    if first in commands or any(a in ("-h", "--help") for a in argv):
        return argv
    if first is None:
        return ["home", *argv]
    if ISSUE_RE.fullmatch(first) or REPO_RE.fullmatch(first):
        return ["board", *argv]
    return argv


def main():
    parser = argparse.ArgumentParser(prog="milestones", description=__doc__)
    sub = parser.add_subparsers()

    def app_flags(command):
        command.add_argument("--port", type=int,
                             help=f"default: {DEFAULT_PORT}, or any free port if that is taken")
        command.add_argument("--no-open", action="store_true", help="print the URL, open nothing")
        command.add_argument("--reload", action="store_true",
                             help="restart on the same URL whenever the package's Python "
                                  "changes (for working on the app; pages need a refresh after)")

    home = sub.add_parser("home", help="open the app in your browser, on every tracked repo's "
                                       "milestones (what `milestones` alone does)")
    app_flags(home)
    home.set_defaults(func=cmd_home)

    status = sub.add_parser("status",
                            help="all open milestones across configured repos, by due date")
    status.add_argument("--json", action="store_true",
                        help="print the milestones as JSON instead of a table")
    status.set_defaults(func=cmd_status)
    triage = sub.add_parser("triage", help="interactively assign milestones to untriaged issues")
    triage.add_argument("--repo", metavar="OWNER/NAME", help="triage a single repo")
    # Nothing to walk if the issues are going out to a pipe.
    how = triage.add_mutually_exclusive_group()
    how.add_argument("--list", action="store_true",
                     help="print the untriaged issues one per line instead of walking them")
    how.add_argument("--json", action="store_true",
                     help="print them as JSON instead")
    triage.add_argument("--prs", action="store_true",
                        help="walk untriaged pull requests instead of issues")
    # `from` is a keyword, hence the dest. Repeatable, and it replaces the default set —
    # the items with no milestone, then the ones on Needs triage — rather than adding to it.
    triage.add_argument("--from", dest="sources", action="append", metavar="MILESTONE",
                        help="walk the open items on this milestone instead (repeatable; "
                             "`none` for the ones with no milestone)")
    triage.set_defaults(func=cmd_triage)
    assign = sub.add_parser("assign", help="put issues on a milestone by title, across repos")
    assign.add_argument("milestone", metavar="MILESTONE",
                        help="milestone title, resolved in each issue's repo")
    assign.add_argument("refs", metavar="REF", nargs="*",
                        help="OWNER/NAME#N or an issue URL; none, to read them from stdin")
    assign.add_argument("--priority", choices=[n.lower() for n in reconcile.KINDS["priority"]["options"]],
                        help="also set the Priority: label on each issue and pull request")
    assign.set_defaults(func=cmd_assign)
    rollover = sub.add_parser("rollover", help="move open issues from one milestone to another")
    rollover.add_argument("repo", metavar="OWNER/NAME")
    rollover.add_argument("src", metavar="FROM", help="source milestone title")
    rollover.add_argument("dst", metavar="TO", help="destination milestone title")
    rollover.add_argument("--close", action="store_true", help="close FROM once empty")
    rollover.set_defaults(func=cmd_rollover)
    setup = sub.add_parser("setup", help="create the standing bucket milestones in a repo")
    setup.add_argument("repo", metavar="OWNER/NAME")
    setup.set_defaults(func=cmd_setup)
    board = sub.add_parser("board", help="triage one repo's issues on a board in your browser, "
                                         "a column per milestone; every move is written "
                                         "to GitHub as you make it (what `milestones REPO` does)")
    board.add_argument("repo", nargs="?", metavar="REPO",
                       help="OWNER/NAME or its URL, or an issue or pull request (OWNER/NAME#N "
                            "or its URL) to open the board at; default: the origin of the "
                            "checkout you are in")
    board.add_argument("--from", dest="sources", action="append", metavar="MILESTONE",
                       help="show only these milestones' columns to begin with (repeatable; "
                            "`none` is the issues with no milestone)")
    board.add_argument("--by", choices=["milestone", "priority", "effort"],
                       help="what the columns are to begin with (default: milestone)")
    # argparse joins choices with commas, so "priority,effort" would read as a repeat in --help
    board.add_argument("--group", choices=["priority", "effort", "priority,effort", "milestone"],
                       metavar="KIND",
                       help="group the cards within each column to begin with: priority, "
                            "effort, milestone, or priority,effort for both (the default)")
    app_flags(board)
    board.add_argument("--brief", action="store_true",
                       help="print the briefing to give a coding agent that will suggest "
                            "changes, and exit")
    board.set_defaults(func=cmd_board)

    suggest = sub.add_parser("suggest", help="propose changes to an issue on a running board, "
                                             "for the person there to accept or reject; "
                                             "writes nothing itself")
    suggest.add_argument("ref", nargs="?", metavar="REF",
                         help="OWNER/NAME#N, an issue URL, or N inside the repo's checkout")
    suggest.add_argument("--milestone", metavar="TITLE", help='"none" takes it off its milestone')
    suggest.add_argument("--priority", metavar="P", help='urgent, high, medium or low; "none" clears')
    suggest.add_argument("--effort", metavar="E", help='xs, s, m, l or xl; "none" clears')
    suggest.add_argument("--add-label", action="append", metavar="LABEL")
    suggest.add_argument("--remove-label", action="append", metavar="LABEL")
    suggest.add_argument("--title", help="a new title")
    suggest.add_argument("--comment", help="a comment to post")
    suggest.add_argument("--comment-file", metavar="PATH", help="a comment to post, from a file")
    suggest.add_argument("--close", choices=["completed", "not_planned"])
    suggest.add_argument("--reason", help="why, in a sentence or two; shown beside the suggestion")
    suggest.add_argument("--source", help="who is suggesting, e.g. a model's name "
                                          "(default: $MILESTONES_SOURCE, else 'agent')")
    suggest.add_argument("--json", action="store_true",
                         help='read a batch from stdin: [{"ref", "kind", "value", "reason"}]')
    suggest.set_defaults(func=cmd_suggest)

    check = sub.add_parser("check",
                           help="milestones that need renaming, dating, closing or rolling over")
    # Nothing to walk if the findings are going out as JSON.
    how = check.add_mutually_exclusive_group()
    how.add_argument("-i", "--interactive", action="store_true",
                     help="walk the findings one by one and fix them")
    how.add_argument("--json", action="store_true",
                     help="print the findings as JSON instead of a report")
    check.set_defaults(func=cmd_check)
    add = sub.add_parser("add", help="track a repo (OWNER/NAME or github.com URL)")
    add.add_argument("repo", metavar="REPO")
    add.set_defaults(func=cmd_add)
    remove = sub.add_parser("remove", help="stop tracking a repo")
    remove.add_argument("repo", metavar="REPO")
    remove.set_defaults(func=cmd_remove)
    focus = sub.add_parser("focus", help="mark repos you're working on right now; "
                                        "`status` stars their rows")
    focus.add_argument("repos", metavar="REPO", nargs="*",
                       help="the repos to focus on; none, to list what's in focus")
    focus.set_defaults(func=cmd_focus)
    unfocus = sub.add_parser("unfocus", help="stop marking a repo you were working on")
    unfocus.add_argument("repos", metavar="REPO", nargs="+")
    unfocus.set_defaults(func=cmd_unfocus)
    ignore = sub.add_parser("ignore", help="hide repos from discover without tracking them")
    ignore.add_argument("repos", metavar="REPO", nargs="+")
    ignore.set_defaults(func=cmd_ignore)
    discover = sub.add_parser("discover",
                              help="the tracked repos, then ones with issues/milestones "
                                   "missing from the config")
    listing = discover.add_mutually_exclusive_group()
    listing.add_argument("--tracked-only", action="store_true",
                         help="just list the tracked repos; don't go looking for more")
    listing.add_argument("--list-ignored", action="store_true",
                         help="list the ignored repos found, rather than only counting them")
    listing.add_argument("--ignore-remaining", action="store_true",
                         help="add every repo suggested above to the ignore list")
    discover.set_defaults(func=cmd_discover)
    doctor = sub.add_parser("doctor", help="show which GitHub token and permissions are in "
                                           "effect, and what that token can see")
    doctor.set_defaults(func=cmd_doctor)

    prs = sub.add_parser("prs", help="every open PR of yours, anywhere, grouped by whether "
                                     "its repo is tracked, ignored, or neither")
    prs.add_argument("--assigned", action="store_true", help="also PRs assigned to you")
    prs.add_argument("--review-requested", action="store_true",
                     help="also PRs waiting on your review")
    prs.add_argument("--mentions", action="store_true", help="also PRs that mention you")
    prs.add_argument("--json", action="store_true",
                     help="print the PRs as JSON instead of tables")
    prs.set_defaults(func=cmd_prs)

    argv = route_argv(sys.argv[1:], set(sub.choices))
    args = parser.parse_args(argv)
    # The app opens with no config at all, and on a repo the config doesn't name.
    args.func(load_config(needs_repos=args.func not in (cmd_home, cmd_board, cmd_suggest)), args)


if __name__ == "__main__":
    main()
