"""milestones: view and manage GitHub milestones across all my repositories."""

import argparse
import datetime
import os
import re
import sys
import termios
import textwrap
import tomllib
import tty
import webbrowser
from collections import Counter
from pathlib import Path

from . import gh

DEFAULT_BUCKETS = ["Needed soon", "Needed later", "Not urgent", "Upstream"]

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
    # An empty ignore list is the normal state; an empty repos list is not.
    config.setdefault("ignore", [])
    if not config.get("repos"):
        sys.exit(f"Config {path} has no repos. Add some; for example:\n\n{EXAMPLE_CONFIG}")
    bad = [r for r in config["repos"] + config["ignore"]
           if r.count("/") != 1 or not all(r.split("/"))]
    if bad:
        sys.exit(f"Config {path}: these are not OWNER/NAME: {', '.join(bad)}")
    return config


REPO_RE = re.compile(r"(?:(?:https?://)?github\.com/)?([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


# "v1.2", "Babel v1.19" — and dated releases, "2026aug24" or "Week ending 2026-08-25".
VERSION_RE = re.compile(r"\bv\d+(\.\d+)*\b", re.I)
DATE_RE = re.compile(r"\b\d{4}(-\d{2}-\d{2}|[a-z]{3}\d{1,2})\b", re.I)


# Each kind is one shape of fix, and heads its own group in the report.
KINDS = {
    "rename": "Rename — no version or date in the title, and not a standing bucket",
    "undated": "Set a due date — however far out; an undated milestone never comes due",
    "overdue": "Roll over or re-date — past due with work still open",
    "done": "Close — every issue on it is closed",
    "empty": "Delete or fill — nothing has ever been filed against it",
    "buckets": "Run setup — the repo uses standing buckets but is missing these",
}


def milestone_problems(title: str, due: str | None, open_issues: int, closed_issues: int,
                       buckets: list[str], today: str) -> list[tuple[str, str]]:
    """(kind, detail) for everything wrong with one open milestone."""
    problems = []
    if due and due < today and open_issues:
        problems.append(("overdue", f"due {due}, {open_issues} still open"))
    if title in buckets:
        # A standing bucket is meant to be undated, named for itself, and empty
        # between triage rounds — and closing one would hide it from status and
        # triage. Running late is the only thing that can be wrong with one.
        return problems
    if not (VERSION_RE.search(title) or DATE_RE.search(title)):
        problems.append(("rename", ""))
    if not due:
        problems.append(("undated", ""))
    if closed_issues and not open_issues:
        problems.append(("done", f"all {closed_issues} closed"))
    if not closed_issues and not open_issues:
        problems.append(("empty", ""))
    return problems


def missing_buckets(buckets: list[str], titles: set[str]) -> list[str]:
    """The standing buckets a repo has adopted but does not have.

    Using none of them is a choice — most repos don't need this much triage — so a
    missing bucket is only a gap once at least one of the others is there to be
    incomplete. Doubles as the list of titles a milestone can be renamed onto: the
    buckets the repo already has are exactly the ones a rename would collide with.
    """
    missing = [b for b in buckets if b not in titles]
    return missing if missing != buckets else []


def parse_repo(text: str) -> str:
    """OWNER/NAME from either that or a github.com URL."""
    match = REPO_RE.fullmatch(text.strip())
    if not match:
        sys.exit(f"Not a repo: {text!r}. Give OWNER/NAME or a github.com URL.")
    return f"{match[1]}/{match[2]}"


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
        text = text.rstrip("\n") + f"\n\n{block}\n"
    path.write_text(text)


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")


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


def build_search_queries(repos: list[str], cap: int = 256) -> list[str]:
    """Queries covering every configured repo, each under GitHub's 256-char cap.

    Scoped by repo rather than by owner: an owner's unconfigured repos would
    otherwise crowd real results out of the single page search_issues fetches.
    """
    def query(batch):
        # Advanced search ANDs repeated qualifiers, so repos must be OR'd explicitly.
        return "is:issue is:open no:milestone archived:false (%s)" % (
            " OR ".join("repo:" + r for r in batch))

    queries, batch = [], []
    for repo in sorted(set(repos)):
        if batch and len(query(batch + [repo])) > cap:
            queries.append(query(batch))
            batch = []
        batch.append(repo)
    return queries + [query(batch)]


def excerpt(body: str | None, width: int = 200) -> str:
    return textwrap.shorten(body or "", width, placeholder=" …")


def print_table(rows: list[tuple], headers: tuple):
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


# --- commands ---------------------------------------------------------------


def after(cursor: str | None) -> str:
    """The `after:` argument for a paginated connection, empty for the first page."""
    return f', after: "{cursor}"' if cursor else ""


def fetch_milestones(repos: list[str]) -> tuple[list[str], list[tuple[str, dict]]]:
    """GitHub's own name for each repo, and (repo, milestone) for every open
    milestone. The names follow renames and fix up the config's capitalisation,
    so callers should key off them, not off `repos`.

    One aliased round trip per page: every repo is queried together, and only the
    repos that still have milestones left go into the next trip, so the usual case
    of nobody being near a full page costs exactly one query.
    """
    names: dict[str, str] = {}
    milestones = []
    pages: dict[str, str | None] = {r: None for r in repos}  # repo -> next page's cursor
    while pages:
        alias_of = {f"r{i}": repo for i, repo in enumerate(pages)}
        aliases = " ".join(
            f'{alias}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") '
            f"{{ nameWithOwner milestones(states: OPEN, first: 100{after(pages[r])}) "
            f"{{ pageInfo {{ hasNextPage endCursor }} "
            f"nodes {{ title url dueOn number "
            f"open: issues(states: OPEN) {{ totalCount }} "
            f"closed: issues(states: CLOSED) {{ totalCount }} }} }} }}"
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


def cmd_status(config, args):
    today = datetime.date.today().isoformat()
    milestones = []
    tracked, entries = fetch_milestones(config["repos"])
    for repo, m in entries:
        due = m["dueOn"][:10] if m["dueOn"] else None
        milestones.append((repo, m["title"], due, m["open"]["totalCount"],
                           m["closed"]["totalCount"], m["url"]))
    milestones.sort(key=lambda m: sort_key(m[1], m[2], config["buckets"]))

    rows = []
    for repo, title, due, count, closed, url in milestones:
        # `check` owns what is wrong with a milestone; status shows the three of its
        # kinds that read as a state the milestone is in rather than a fix to make,
        # and so agrees with `check` about standing buckets and about a past-due
        # milestone with nothing left open. Empty means nothing was ever filed on it,
        # not "all done".
        kinds = {kind for kind, _ in milestone_problems(title, due, count, closed,
                                                        config["buckets"], today)}
        flags = " ".join(flag for kind, flag in (("overdue", "!OVERDUE"),
                                                 ("empty", "(empty)"), ("done", "(done)"))
                         if kind in kinds)
        rows.append((repo, title, due or "—", count, closed, flags, url))
    if rows:
        print_table(rows, ("REPO", "MILESTONE", "DUE", "OPEN", "DONE", "", "URL"))
    # A tracked repo with no open milestone has no row of its own, and so is invisible
    # here unless it is named; `discover` lists the config's repos in full.
    quiet = sorted(set(tracked) - {m[0] for m in milestones})
    if quiet:
        if rows:
            print()
        print(f"{len(quiet)} tracked repo{'s' if len(quiet) != 1 else ''} "
              f"with no open milestones:")
        print_table([(r, repo_url(r)) for r in quiet], ("REPO", "URL"))
    elif not rows:
        print("No open milestones in any configured repo.")


def _milestones_by_title(repo: str, state: str = "all") -> dict[str, dict]:
    return {m["title"]: m
            for m in gh.api(f"repos/{repo}/milestones?state={state}&per_page=100",
                            paginate=True)}


def cmd_setup(config, args):
    existing = _milestones_by_title(args.repo)
    for bucket in config["buckets"]:
        milestone = existing.get(bucket)
        if milestone is None:
            gh.api(f"repos/{args.repo}/milestones", method="POST", title=bucket)
            print(f"created:  {bucket}")
        elif milestone["state"] != "open":
            # A closed bucket is invisible to status and the triage menu, so
            # "it exists" is not good enough for a repair command.
            gh.api(f"repos/{args.repo}/milestones/{milestone['number']}", method="PATCH",
                   state="open")
            print(f"reopened: {bucket}")
        else:
            print(f"exists:   {bucket}")


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
    return {"repo": repo, "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "labels": [l["name"] for l in issue["labels"]],
            "updated": issue["updated_at"], "url": issue["html_url"]}


def cmd_triage(config, args):
    if args.repo:
        raw = gh.api(f"repos/{args.repo}/issues?milestone=none&state=open&per_page=100",
                     paginate=True)
        issues = [_norm_issue(i, args.repo) for i in raw if "pull_request" not in i]
    else:
        issues = []
        for query in build_search_queries(config["repos"]):
            for item in gh.search_issues(query):
                repo = "/".join(item["repository_url"].split("/")[-2:])
                issues.append(_norm_issue(item, repo))
        issues.sort(key=lambda i: i["updated"], reverse=True)

    if not issues:
        print("Nothing to triage.")
        return
    menus: dict[str, list[dict]] = {}
    for n, issue in enumerate(issues, 1):
        repo = issue["repo"]
        if repo not in menus:
            menus[repo] = sorted(
                _milestones_by_title(repo, state="open").values(),
                key=lambda m: sort_key(m["title"], m["due_on"], config["buckets"]))
        choices = menus[repo]

        print(f"\n[{n}/{len(issues)}] {repo}#{issue['number']}  (updated {issue['updated'][:10]})")
        print(f"  {issue['title']}")
        if issue["labels"]:
            print(f"  labels: {', '.join(issue['labels'])}")
        if issue["body"]:
            print(f"  > {excerpt(issue['body'])}")
        for i, m in enumerate(choices, 1):
            due = f", due {m['due_on'][:10]}" if m["due_on"] else ""
            # REST open_issues counts PRs as well as issues, which is what we want here:
            # both are work sitting on that milestone.
            print(f"  {i}) {m['title']} ({m['open_issues']} open{due})")
        if not choices:
            print(f"  (no open milestones in {repo} — run: milestones setup {repo})")

        assign = f"[1-{len(choices)}] assign, " if choices else ""
        while True:
            answer = ask(f"  {assign}s skip, o open, q quit: ").strip().lower()
            if answer == "q":
                return
            if answer == "s" or (answer == "" and not choices):
                break
            if answer == "o":
                webbrowser.open(issue["url"])
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                chosen = choices[int(answer) - 1]
                gh.api(f"repos/{repo}/issues/{issue['number']}", method="PATCH",
                       milestone=chosen["number"])
                print(f"  → {chosen['title']}")
                break
            print(f"  Not one of: {assign}s, o, q.")


def issue_count(issues: int | None) -> str:
    """"(3 issues)", and nothing at all where a count makes no sense."""
    if issues is None:
        return ""
    return f"({issues} issue{'' if issues == 1 else 's'})"


def collect_findings(config) -> list[dict]:
    today = datetime.date.today().isoformat()
    repos, milestones = fetch_milestones(config["repos"])
    seen: dict[str, set] = {r: set() for r in repos}
    for repo, m in milestones:
        seen[repo].add(m["title"])
    # One answer per repo, settled before any finding is built: which standing buckets
    # this repo is short of, and empty for a repo that uses none of them.
    free = {repo: missing_buckets(config["buckets"], titles) for repo, titles in seen.items()}

    findings = []
    for repo, m in milestones:
        for kind, detail in milestone_problems(m["title"], m["dueOn"] and m["dueOn"][:10],
                                               m["open"]["totalCount"], m["closed"]["totalCount"],
                                               config["buckets"], today):
            findings.append({"kind": kind, "repo": repo, "title": m["title"], "detail": detail,
                             "url": m["url"], "number": m["number"],
                             "free_buckets": free[repo],
                             "issues": m["open"]["totalCount"] + m["closed"]["totalCount"]})
    for repo, missing in free.items():
        if missing:
            findings.append({"kind": "buckets", "repo": repo, "title": "(whole repo)",
                             "detail": ", ".join(missing), "issues": None,
                             "free_buckets": missing,
                             "url": f"https://github.com/{repo}/milestones", "number": None})
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
        counts = [issue_count(f["issues"]) for f in group]
        count_w = max(len(c) for c in counts)
        for count, f in zip(counts, group):
            line = "  • %s  %s  %s  %s" % (f["repo"].ljust(repo_w), f["title"].ljust(title_w),
                                           count.ljust(count_w), f["detail"])
            print("%s\n    %s" % (line.rstrip(), f["url"]))


def cmd_check(config, args):
    findings = collect_findings(config)
    if not findings:
        print("Nothing to fix — every open milestone is named and dated sensibly.")
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
        count = issue_count(f["issues"])
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
        if kind == "done":
            options.append(("c", "close it", close_it))
        if kind == "empty":
            options.append(("d", "delete it", delete_it))
        if kind == "buckets":
            options.append(("b", "create the missing buckets",
                            lambda: cmd_setup(config, argparse.Namespace(repo=repo))))
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


def cmd_ignore(config, args):
    # No API round trip to canonicalise the name, unlike `add`: ignoring is not tracking,
    # a typo costs nothing but a repo staying visible, and the first pass through
    # `discover`'s output is dozens of repos at once. Case is handled where they're
    # compared instead.
    ignore = list(config["ignore"])
    for text in args.repos:
        repo = parse_repo(text)
        if repo.lower() in {r.lower() for r in config["repos"]}:
            print(f"tracked, not ignored: {repo} — `milestones remove {repo}` first")
        elif repo.lower() in {r.lower() for r in ignore}:
            print(f"already ignored: {repo}")
        else:
            ignore.append(repo)
            print(f"ignoring {repo}")
    if ignore != config["ignore"]:
        write_repo_list(config_path(), "ignore", ignore)


def cmd_discover(config, args):
    # Straight from the config, before any API call: it prints instantly, and it still
    # prints if the search below dies on an owner that no longer exists.
    print_table([(r, repo_url(r)) for r in config["repos"]], ("TRACKED REPO", "URL"))
    if args.tracked_only:
        return
    print()
    # Both lists are typed by hand while GitHub answers with the canonical spelling, so
    # compare in lower case — `remove` already does.
    configured = {r.lower() for r in config["repos"]}
    ignored = {r.lower() for r in config["ignore"]}
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
                if name.lower() in ignored:
                    hidden.add(name)
                else:
                    rows.add((issues, ms, name))
            if not repositories["pageInfo"]["hasNextPage"]:
                break
            cursor = repositories["pageInfo"]["endCursor"]
    if rows:
        print_table([(name, issues, ms, repo_url(name))
                     for issues, ms, name in sorted(rows, reverse=True)],
                    ("REPO NOT IN CONFIG", "OPEN ISSUES", "OPEN MILESTONES", "URL"))
    elif not hidden:
        # Only true if nothing was hidden either: repos left out on purpose are not repos
        # the config covers.
        print("Nothing new — the config covers every repo found.")
    if hidden:
        if rows:
            print()
        print(f"Found {len(hidden)} ignored repositor{'y' if len(hidden) == 1 else 'ies'}; "
              f"use `milestones add` to explicitly add them or delete them from the "
              f"ignore list at {config_path()}.")


def main():
    parser = argparse.ArgumentParser(prog="milestones", description=__doc__)
    sub = parser.add_subparsers()
    parser.set_defaults(func=cmd_status)  # bare `milestones` is `milestones status`

    sub.add_parser("status", help="all open milestones across configured repos, by due date"
                   ).set_defaults(func=cmd_status)
    triage = sub.add_parser("triage", help="interactively assign milestones to untriaged issues")
    triage.add_argument("--repo", metavar="OWNER/NAME", help="triage a single repo")
    triage.set_defaults(func=cmd_triage)
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
    check.add_argument("-i", "--interactive", action="store_true",
                       help="walk the findings one by one and fix them")
    check.set_defaults(func=cmd_check)
    add = sub.add_parser("add", help="track a repo (OWNER/NAME or github.com URL)")
    add.add_argument("repo", metavar="REPO")
    add.set_defaults(func=cmd_add)
    remove = sub.add_parser("remove", help="stop tracking a repo")
    remove.add_argument("repo", metavar="REPO")
    remove.set_defaults(func=cmd_remove)
    ignore = sub.add_parser("ignore", help="hide repos from discover without tracking them")
    ignore.add_argument("repos", metavar="REPO", nargs="+")
    ignore.set_defaults(func=cmd_ignore)
    discover = sub.add_parser("discover",
                              help="the tracked repos, then ones with issues/milestones "
                                   "missing from the config")
    discover.add_argument("--tracked-only", action="store_true",
                          help="just list the tracked repos; don't go looking for more")
    discover.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    args.func(load_config(), args)


if __name__ == "__main__":
    main()
