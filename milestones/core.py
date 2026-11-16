"""What the command line and the web app both stand on: the config, the conventions (which
milestones are buckets, what is wrong with a milestone), and the reads of GitHub that span
repos. Nothing here prints a table or asks a question; a failed call is gh's SystemExit."""

import datetime
import os
import re
import sys
import tomllib
from pathlib import Path

from . import gh
# The table of priority and effort labels: pure, and the board's as much as the CLI's.
from .board import reconcile

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


def load_config(needs_repos: bool = True) -> dict:
    """The config, checked. Most commands are about the repos it names and stop without
    any; the app is not, and with `needs_repos` off gets the defaults for a config that
    isn't there yet."""
    path = config_path()
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        if needs_repos:
            sys.exit(f"No config found at {path}. Create it; for example:\n\n{EXAMPLE_CONFIG}")
        config = {"repos": []}
    config.setdefault("buckets", list(DEFAULT_BUCKETS))
    # An empty ignore list is the normal state; an empty repos list is not. Focusing on
    # nothing in particular is the normal state too.
    config.setdefault("ignore", [])
    config.setdefault("focus", [])
    config.setdefault("repos", [])
    if needs_repos and not config["repos"]:
        sys.exit(f"Config {path} has no repos. Add some; for example:\n\n{EXAMPLE_CONFIG}")
    bad = [r for r in config["repos"] + config["ignore"] + config["focus"]
           if r.count("/") != 1 or not all(r.split("/"))]
    if bad:
        sys.exit(f"Config {path}: these are not OWNER/NAME: {', '.join(bad)}")
    # Owner -> colour, resolved here so `status` can hand org_colors plain numbers.
    config["colors"] = {owner: parse_color(path, owner, value)
                        for owner, value in config.get("colors", {}).items()}
    # Repo -> a label of its own -> the standard label it stands for, both keyed in lower
    # case: read as that priority or effort, and never written.
    labels = {}
    for repo, aliases in config.get("labels", {}).items():
        wrong = [f'"{name}" = "{to}"' for name, to in aliases.items()
                 if not isinstance(to, str) or to.lower() not in reconcile.STANDARD]
        if wrong:
            sys.exit(f"Config {path}: [labels.\"{repo}\"] {', '.join(wrong)}: a label stands "
                     f"for one of {', '.join(standard_labels())}.")
        labels[repo.lower()] = {name.lower(): to for name, to in aliases.items()}
    config["labels"] = labels
    return config


def standard_labels() -> list[str]:
    return [reconcile.standard_label(kind, option)
            for kind, spec in reconcile.KINDS.items() for option in spec["options"]]


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


# Names for the org colours, so a config can say "pink" rather than 218. Pastels and mid
# tones only: an owner's colour is an identity, not a rating, and it should not compete with
# the DUE and % columns for the eye.
COLOR_NAMES = {"blue": 39, "cyan": 44, "teal": 74, "indigo": 105, "violet": 141,
               "magenta": 170, "purple": 183, "yellow": 186, "green": 114, "rose": 212,
               "pink": 218, "gold": 220, "grey": 250}


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


def untriaged_counts(items: list[dict], parked: dict[str, int] | None = None) -> list[dict]:
    """Per repo, how many open issues and pull requests have no milestone, and how many sit
    on Needs triage (`parked`, repo -> count, from the milestone rows status already has) —
    only the repos that have any, by name. `repo` is GitHub's spelling, as fetch_milestones'
    names are."""
    counts: dict[str, dict] = {}
    def row(repo):
        return counts.setdefault(repo, {"repo": repo, "issues": 0, "prs": 0, "needs_triage": 0})
    for item in items:
        row(item["repo"])["prs" if item["pr"] else "issues"] += 1
    for repo, count in (parked or {}).items():
        if count:
            row(repo)["needs_triage"] = count
    return [counts[repo] for repo in sorted(counts, key=str.lower)]


def _milestones_by_title(repo: str, state: str = "all") -> dict[str, dict]:
    return {m["title"]: m
            for m in gh.api(f"repos/{repo}/milestones?state={state}&per_page=100",
                            paginate=True)}


def _norm_issue(issue: dict, repo: str) -> dict:
    # Both the REST listing and a search item carry `draft` on a pull request, and the
    # milestone it is on, if any — the walk says so when it offers an item that has one.
    milestone = issue.get("milestone")
    return {"repo": repo, "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "labels": [l["name"] for l in issue["labels"]],
            "updated": issue["updated_at"], "url": issue["html_url"], "id": issue["node_id"],
            "pr": "pull_request" in issue, "draft": bool(issue.get("draft")),
            "milestone": milestone["title"] if milestone else None}


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


def collect_findings(config, fetched=None) -> list[dict]:
    today = datetime.date.today().isoformat()
    repos, milestones = fetched or fetch_all_milestones(config)
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
                             "due": m["dueOn"] and m["dueOn"][:10],
                             "bucket": m["title"] in config["buckets"],
                             "free_buckets": free[repo],
                             "issues": m["open"]["totalCount"] + m["closed"]["totalCount"],
                             "prs": m["openPrs"]["totalCount"] + m["closedPrs"]["totalCount"]})
    findings.sort(key=lambda f: (list(KINDS).index(f["kind"]), f["repo"], f["title"]))
    return findings


def fetch_all_milestones(config: dict) -> tuple[list[str], list[tuple[str, dict]]]:
    """Every milestone of every tracked repo, open and closed: what `status_data` and
    `collect_findings` both read, for a caller that wants both from one round trip."""
    return fetch_milestones(config["repos"], closed=True) if config["repos"] else ([], [])


def status_data(config: dict, fetched=None, untriaged_items: list[dict] | None = None) -> dict:
    """Every open milestone in the tracked repos by due date, and what isn't a row: the
    tracked repos with none, the closed milestones still holding open work, and how much in
    each repo is on no milestone. The shape `status --json` printed.

    `fetched` is `fetch_all_milestones`' answer and `untriaged_items` the search for what is
    on no milestone, for a caller that has them already; each is read here otherwise."""
    today = datetime.date.today().isoformat()
    milestones = []
    stranded = []  # closed milestones with open work: not rows, but not silence either
    tracked, entries = fetched or fetch_all_milestones(config)
    for repo, m in entries:
        due = m["dueOn"][:10] if m["dueOn"] else None
        count, prs = m["open"]["totalCount"], m["openPrs"]["totalCount"]
        if m["state"] == "CLOSED":
            if count + prs:
                stranded.append({"repo": repo, "title": m["title"], "open": count,
                                 "open_prs": prs, "url": m["url"], "number": m["number"]})
            continue
        if (m["title"] in OPTIONAL_BUCKETS and m["title"] in config["buckets"]
                and not count + prs):
            continue
        milestones.append((repo, m["title"], due, count, m["closed"]["totalCount"],
                           prs, m["closedPrs"]["totalCount"], m["url"], m["number"]))
    milestones.sort(key=lambda m: sort_key(m[1], m[2], config["buckets"]))
    # A tracked repo with no open milestone has no row of its own, and so is invisible
    # here unless it is named; `discover` lists the config's repos in full.
    quiet = sorted(set(tracked) - {m[0] for m in milestones})
    # "Is everything triaged?" is the other half of the question status answers; one
    # search over every tracked repo, issues and PRs together — and what is parked on Needs
    # triage, read off the rows above rather than searched for, so it can't disagree with
    # the bucket's own row. Only where the config has the bucket: elsewhere it is a
    # milestone like any other, and `triage` wouldn't walk it.
    parked = {repo: m["open"]["totalCount"] + m["openPrs"]["totalCount"]
              for repo, m in entries
              if m["title"] == TRIAGE_BUCKET and m["state"] == "OPEN"
              and TRIAGE_BUCKET in config["buckets"]}
    if untriaged_items is None:
        untriaged_items = fetch_untriaged(config, None, kind=None) if config["repos"] else []
    untriaged = untriaged_counts(untriaged_items, parked)
    for row in untriaged:
        row["focus"] = is_focused(row["repo"], config["focus"])

    records = []
    for repo, title, due, count, closed, prs, closed_prs, url, number in milestones:
        # `milestone_problems` owns what is wrong with a milestone; a row shows the three of
        # its kinds that read as a state the milestone is in rather than a fix to make, and
        # so agrees with the findings about standing buckets and about a past-due milestone
        # with nothing left open. Empty means nothing was ever filed on it, not "all done".
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
                        "bucket": title in config["buckets"],
                        "url": url, "number": number})
    # The quiet repos in the same shape as a milestone row, so a reader doesn't have to
    # cross-reference to find out whether one is focused. The config's own list comes
    # too: it is the only place an entry naming a repo you no longer track shows up.
    return {"milestones": records,
            "quiet_repos": [{"repo": r, "focus": is_focused(r, config["focus"]),
                             "url": repo_url(r)} for r in quiet],
            "stranded": stranded, "untriaged": untriaged, "focus": config["focus"]}


def prs_data(config: dict, assigned: bool = False, review_requested: bool = False,
             mentions: bool = False) -> list[dict]:
    """Every open pull request of yours, anywhere on GitHub, each with the `group` its repo
    is in: tracked, untracked or ignored."""
    prs = []
    for item in gh.search_issues(prs_query(assigned, review_requested, mentions)):
        repo = "/".join(item["repository_url"].split("/")[-2:])
        prs.append({"repo": repo, "number": item["number"], "title": item["title"],
                    "draft": item["draft"], "updated": item["updated_at"],
                    "milestone": item["milestone"] and item["milestone"]["title"],
                    "url": item["html_url"], "group": pr_group(repo, config)})
    return prs


def rollover_plan(repo: str, src_title: str, dst_title: str, close: bool,
                  buckets: list[str]) -> tuple[dict, dict, list[dict]]:
    """Check a rollover and say what it would move: (the source milestone, the destination,
    the open issues and pull requests on the source). Exits for a title the repo hasn't got,
    a closed destination, or a `close` that would close a standing bucket."""
    by_title = _milestones_by_title(repo)
    for title in (src_title, dst_title):
        if title not in by_title:
            sys.exit(f"No milestone '{title}' in {repo}. "
                     f"Milestones: {', '.join(sorted(by_title))}")
    src, dst = by_title[src_title], by_title[dst_title]
    if dst["state"] != "open":
        sys.exit(f"'{dst_title}' is closed; issues moved onto it would disappear from both "
                 f"status and triage. Reopen it first.")
    if close and src_title in buckets:
        # Every other path refuses to close a bucket; a closing rollover was the way round it.
        sys.exit(f"'{src_title}' is a standing bucket, so it outlives the issues on it. "
                 f"Closing it would hide it from status and triage until setup reopened it; "
                 f"roll it over without closing it.")
    # The REST issues endpoint returns pull requests too, and they move the same way.
    items = gh.api(f"repos/{repo}/issues?milestone={src['number']}&state=open&per_page=100",
                   paginate=True)
    return src, dst, items


def rollover_move(repo: str, src: dict, dst: dict, items: list[dict], close: bool,
                  moved=lambda item: None) -> None:
    """Carry a checked rollover out, calling `moved` after each item."""
    for item in items:
        gh.api(f"repos/{repo}/issues/{item['number']}", method="PATCH", milestone=dst["number"])
        moved(item)
    if close:
        # Not re-reading the milestone's open_issues to confirm it is empty: that
        # counter lags writes, and we just moved everything open off it.
        gh.api(f"repos/{repo}/milestones/{src['number']}", method="PATCH", state="closed")
