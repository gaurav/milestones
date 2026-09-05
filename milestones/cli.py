"""milestones: view and manage GitHub milestones across all my repositories."""

import argparse
import datetime
import os
import re
import sys
import termios
import tomllib
import tty
import webbrowser
from pathlib import Path

from . import gh

EXAMPLE_CONFIG = """\
buckets = ["Needed soon", "Needed later", "Not urgent", "Upstream"]
repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]
"""


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "milestones.toml"


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"No config found at {path}. Create it; for example:\n\n{EXAMPLE_CONFIG}")
    config.setdefault("buckets", ["Needed soon", "Needed later", "Not urgent", "Upstream"])
    if not config.get("repos"):
        sys.exit(f"Config {path} has no repos. Add some; for example:\n\n{EXAMPLE_CONFIG}")
    bad = [r for r in config["repos"] if r.count("/") != 1 or not all(r.split("/"))]
    if bad:
        sys.exit(f"Config {path}: these are not OWNER/NAME: {', '.join(bad)}")
    return config


REPO_RE = re.compile(r"(?:(?:https?://)?github\.com/)?([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
REPOS_BLOCK = re.compile(r"^repos\s*=\s*\[[^\]]*\]", re.M)


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
    "buckets": "Run setup — the repo is missing standing buckets",
}


def milestone_problems(title: str, due: str | None, open_issues: int, closed_issues: int,
                       buckets: list[str], today: str) -> list[tuple[str, str]]:
    """(kind, detail) for everything wrong with one open milestone."""
    problems = []
    is_bucket = title in buckets
    if not is_bucket and not (VERSION_RE.search(title) or DATE_RE.search(title)):
        problems.append(("rename", ""))
    if not is_bucket and not due:
        problems.append(("undated", ""))
    if due and due < today and open_issues:
        problems.append(("overdue", f"due {due}, {open_issues} still open"))
    if closed_issues and not open_issues and not is_bucket:
        problems.append(("done", f"all {closed_issues} closed"))
    if not closed_issues and not open_issues and not is_bucket:
        problems.append(("empty", ""))
    return problems


def parse_repo(text: str) -> str:
    """OWNER/NAME from either that or a github.com URL."""
    match = REPO_RE.fullmatch(text.strip())
    if not match:
        sys.exit(f"Not a repo: {text!r}. Give OWNER/NAME or a github.com URL.")
    return f"{match[1]}/{match[2]}"


def write_repos(path: Path, repos: list[str]) -> None:
    """Rewrite just the repos list, leaving the rest of the config alone."""
    # ponytail: comments *inside* the repos list are dropped; nothing else is touched.
    block = "repos = [\n" + "".join(f'  "{r}",\n' for r in repos) + "]"
    text, count = REPOS_BLOCK.subn(lambda _: block, path.read_text(), count=1)
    if count != 1:
        sys.exit(f"Can't find a `repos = [...]` list to edit in {path}; edit it by hand.")
    path.write_text(text)


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")


def owners_of(repos: list[str]) -> list[str]:
    return sorted({r.split("/")[0] for r in repos})


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
    text = " ".join((body or "").split())
    return text[:width] + ("…" if len(text) > width else "")


def print_table(rows: list[tuple], headers: tuple):
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


# --- commands ---------------------------------------------------------------


def fetch_milestones(repos: list[str], fields: str) -> tuple[list[str], list[tuple[str, dict]]]:
    """GitHub's own name for each repo, and (repo, milestone) for every open
    milestone, in one GraphQL round trip. The names follow renames and fix up
    the config's capitalisation, so callers should key off them, not off `repos`."""
    # ponytail: first 50 open milestones per repo, paginate if a repo exceeds it.
    fragment = ("fragment ms on Repository { nameWithOwner "
                f"milestones(states: OPEN, first: 50) {{ nodes {{ title url dueOn {fields} }} }} }}")
    aliases = " ".join(
        f'r{i}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") {{ ...ms }}'
        for i, r in enumerate(repos)
    )
    data = gh.graphql(f"{fragment}\nquery {{ {aliases} }}")
    return ([repo["nameWithOwner"] for repo in data.values()],
            [(repo["nameWithOwner"], m)
             for repo in data.values() for m in repo["milestones"]["nodes"]])


def cmd_status(config, args):
    today = datetime.date.today().isoformat()
    milestones = []
    fields = ("open: issues(states: OPEN) { totalCount } "
              "closed: issues(states: CLOSED) { totalCount }")
    for repo, m in fetch_milestones(config["repos"], fields)[1]:
        due = m["dueOn"][:10] if m["dueOn"] else None
        milestones.append((repo, m["title"], due, m["open"]["totalCount"],
                           m["closed"]["totalCount"], m["url"]))
    milestones.sort(key=lambda m: sort_key(m[1], m[2], config["buckets"]))

    rows = []
    for repo, title, due, count, closed, url in milestones:
        flags = " ".join(filter(None, ["!OVERDUE" if due and due < today else "",
                                       # Empty means nothing was ever filed, not "all done".
                                       "(empty)" if count == 0 and closed == 0 else "",
                                       "(done)" if count == 0 and closed else ""]))
        rows.append((repo, title, due or "—", count, closed, flags, url))
    if rows:
        print_table(rows, ("REPO", "MILESTONE", "DUE", "OPEN", "DONE", "", "URL"))
    else:
        print("No open milestones in any configured repo.")


def _milestones_by_title(repo: str, state: str = "all") -> dict[str, dict]:
    return {m["title"]: m for m in gh.api(f"repos/{repo}/milestones?state={state}", paginate=True)}


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


def _milestone_menu(config, repo: str) -> list[dict]:
    open_ms = [m for m in _milestones_by_title(repo, state="open").values()]
    open_ms.sort(key=lambda m: sort_key(m["title"], m["due_on"], config["buckets"]))
    return open_ms


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
            menus[repo] = _milestone_menu(config, repo)
        choices = menus[repo]

        print(f"\n[{n}/{len(issues)}] {repo}#{issue['number']}  (updated {issue['updated'][:10]})")
        print(f"  {issue['title']}")
        if issue["labels"]:
            print(f"  labels: {', '.join(issue['labels'])}")
        if issue["body"]:
            print(f"  > {excerpt(issue['body'])}")
        for i, m in enumerate(choices, 1):
            due = f", due {m['due_on'][:10]}" if m["due_on"] else ""
            # REST open_issues counts PRs too, so this is a rough "how loaded is it" signal.
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
    fields = ("number open: issues(states: OPEN) { totalCount } "
              "closed: issues(states: CLOSED) { totalCount }")
    repos, milestones = fetch_milestones(config["repos"], fields)
    seen: dict[str, set] = {r: set() for r in repos}
    findings = []
    for repo, m in milestones:
        seen[repo].add(m["title"])
        for kind, detail in milestone_problems(m["title"], m["dueOn"] and m["dueOn"][:10],
                                               m["open"]["totalCount"], m["closed"]["totalCount"],
                                               config["buckets"], today):
            findings.append({"kind": kind, "repo": repo, "title": m["title"], "detail": detail,
                             "url": m["url"], "number": m["number"],
                             "issues": m["open"]["totalCount"] + m["closed"]["totalCount"]})
    for repo, titles in seen.items():
        missing = [b for b in config["buckets"] if b not in titles]
        if missing:
            findings.append({"kind": "buckets", "repo": repo, "title": "(whole repo)",
                             "detail": ", ".join(missing), "issues": None,
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
    if not typed:
        return None
    return max(set(typed), key=lambda d: (typed.count(d), len(typed) - typed[::-1].index(d)))


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


def walk_findings(config, findings: list[dict]) -> None:
    today = datetime.date.today()
    typed: list[datetime.date] = []  # session-only; the "again" slot follows these
    for n, f in enumerate(findings, 1):
        dates = date_choices(today, favourite_date(typed))
        repo, number, kind = f["repo"], f["number"], f["kind"]
        detail = f"  ({f['detail']})" if f["detail"] else ""
        count = issue_count(f["issues"])
        print(f"\n[{n}/{len(findings)}] {kind}: {repo}  {f['title']}"
              f"{'  ' + count if count else ''}{detail}")
        print(f"  {f['url']}")

        def patch(**fields):
            gh.api(f"repos/{repo}/milestones/{number}", method="PATCH", **fields)

        options = []
        if kind == "rename":
            options += [(str(i), f"→ {b}") for i, b in enumerate(config["buckets"], 1)]
            options.append(("r", "rename to…"))
        if kind in ("undated", "overdue"):
            options += [(key, f"{label} {date.isoformat()}") for key, label, date in dates]
            options.append(("e", "another date…"))
        if kind == "overdue":
            options.append(("r", "roll its issues over…"))
        if kind == "done":
            options.append(("c", "close it"))
        if kind == "empty":
            options.append(("d", "delete it"))
        if kind == "buckets":
            options.append(("b", "create the missing buckets"))
        options += [("o", "open"), ("s", "skip"), ("q", "quit")]

        prompt = "  " + "  ".join(f"[{key}] {label}" for key, label in options) + "  "
        while True:
            key = read_key(prompt).lower()
            if key == "q":
                return
            if key == "s":
                break
            if key == "o":
                webbrowser.open(f["url"])
                continue
            # Anything needing more than a keypress asks a second question; blank skips.
            if key == "r" and kind == "rename":
                title = ask("  new title (blank to skip): ").strip()
                if title:
                    patch(title=title)
                    print(f"  → renamed to '{title}'")
                break
            if key == "r" and kind == "overdue":
                dst = ask("  roll its open issues onto which milestone? (blank to skip) ").strip()
                if dst:
                    cmd_rollover(config, argparse.Namespace(repo=repo, src=f["title"], dst=dst,
                                                            close=False))
                break
            if key == "e" and kind in ("undated", "overdue"):
                answer = ask("  due date, YYYY-MM-DD (blank to skip): ").strip()
                if not answer:
                    break
                try:
                    chosen = datetime.date.fromisoformat(answer)
                except ValueError:
                    print("  Not a YYYY-MM-DD date.")
                    continue
                typed.append(chosen)
                set_due(repo, number, chosen)
                break
            if key == "d" and kind == "empty":
                if ask(f"  delete '{f['title']}' from {repo}? [y/N] ").strip().lower() == "y":
                    gh.api(f"repos/{repo}/milestones/{number}", method="DELETE")
                    print("  → deleted")
                    break
                continue
            if key == "c" and kind == "done":
                patch(state="closed")
                print("  → closed")
                break
            if key == "b" and kind == "buckets":
                cmd_setup(config, argparse.Namespace(repo=repo))
                break
            if kind == "rename" and key.isdigit() and 1 <= int(key) <= len(config["buckets"]):
                title = config["buckets"][int(key) - 1]
                patch(title=title)
                print(f"  → renamed to '{title}'")
                break
            if kind in ("undated", "overdue"):
                chosen = next((d for k, _, d in dates if k == key), None)
                if chosen:
                    set_due(repo, number, chosen)
                    break
            print("  Not one of those.")


def cmd_add(config, args):
    # The API answer normalises case and follows renames, and 404s on a typo.
    repo = gh.api(f"repos/{parse_repo(args.repo)}")["full_name"]
    if repo in config["repos"]:
        print(f"already tracked: {repo}")
        return
    write_repos(config_path(), config["repos"] + [repo])
    print(f"tracking {repo} — run `milestones setup {repo}` to create its buckets")


def cmd_remove(config, args):
    repo = parse_repo(args.repo)
    keep = [r for r in config["repos"] if r.lower() != repo.lower()]
    if len(keep) == len(config["repos"]):
        sys.exit(f"Not tracked: {repo}. Tracked: {', '.join(config['repos'])}")
    if not keep:
        sys.exit(f"{repo} is the only tracked repo; a config with none is rejected on load.")
    write_repos(config_path(), keep)
    print(f"stopped tracking {repo}")


def cmd_discover(config, args):
    configured = set(config["repos"])
    rows = set()  # transferred repos can echo under their old owner; dedupe
    for owner in owners_of(config["repos"]):
        # ponytail: first 100 repos per owner, add pagination when an owner exceeds it.
        data = gh.graphql(
            f'query {{ repositoryOwner(login: "{owner}") {{ '
            f"repositories(first: 100, isFork: false, ownerAffiliations: OWNER, "
            f"orderBy: {{field: PUSHED_AT, direction: DESC}}) {{ nodes {{ "
            f"nameWithOwner isArchived issues(states: OPEN) {{ totalCount }} "
            f"milestones(states: OPEN) {{ totalCount }} }} }} }} }}"
        )
        if data["repositoryOwner"] is None:
            sys.exit(f"No GitHub user or organisation '{owner}' — check the repos in your config.")
        for r in data["repositoryOwner"]["repositories"]["nodes"]:
            issues, ms = r["issues"]["totalCount"], r["milestones"]["totalCount"]
            if not r["isArchived"] and r["nameWithOwner"] not in configured and (issues or ms):
                rows.add((issues, ms, r["nameWithOwner"]))
    if rows:
        print_table([(name, issues, ms) for issues, ms, name in sorted(rows, reverse=True)],
                    ("REPO NOT IN CONFIG", "OPEN ISSUES", "OPEN MILESTONES"))
    else:
        print("Nothing new — the config covers every repo found.")


def main():
    parser = argparse.ArgumentParser(prog="milestones", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(command="status")

    sub.add_parser("status", help="all open milestones across configured repos, by due date")
    triage = sub.add_parser("triage", help="interactively assign milestones to untriaged issues")
    triage.add_argument("--repo", metavar="OWNER/NAME", help="triage a single repo")
    rollover = sub.add_parser("rollover", help="move open issues from one milestone to another")
    rollover.add_argument("repo", metavar="OWNER/NAME")
    rollover.add_argument("src", metavar="FROM", help="source milestone title")
    rollover.add_argument("dst", metavar="TO", help="destination milestone title")
    rollover.add_argument("--close", action="store_true", help="close FROM once empty")
    setup = sub.add_parser("setup", help="create the standing bucket milestones in a repo")
    setup.add_argument("repo", metavar="OWNER/NAME")
    check = sub.add_parser("check",
                           help="milestones that need renaming, dating, closing or rolling over")
    check.add_argument("-i", "--interactive", action="store_true",
                       help="walk the findings one by one and fix them")
    add = sub.add_parser("add", help="track a repo (OWNER/NAME or github.com URL)")
    add.add_argument("repo", metavar="REPO")
    remove = sub.add_parser("remove", help="stop tracking a repo")
    remove.add_argument("repo", metavar="REPO")
    sub.add_parser("discover", help="repos with issues/milestones missing from the config")

    args = parser.parse_args()
    config = load_config()
    {"status": cmd_status, "triage": cmd_triage, "rollover": cmd_rollover,
     "setup": cmd_setup, "discover": cmd_discover, "add": cmd_add, "check": cmd_check,
     "remove": cmd_remove}[args.command](config, args)


if __name__ == "__main__":
    main()
