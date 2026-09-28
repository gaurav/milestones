"""milestones: view and manage GitHub milestones across all my repositories."""

import argparse
import datetime
import json
import os
import re
import sys
import termios
import textwrap
import tomllib
import tty
import webbrowser
from collections import Counter
from itertools import cycle
from pathlib import Path

from . import gh

# The one bucket the triage walk reads from as well as writes to: an issue on it has been
# looked at and can't be placed yet — it needs a reproduction, tests, an investigation or a
# discussion first — where an issue with no milestone is one nobody has looked at. Sometimes a
# person sets it not knowing that no milestone means the same thing; that is harmless, since
# `triage` walks both. It is last in the list so that it sorts after the urgency buckets.
TRIAGE_BUCKET = "Needs triage"

# MILESTONES.md says what each of these is for; keep its table in step with this list.
DEFAULT_BUCKETS = ["Critical", "Needed soon", "Needed later", "Not urgent", "Upstream",
                   TRIAGE_BUCKET]

# Buckets that exist only once something needs them: `setup` doesn't create one, `triage`
# offers it anyway and creates it when it's picked, and `status` hides one with nothing
# open on it, so that a quiet "Critical" doesn't say "all is well" at the top of the table.
OPTIONAL_BUCKETS = {"Critical"}

EXAMPLE_CONFIG = """\
buckets = [%s]
repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]
""" % ", ".join(f'"{b}"' for b in DEFAULT_BUCKETS)


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "milestones.toml"


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"No config found at {path}. Create it; for example:\n\n{EXAMPLE_CONFIG}")
    config.setdefault("buckets", list(DEFAULT_BUCKETS))
    # An empty ignore list is the normal state; an empty repos list is not. Focusing on
    # nothing in particular is the normal state too.
    config.setdefault("ignore", [])
    config.setdefault("focus", [])
    if not config.get("repos"):
        sys.exit(f"Config {path} has no repos. Add some; for example:\n\n{EXAMPLE_CONFIG}")
    bad = [r for r in config["repos"] + config["ignore"] + config["focus"]
           if r.count("/") != 1 or not all(r.split("/"))]
    if bad:
        sys.exit(f"Config {path}: these are not OWNER/NAME: {', '.join(bad)}")
    # Owner -> colour, resolved here so `status` can hand org_colors plain numbers.
    config["colors"] = {owner: parse_color(path, owner, value)
                        for owner, value in config.get("colors", {}).items()}
    return config


def parse_color(path: Path, owner: str, value) -> int:
    """One `[colors]` entry: a name from COLOR_NAMES, or a 256-colour number."""
    if isinstance(value, str) and value in COLOR_NAMES:
        return COLOR_NAMES[value]
    # `isinstance(True, int)` is true, so a bare `owner = true` would resolve to colour 1
    # rather than being rejected.
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255:
        return value
    sys.exit(f"Config {path}: colour {value!r} for {owner} is not one of "
             f"{', '.join(sorted(COLOR_NAMES))}, or a number from 0 to 255.")


REPO_RE = re.compile(r"(?:(?:https?://)?github\.com/)?([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


# "v1.2", "Babel v1.19" — and dated releases, "2026aug24" or "Week ending 2026-08-25".
VERSION_RE = re.compile(r"\bv\d+(\.\d+)*\b", re.I)
DATE_RE = re.compile(r"\b\d{4}(-\d{2}-\d{2}|[a-z]{3}\d{1,2})\b", re.I)


# Each kind is one shape of fix, and heads its own group in the report.
KINDS = {
    "stranded": "Roll over or reopen — closed, but work is still open on it",
    "rename": "Rename — no version or date in the title, and not a standing bucket",
    "undated": "Set a due date — however far out; an undated milestone never comes due",
    "overdue": "Roll over or re-date — past due with work still open",
    "done": "Close — every issue on it is closed",
    "empty": "Delete or fill — nothing has ever been filed against it",
}


def milestone_problems(title: str, due: str | None, open_issues: int, closed_issues: int,
                       buckets: list[str], today: str,
                       open_prs: int = 0, closed_prs: int = 0,
                       closed: bool = False) -> list[tuple[str, str]]:
    """(kind, detail) for everything wrong with one milestone.

    A pull request is work on the milestone as much as an issue is, so every
    decision here is over both: a milestone holding only open PRs is not done.
    """
    open_work, closed_work = open_issues + open_prs, closed_issues + closed_prs
    if closed:
        # Work on a closed milestone is invisible to status and to triage, which only
        # sees items with no milestone at all. Nothing else can be wrong with a closed
        # milestone that matters, and this holds for a closed bucket too.
        return [("stranded", f"closed with {open_work} still open")] if open_work else []
    problems = []
    if due and due < today and open_work:
        problems.append(("overdue", f"due {due}, {open_work} still open"))
    if title in buckets:
        # A standing bucket is meant to be undated, named for itself, and empty
        # between triage rounds — and closing one would hide it from status and
        # triage. Running late is the only thing that can be wrong with one.
        return problems
    if not (VERSION_RE.search(title) or DATE_RE.search(title)):
        problems.append(("rename", ""))
    if not due:
        problems.append(("undated", ""))
    if closed_work and not open_work:
        problems.append(("done", f"all {closed_work} closed"))
    if not closed_work and not open_work:
        problems.append(("empty", ""))
    return problems


def free_buckets(buckets: list[str], titles: set[str]) -> list[str]:
    """The standing buckets a milestone in this repo could be renamed onto.

    That is the ones it hasn't got, since a rename onto one it has would collide. A repo
    using none of them gets none: most repos don't need this much triage, and a rename is
    no way to opt one in. A repo is free to use any subset, so what is left over here is
    not a gap to fill.
    """
    free = [b for b in buckets if b not in titles]
    return free if free != buckets else []


def parse_repo(text: str) -> str:
    """OWNER/NAME from either that or a github.com URL."""
    match = REPO_RE.fullmatch(text.strip())
    if not match:
        sys.exit(f"Not a repo: {text!r}. Give OWNER/NAME or a github.com URL.")
    return f"{match[1]}/{match[2]}"


# "a/b#12", "https://github.com/a/b/issues/12" — and "/pull/12", since the issues endpoint
# sets a PR's milestone the same way, as rollover already relies on.
ISSUE_RE = re.compile(
    r"(?:(?:https?://)?github\.com/)?([^/\s#]+)/([^/\s#]+)(?:#|/(?:issues|pull)/)(\d+)/?$")


def parse_issue_ref(text: str) -> tuple[str, int]:
    """(OWNER/NAME, number) from a ref, an issue URL, or a whole `triage --list` line."""
    # The first token only: a --list line carries the title and labels after the ref.
    tokens = text.split()
    match = ISSUE_RE.fullmatch(tokens[0]) if tokens else None
    if not match:
        sys.exit(f"Not an issue: {text!r}. Give OWNER/NAME#123 or a github.com issue URL.")
    return f"{match[1]}/{match[2]}", int(match[3])


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


def is_ignored(name: str, ignore: list[str]) -> bool:
    """Whether OWNER/NAME is on the ignore list, itself or under an ignored owner."""
    # Folded case throughout: the list is typed by hand while GitHub answers with the
    # canonical spelling.
    name = name.lower()
    return any(entry.lower() in (name, f"{name.split('/')[0]}/*") for entry in ignore)


def is_focused(repo: str, focus: list[str]) -> bool:
    """Whether OWNER/NAME is one of the repos you're currently working on.

    No `OWNER/*` here, unlike the ignore list: focus is a handful of repos you name, and
    a whole organisation at once has never been the thing anyone wanted. Folded case
    either way — the list is typed by hand while GitHub answers with the canonical
    spelling.
    """
    return repo.lower() in {f.lower() for f in focus}


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


def repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def sort_key(title: str, due_on: str | None, buckets: list[str]):
    """Dated milestones ascending (overdue naturally first), then buckets in
    config order, then other undated milestones by title."""
    if due_on:
        return (0, due_on, title)
    if title in buckets:
        # Slot 0 already separates the branches, so slot 1 is only ever compared
        # against the same type.
        return (1, buckets.index(title), title)
    return (2, "", title)


def build_search_queries(repos: list[str], cap: int = 256, kind: str | None = "issue",
                         milestone: str | None = None) -> list[str]:
    """Queries covering every configured repo, each under GitHub's 256-char cap.

    Scoped by repo rather than by owner: an owner's unconfigured repos would
    otherwise crowd real results out of the single page search_issues fetches.
    `kind` is "issue", "pr", or None for both in one query. `milestone` is a title to
    search on — matched by title alone, whatever state the milestone is in — or None for
    items with no milestone. One source per query set, on purpose: `(no:milestone OR
    milestone:"…")` is accepted too, but the walk wants its sources in order, and each
    query keeps its own 1000-result headroom this way.
    """
    where = f'milestone:"{milestone}"' if milestone else "no:milestone"

    def query(batch):
        # Advanced search ANDs repeated qualifiers, so repos must be OR'd explicitly.
        return "%sis:open %s archived:false (%s)" % (
            f"is:{kind} " if kind else "", where, " OR ".join("repo:" + r for r in batch))

    queries, batch = [], []
    for repo in sorted(set(repos)):
        if batch and len(query(batch + [repo])) > cap:
            queries.append(query(batch))
            batch = []
        batch.append(repo)
    return queries + [query(batch)]


def prs_query(assigned: bool = False, review_requested: bool = False,
              mentions: bool = False) -> str:
    """Every open pull request that is yours: authored, and whatever else is asked for."""
    who = ["author:@me"] + [q for flag, q in ((assigned, "assignee:@me"),
                                              (review_requested, "review-requested:@me"),
                                              (mentions, "mentions:@me")) if flag]
    # Always parenthesised: advanced search ANDs bare repeated qualifiers.
    return "is:pr is:open (%s)" % " OR ".join(who)


def pr_group(repo: str, config: dict) -> str:
    """Which table a PR's repo belongs in: tracked wins, then ignored, else untracked."""
    if repo.lower() in {r.lower() for r in config["repos"]}:
        return "tracked"
    return "ignored" if is_ignored(repo, config["ignore"]) else "untracked"


def triage_order(issues: list[dict], focus: list[str]) -> list[dict]:
    """Freshest first, but the repos you're working on ahead of everything else."""
    # Two passes rather than one compound key: `reverse` would flip the focus flag along
    # with the date. Python's sort is stable, so the second pass keeps the first's order
    # within each group.
    issues = sorted(issues, key=lambda i: i["updated"], reverse=True)
    issues.sort(key=lambda i: not is_focused(i["repo"], focus))
    return issues


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

# Names for the org colours, so a config can say "pink" rather than 218. Pastels and mid
# tones only: an owner's colour is an identity, not a rating, and it should not compete with
# the DUE and % columns for the eye.
COLOR_NAMES = {"blue": 39, "cyan": 44, "teal": 74, "indigo": 105, "violet": 141,
               "magenta": 170, "purple": 183, "yellow": 186, "green": 114, "rose": 212,
               "pink": 218, "gold": 220, "grey": 250}

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
    # A column empty from its header to the last row takes no space at all, rather than
    # indenting everything past it: the flags column with nothing flagged, the focus
    # column with nothing focused.
    keep = [i for i, width in enumerate(widths) if width]
    for row in [headers, *rows]:
        cells = []
        for i in keep:
            pad = " " * (widths[i] - visible(row[i]))
            cells.append(pad + str(row[i]) if headers[i] in right else str(row[i]) + pad)
        print("  ".join(cells).rstrip())


# --- commands ---------------------------------------------------------------


def after(cursor: str | None) -> str:
    """The `after:` argument for a paginated connection, empty for the first page."""
    return f', after: "{cursor}"' if cursor else ""


def fetch_milestones(repos: list[str],
                     closed: bool = False) -> tuple[list[str], list[tuple[str, dict]]]:
    """GitHub's own name for each repo, and (repo, milestone) for every open
    milestone — and every closed one too, with `closed`; each carries its `state`.
    The names follow renames and fix up the config's capitalisation, so callers
    should key off them, not off `repos`.

    One aliased round trip per page: every repo is queried together, and only the
    repos that still have milestones left go into the next trip, so the usual case
    of nobody being near a full page costs exactly one query.
    """
    names: dict[str, str] = {}
    milestones = []
    pages: dict[str, str | None] = {r: None for r in repos}  # repo -> next page's cursor
    states = "[OPEN, CLOSED]" if closed else "OPEN"
    while pages:
        alias_of = {f"r{i}": repo for i, repo in enumerate(pages)}
        aliases = " ".join(
            f'{alias}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") '
            f"{{ nameWithOwner milestones(states: {states}, first: 100{after(pages[r])}) "
            f"{{ pageInfo {{ hasNextPage endCursor }} "
            f"nodes {{ title url dueOn number state "
            f"open: issues(states: OPEN) {{ totalCount }} "
            f"closed: issues(states: CLOSED) {{ totalCount }} "
            # `issues` never counts pull requests; they are their own connection.
            f"openPrs: pullRequests(states: OPEN) {{ totalCount }} "
            f"closedPrs: pullRequests(states: [CLOSED, MERGED]) {{ totalCount }} }} }} }}"
            for alias, r in alias_of.items()
        )
        data = gh.graphql(f"query {{ {aliases} }}")
        pages = {}
        for alias, repo in alias_of.items():
            node = data[alias]
            if node is None:  # deleted, private, or a typo; gh.graphql has warned
                continue
            names[repo] = node["nameWithOwner"]
            milestones += [(node["nameWithOwner"], m) for m in node["milestones"]["nodes"]]
            page = node["milestones"]["pageInfo"]
            if page["hasNextPage"]:
                pages[repo] = page["endCursor"]
    return list(names.values()), milestones


def untriaged_counts(items: list[dict]) -> list[dict]:
    """Per repo, how many open issues and pull requests have no milestone — only the repos
    that have any, by name. `repo` is GitHub's spelling, as fetch_milestones' names are."""
    counts: dict[str, dict] = {}
    for item in items:
        row = counts.setdefault(item["repo"], {"repo": item["repo"], "issues": 0, "prs": 0})
        row["prs" if item["pr"] else "issues"] += 1
    return [counts[repo] for repo in sorted(counts, key=str.lower)]


def cmd_status(config, args):
    today = datetime.date.today().isoformat()
    milestones = []
    stranded = []  # closed milestones with open work: not rows, but not silence either
    tracked, entries = fetch_milestones(config["repos"], closed=True)
    for repo, m in entries:
        due = m["dueOn"][:10] if m["dueOn"] else None
        count, prs = m["open"]["totalCount"], m["openPrs"]["totalCount"]
        if m["state"] == "CLOSED":
            if count + prs:
                stranded.append({"repo": repo, "title": m["title"], "open": count,
                                 "open_prs": prs, "url": m["url"]})
            continue
        if (m["title"] in OPTIONAL_BUCKETS and m["title"] in config["buckets"]
                and not count + prs):
            continue
        milestones.append((repo, m["title"], due, count, m["closed"]["totalCount"],
                           prs, m["closedPrs"]["totalCount"], m["url"]))
    milestones.sort(key=lambda m: sort_key(m[1], m[2], config["buckets"]))
    # A tracked repo with no open milestone has no row of its own, and so is invisible
    # here unless it is named; `discover` lists the config's repos in full.
    quiet = sorted(set(tracked) - {m[0] for m in milestones})
    # "Is everything triaged?" is the other half of the question status answers; one
    # search over every tracked repo, issues and PRs together.
    untriaged = untriaged_counts(fetch_untriaged(config, None, kind=None))
    for row in untriaged:
        row["focus"] = is_focused(row["repo"], config["focus"])

    records = []
    for repo, title, due, count, closed, prs, closed_prs, url in milestones:
        # `check` owns what is wrong with a milestone; status shows the three of its
        # kinds that read as a state the milestone is in rather than a fix to make,
        # and so agrees with `check` about standing buckets and about a past-due
        # milestone with nothing left open. Empty means nothing was ever filed on it,
        # not "all done".
        kinds = {kind for kind, _ in milestone_problems(title, due, count, closed,
                                                        config["buckets"], today,
                                                        prs, closed_prs)}
        # `open` and `closed` are issues, as GitHub counts them; `percent` is over all the
        # work, so a milestone that holds only PRs still says how far along it is.
        done_work, total = closed + closed_prs, count + closed + prs + closed_prs
        records.append({"repo": repo, "title": title, "due": due, "open": count,
                        "closed": closed, "open_prs": prs, "closed_prs": closed_prs,
                        "percent": round(100 * done_work / total) if total else None,
                        "flags": [k for k in ("overdue", "empty", "done") if k in kinds],
                        "focus": is_focused(repo, config["focus"]),
                        "url": url})
    if args.json:
        # The quiet repos in the same shape as a milestone row, so a reader doesn't have to
        # cross-reference to find out whether one is focused. The config's own list comes
        # too: it is the only place an entry naming a repo you no longer track shows up.
        json.dump({"milestones": records,
                   "quiet_repos": [{"repo": r, "focus": is_focused(r, config["focus"]),
                                    "url": repo_url(r)} for r in quiet],
                   "stranded": stranded, "untriaged": untriaged,
                   "focus": config["focus"]}, sys.stdout, indent=2)
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
        # PRS is open pull requests; print_table drops it when no milestone has any.
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
    print(f"\n{issues} untriaged issue{'s' if issues != 1 else ''} and {prs} pull "
          f"request{'s' if prs != 1 else ''} (no milestone):")
    # A zero prints as blank so the column reads as "which repos have PRs waiting".
    print_table([(star(r["focus"]), r["repo"], r["issues"] or "", r["prs"] or "")
                 for r in untriaged],
                ("", "REPO", "ISSUES", "PRS"), right=("ISSUES", "PRS"))
    print("run: milestones triage --prs" + ("  (then: milestones triage)" if issues else "")
          if prs else "run: milestones triage")


def _milestones_by_title(repo: str, state: str = "all") -> dict[str, dict]:
    return {m["title"]: m
            for m in gh.api(f"repos/{repo}/milestones?state={state}&per_page=100",
                            paginate=True)}


def _ensure_bucket(repo: str, title: str, existing: dict[str, dict]) -> dict:
    """The open milestone called `title`, creating or reopening it as needed."""
    milestone = existing.get(title)
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


def cmd_rollover(config, args):
    by_title = _milestones_by_title(args.repo)
    for title in (args.src, args.dst):
        if title not in by_title:
            sys.exit(f"No milestone '{title}' in {args.repo}. "
                     f"Milestones: {', '.join(sorted(by_title))}")
    src, dst = by_title[args.src], by_title[args.dst]
    if dst["state"] != "open":
        sys.exit(f"'{args.dst}' is closed; issues moved onto it would disappear from both "
                 f"status and triage. Reopen it first.")
    if args.close and args.src in config["buckets"]:
        # Every other path refuses to close a bucket; --close was the way round it.
        sys.exit(f"'{args.src}' is a standing bucket, so it outlives the issues on it. "
                 f"Closing it would hide it from status and triage until setup reopened it; "
                 f"roll it over without --close.")

    # The REST issues endpoint returns pull requests too, and they move the same way.
    items = gh.api(f"repos/{args.repo}/issues?milestone={src['number']}&state=open&per_page=100",
                   paginate=True)
    if items:
        for item in items:
            print(f"  #{item['number']} {item['title']}"
                  f"{' (PR)' if 'pull_request' in item else ''}")
        answer = ask(f"Move {len(items)} open issues and PRs from '{args.src}' to '{args.dst}' "
                     f"in {args.repo}? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Aborted.")
        for item in items:
            gh.api(f"repos/{args.repo}/issues/{item['number']}", method="PATCH",
                   milestone=dst["number"])
            print(f"moved #{item['number']}")
    else:
        print(f"Nothing open in '{args.src}'.")

    if args.close:
        # Not re-reading the milestone's open_issues to confirm it is empty: that
        # counter lags writes, and we just moved everything open off it.
        gh.api(f"repos/{args.repo}/milestones/{src['number']}", method="PATCH", state="closed")
        print(f"closed '{args.src}'")


def _norm_issue(issue: dict, repo: str) -> dict:
    # Both the REST listing and a search item carry `draft` on a pull request, and the
    # milestone it is on, if any — the walk says so when it offers an item that has one.
    milestone = issue.get("milestone")
    return {"repo": repo, "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "labels": [l["name"] for l in issue["labels"]],
            "updated": issue["updated_at"], "url": issue["html_url"], "id": issue["node_id"],
            "pr": "pull_request" in issue, "draft": bool(issue.get("draft")),
            "milestone": milestone["title"] if milestone else None}


def priority_options(fields: list[dict]) -> dict[str, tuple[str, str]] | None:
    """The walk's priority keys as (field id, option id), from an organisation's issue
    fields — its single-select "Priority" with Urgent, High and Low options, names matched
    ignoring case. None when it hasn't got all of that, so no key is offered by half."""
    for field in fields:
        if field.get("__typename") != "IssueFieldSingleSelect" or field["name"].lower() != "priority":
            continue
        by_name = {o["name"].lower(): o["id"] for o in field["options"]}
        keyed = {key: (field["id"], by_name[name.lower()])
                 for key, name in PRIORITY_KEYS.items() if name.lower() in by_name}
        return keyed if len(keyed) == len(PRIORITY_KEYS) else None
    return None


def fetch_priority_field(owner: str) -> dict[str, tuple[str, str]] | None:
    """`priority_options` for a repo owner. Issue fields are an organisation feature, so a
    user account is answered without the GraphQL round trip (and its NOT_FOUND warning)."""
    if gh.api(f"users/{owner}")["type"] != "Organization":
        return None
    data = gh.graphql(
        f'query {{ organization(login: "{owner}") {{ issueFields(first: 50) {{ nodes {{ '
        f"__typename ... on IssueFieldSingleSelect {{ id name options {{ id name }} }} }} }} }} }}")
    return priority_options(data["organization"]["issueFields"]["nodes"])


def set_priority(issue_id: str, field_id: str, option_id: str) -> None:
    gh.graphql(f'mutation {{ setIssueFieldValue(input: {{issueId: "{issue_id}", '
               f'issueFields: [{{fieldId: "{field_id}", singleSelectOptionId: "{option_id}"}}]}}) '
               f"{{ clientMutationId }} }}")


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


def fetch_untriaged(config, repo: str | None, kind: str | None = "issue",
                    sources: list[str | None] = (None,)) -> list[dict]:
    """Open items from each of `sources` in turn — None for the ones with no milestone, a
    title for the ones on that milestone — each in walk order: focused repos first, then
    freshest. Every item carries the `source` it came from, spelt as it was asked for.

    `kind` is "issue", "pr", or None for both at once.
    """
    issues = []
    by_title = _milestones_by_title(repo) if repo and any(sources) else {}
    for source in sources:
        items = []
        if repo:
            # A title the repo hasn't got is nothing to walk from it, and the walk's opening
            # line says so; it is not an error, since the other sources may still have items.
            number = "none" if source is None else (
                by_title[source]["number"] if source in by_title else None)
            raw = gh.api(f"repos/{repo}/issues?milestone={number}&state=open&per_page=100",
                         paginate=True) if number is not None else []
            items = [_norm_issue(i, repo) for i in raw
                     if kind is None or ("pull_request" in i) == (kind == "pr")]
        else:
            for query in build_search_queries(config["repos"], kind=kind, milestone=source):
                for item in gh.search_issues(query):
                    items.append(_norm_issue(item, "/".join(item["repository_url"].split("/")[-2:])))
        for item in items:
            item["source"] = source
        issues += triage_order(items, config["focus"])
    return issues


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


def carries(closed: dict, repo: str) -> bool:
    """Whether a PR in `repo` takes this issue it closes onto its own milestone: open, on no
    milestone yet, and in the same repo — milestone numbers are per repo, so one elsewhere
    is shown but left alone."""
    return (closed["state"] == "OPEN" and not closed["milestone"]
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


def triage_scope(items: list[dict], noun: str, repo: str | None,
                 tracked: int) -> tuple[str, str]:
    """What a walk covers, as (the phrase the summary repeats, the per-repo counts only the
    opening line adds): ("11 PRs without a milestone in https://github.com/a/b", "")."""
    what = f"{plural(len(items), noun) if items else 'no ' + noun + 's'} without a milestone"
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
                   rerun: str) -> list[str]:
    """The lines a walk ends on, however it ends: what it covered, what it wrote, and what
    is still to do. `done` is the walk's log, a list per kind of thing that happened."""
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
    for ref, reason in done["unprioritised"]:
        lines.append(f"Priority not set on {ref}: {reason}")
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
        lines.append(f"{left} still without a milestone: `{rerun}` walks them again{lag}")
    return [lines[0]] + [f"  - {line}" for line in lines[1:]]


def cmd_triage(config, args):
    issues = fetch_untriaged(config, args.repo, "pr" if args.prs else "issue")
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
            labels = f"  [{', '.join(i['labels'])}]" if i["labels"] else ""
            closes = f"  {closes_line(i['closes'], i['repo'])}" if i.get("closes") else ""
            print(f"{i['repo']}#{i['number']}  {i['title']}{marker}{labels}{closes}")
        return

    noun = "PR" if args.prs else "issue"
    scope, per_repo = triage_scope(issues, noun, args.repo, len(config["repos"]))
    if not issues:
        print(f"Nothing to triage: {scope}.")
        return
    print(f"Found {scope}" + (f": {per_repo}" if per_repo else ""))
    rerun = "milestones triage" + (" --prs" if args.prs else "") + (
        f" --repo {args.repo}" if args.repo else "")
    # Everything the walk does, for the summary it ends on: (ref, milestone title) for
    # "assigned", "carried" and "passed", (ref, priority name) for "priority", (ref, why)
    # for "unprioritised" and "failed", (repo, title) for "created" and "reopened", and
    # (title, url, "top" or "bottom") for "unordered"; "skipped" is bare refs.
    done: dict[str, list] = {key: [] for key in (
        "assigned", "carried", "passed", "priority", "unprioritised", "failed", "skipped",
        "created", "reopened", "unordered")}
    menus: dict[str, list[dict]] = {}
    known: dict[str, dict[str, dict]] = {}  # repo -> every milestone by title, open or closed
    priorities: dict[str, dict | None] = {}  # owner -> the priority keys it can take, if any
    # (repo, number) -> (milestone title, PR number) for issues a PR carried with it this
    # run: the issue walk fetched its list before those writes, so it would offer them again.
    carried: dict[tuple[str, int], tuple[str, int]] = {}

    def set_priorities(owner: str, priority: dict | None, issue: dict, key: str) -> None:
        """Priority is an organisation issue field, and pull requests haven't got fields:
        a PR's priority goes onto the open issues it closes, which are the work."""
        name = PRIORITY_KEYS[key]
        ref = f"{issue['repo']}#{issue['number']}"
        if priority is None:
            print(f"  (no Priority field for {owner}; milestone set, priority not)")
            done["unprioritised"].append((ref, f"no Priority field for {owner}"))
            return
        # The field and its option ids are the organisation's, so only an issue of the same
        # owner can take them.
        targets = ([(ref, issue["id"])] if not issue["pr"] else
                   [(f"{c['repo']}#{c['number']}", c["id"]) for c in issue.get("closes") or []
                    if c["state"] == "OPEN" and c["repo"].split("/")[0].lower() == owner.lower()])
        if not targets:
            print(f"  (a PR has no fields, and this one closes no open issue of {owner}'s; "
                  "nothing to prioritise)")
            done["unprioritised"].append(
                (ref, f"a PR has no fields, and it closes no open issue of {owner}'s"))
            return
        field_id, option_id = priority[key]
        for target, node_id in targets:
            try:
                set_priority(node_id, field_id, option_id)
            except SystemExit as exit:
                print(f"  {exit}")
                # Its first line: the rest is gh's stderr, already printed above.
                done["failed"].append((target, str(exit).splitlines()[0]))
                continue
            print(f"  → also {target} priority {name}" if issue["pr"]
                  else f"  → priority {name}")
            done["priority"].append((target, name))

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
            owner = repo.split("/")[0]
            if owner not in priorities:
                priorities[owner] = fetch_priority_field(owner)
            priority = priorities[owner]

            marker = f"  {item_marker(issue)}" if issue["pr"] else ""
            print(f"\n[{n}/{len(issues)}] {repo}#{issue['number']}{marker}"
                  f"  (updated {issue['updated'][:10]})")
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
                print(f"  {i}) {title} ({m['open_issues']} open{due})")
            if not choices:
                print(f"  (no open milestones in {repo} — run: milestones setup {repo})")
            if priority:
                print("  priority: " + "  ".join(f"{k} {name}"
                                                 for k, name in PRIORITY_KEYS.items()))

            assign = f"[1-{len(choices)}] assign, " if choices else ""
            hint = "  (add ! + - for priority)" if priority else ""
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
                        # After the milestone, so a failed field write can't lose the assignment.
                        set_priorities(owner, priority, issue, suffix)
                    # A PR is triaged for the issues it closes too: an open one with no
                    # milestone goes where the PR goes, and one already placed is left alone.
                    for c in issue.get("closes") or []:
                        if carries(c, repo):
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
        print("\n".join(triage_summary(scope, len(issues), done, config["buckets"], rerun)))


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
    # --priority is the walk's ! + - for a pipeline: the organisation's Priority field,
    # resolved per owner before any write, and skipped with a word where there is none.
    priority: dict[str, tuple[str, str] | None] = {}
    if args.priority:
        key = {name.lower(): k for k, name in PRIORITY_KEYS.items()}[args.priority]
        for owner in {repo.split("/")[0] for repo in targets}:
            options = fetch_priority_field(owner)
            priority[owner] = options and options[key]
            if not options:
                print(f"skipping priority for {owner}: no Priority field with Urgent, High "
                      f"and Low options")
    # A PR stands in for the issues it closes, as in the walk: `carries` says which go onto
    # its milestone with it, and its priority lands on the open ones its owner can take.
    # Resolved before the prompt, so the count asked about is the count written.
    items = [{"repo": r, "number": n, "pr": True} for r, n in todo]
    fetch_closing_issues(items)
    closes = {(i["repo"], i["number"]): i["closes"] for i in items}
    carry: dict[tuple[str, int], int] = {}  # (repo, issue) -> the PR that closes it
    for (repo, number), linked in closes.items():
        for c in linked:
            if carries(c, repo) and (repo, c["number"]) not in closes:
                carry.setdefault((repo, c["number"]), number)
    print(f"{len(todo)} issue{'s' if len(todo) != 1 else ''} across {len(targets)} "
          f"repo{'s' if len(targets) != 1 else ''} → '{args.milestone}'"
          + (f", and {plural(len(carry), 'issue')} their PRs close" if carry else "")
          + (f", priority {PRIORITY_KEYS[key]}" if args.priority else ""))
    # ponytail: a pipe is the confirmation — the refs were picked in fzf or listed by a
    # script, there is no tty to answer from, and a milestone is a reversible field that
    # never closes or deletes anything.
    if sys.stdin.isatty() and ask("Proceed? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")
    for repo, number in todo:
        issue = gh.api(f"repos/{repo}/issues/{number}", method="PATCH", milestone=targets[repo])
        owner = repo.split("/")[0]
        field = priority.get(owner)
        # After the milestone, so a failed field write can't lose the assignment.
        if field and "pull_request" not in issue:
            set_priority(issue["node_id"], *field)
            print(f"  {repo}#{number} → {args.milestone}, priority {PRIORITY_KEYS[key]}")
            continue
        print(f"  {repo}#{number} → {args.milestone}")
        if field:
            # A PR has no fields: its priority goes onto the open issues it closes.
            prioritised = [c for c in closes[repo, number] if c["state"] == "OPEN"
                           and c["repo"].split("/")[0].lower() == owner.lower()]
            for c in prioritised:
                set_priority(c["id"], *field)
                print(f"    {c['repo']}#{c['number']} priority {PRIORITY_KEYS[key]} "
                      f"(closed by #{number})")
            if not prioritised:
                print(f"    (a PR has no fields, and #{number} closes no open issue of "
                      f"{owner}'s; priority not set)")
    for (repo, number), pr in carry.items():
        gh.api(f"repos/{repo}/issues/{number}", method="PATCH", milestone=targets[repo])
        print(f"  {repo}#{number} → {args.milestone} (closed by #{pr})")


def cmd_prs(config, args):
    prs = []
    for item in gh.search_issues(prs_query(args.assigned, args.review_requested, args.mentions)):
        repo = "/".join(item["repository_url"].split("/")[-2:])
        prs.append({"repo": repo, "number": item["number"], "title": item["title"],
                    "draft": item["draft"], "updated": item["updated_at"],
                    "milestone": item["milestone"] and item["milestone"]["title"],
                    "url": item["html_url"], "group": pr_group(repo, config)})
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


def collect_findings(config) -> list[dict]:
    today = datetime.date.today().isoformat()
    repos, milestones = fetch_milestones(config["repos"], closed=True)
    # Closed titles too: a title is unique across open and closed milestones, so a rename
    # onto a closed bucket's title would 422 just as onto an open one's.
    seen: dict[str, set] = {r: set() for r in repos}
    for repo, m in milestones:
        seen[repo].add(m["title"])
    # One answer per repo, settled before any finding is built: which standing buckets
    # a milestone here could be renamed onto, and none for a repo that uses none of them.
    free = {repo: free_buckets(config["buckets"], titles) for repo, titles in seen.items()}

    findings = []
    for repo, m in milestones:
        for kind, detail in milestone_problems(m["title"], m["dueOn"] and m["dueOn"][:10],
                                               m["open"]["totalCount"], m["closed"]["totalCount"],
                                               config["buckets"], today,
                                               m["openPrs"]["totalCount"],
                                               m["closedPrs"]["totalCount"],
                                               closed=m["state"] == "CLOSED"):
            findings.append({"kind": kind, "repo": repo, "title": m["title"], "detail": detail,
                             "url": m["url"], "number": m["number"],
                             "free_buckets": free[repo],
                             "issues": m["open"]["totalCount"] + m["closed"]["totalCount"],
                             "prs": m["openPrs"]["totalCount"] + m["closedPrs"]["totalCount"]})
    findings.sort(key=lambda f: (list(KINDS).index(f["kind"]), f["repo"], f["title"]))
    return findings


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


def main():
    parser = argparse.ArgumentParser(prog="milestones", description=__doc__)
    sub = parser.add_subparsers()
    # Bare `milestones` is `milestones status`, so it needs that command's defaults too.
    parser.set_defaults(func=cmd_status, json=False)

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
    triage.set_defaults(func=cmd_triage)
    assign = sub.add_parser("assign", help="put issues on a milestone by title, across repos")
    assign.add_argument("milestone", metavar="MILESTONE",
                        help="milestone title, resolved in each issue's repo")
    assign.add_argument("refs", metavar="REF", nargs="*",
                        help="OWNER/NAME#N or an issue URL; none, to read them from stdin")
    assign.add_argument("--priority", choices=[n.lower() for n in PRIORITY_KEYS.values()],
                        help="also set the organisation's Priority field on each issue")
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
    prs = sub.add_parser("prs", help="every open PR of yours, anywhere, grouped by whether "
                                     "its repo is tracked, ignored, or neither")
    prs.add_argument("--assigned", action="store_true", help="also PRs assigned to you")
    prs.add_argument("--review-requested", action="store_true",
                     help="also PRs waiting on your review")
    prs.add_argument("--mentions", action="store_true", help="also PRs that mention you")
    prs.add_argument("--json", action="store_true",
                     help="print the PRs as JSON instead of tables")
    prs.set_defaults(func=cmd_prs)

    args = parser.parse_args()
    args.func(load_config(), args)


if __name__ == "__main__":
    main()
