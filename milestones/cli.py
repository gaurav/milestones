"""milestones: view and manage GitHub milestones across all my repositories."""

import argparse
import datetime
import os
import sys
import tomllib
import webbrowser
from pathlib import Path

from . import gh

EXAMPLE_CONFIG = """\
buckets = ["Soon", "Later", "Not urgent"]
repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]
"""


def load_config() -> dict:
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "milestones.toml"
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"No config found at {path}. Create it; for example:\n\n{EXAMPLE_CONFIG}")
    config.setdefault("buckets", ["Soon", "Later", "Not urgent"])
    if not config.get("repos"):
        sys.exit(f"Config {path} has no repos. Add some; for example:\n\n{EXAMPLE_CONFIG}")
    bad = [r for r in config["repos"] if r.count("/") != 1 or not all(r.split("/"))]
    if bad:
        sys.exit(f"Config {path}: these are not OWNER/NAME: {', '.join(bad)}")
    return config


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


def build_search_query(repos: list[str]) -> str:
    # Advanced search ANDs repeated qualifiers, so owners must be OR'd explicitly.
    owners = " OR ".join(f"user:{o}" for o in owners_of(repos))
    return f"is:issue is:open no:milestone archived:false ({owners})"


def excerpt(body: str | None, width: int = 200) -> str:
    text = " ".join((body or "").split())
    return text[:width] + ("…" if len(text) > width else "")


def print_table(rows: list[tuple], headers: tuple):
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


# --- commands ---------------------------------------------------------------


def cmd_status(config, args):
    # ponytail: first 50 open milestones per repo, paginate if a repo exceeds it.
    fragment = (
        "fragment ms on Repository { nameWithOwner "
        "milestones(states: OPEN, first: 50) { nodes { "
        "title dueOn issues(states: OPEN) { totalCount } } } }"
    )
    aliases = " ".join(
        f'r{i}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") {{ ...ms }}'
        for i, r in enumerate(config["repos"])
    )
    data = gh.graphql(f"{fragment}\nquery {{ {aliases} }}")

    today = datetime.date.today().isoformat()
    milestones = []
    for repo_data in data.values():
        for m in repo_data["milestones"]["nodes"]:
            due = m["dueOn"][:10] if m["dueOn"] else None
            milestones.append((repo_data["nameWithOwner"], m["title"], due,
                               m["issues"]["totalCount"]))
    milestones.sort(key=lambda m: sort_key(m[1], m[2], config["buckets"]))

    rows = []
    for repo, title, due, count in milestones:
        flags = " ".join(filter(None, ["!OVERDUE" if due and due < today else "",
                                       "(empty)" if count == 0 else ""]))
        rows.append((repo, title, due or "—", count, flags))
    if rows:
        print_table(rows, ("REPO", "MILESTONE", "DUE", "OPEN", ""))
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

    raw = gh.api(f"repos/{args.repo}/issues?milestone={src['number']}&state=open&per_page=100",
                 paginate=True)
    issues = [i for i in raw if "pull_request" not in i]
    open_prs = len(raw) - len(issues)
    if issues:
        for issue in issues:
            print(f"  #{issue['number']} {issue['title']}")
        answer = ask(f"Move {len(issues)} open issues from '{args.src}' to '{args.dst}' "
                     f"in {args.repo}? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Aborted.")
        for issue in issues:
            gh.api(f"repos/{args.repo}/issues/{issue['number']}", method="PATCH",
                   milestone=dst["number"])
            print(f"moved #{issue['number']}")
    else:
        print(f"No open issues in '{args.src}'.")

    if args.close:
        # Not re-reading the milestone's open_issues: it lags writes, and it counts
        # the PRs we deliberately leave alone. We moved every open issue we saw, so
        # only those PRs can stand in the way.
        if open_prs:
            sys.exit(f"Not closing '{args.src}': {open_prs} open pull request(s) still on it. "
                     f"Move them by hand, or close it in the web UI.")
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
        configured = set(config["repos"])
        issues = []
        for item in gh.search_issues(build_search_query(config["repos"])):
            repo = "/".join(item["repository_url"].split("/")[-2:])
            if repo in configured:
                issues.append(_norm_issue(item, repo))

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
            due = f" (due {m['due_on'][:10]})" if m["due_on"] else ""
            print(f"  {i}) {m['title']}{due}")
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
    sub = parser.add_subparsers(dest="command", required=True)

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
    sub.add_parser("discover", help="repos with issues/milestones missing from the config")

    args = parser.parse_args()
    config = load_config()
    {"status": cmd_status, "triage": cmd_triage, "rollover": cmd_rollover,
     "setup": cmd_setup, "discover": cmd_discover}[args.command](config, args)


if __name__ == "__main__":
    main()
