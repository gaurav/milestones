"""What the board shows: one repo's milestones and every open issue and pull request, with
all the places an earlier triage decision might have been written down.

The normalisers here are pure, and are what `docs/BoardAPI.md` describes as a card."""

import time

from .. import gh
from .log import say

PAGE = 50
# How long to wait before each new try at a page that failed, and the page size halves with
# each one: a page of fifty cards with every field is a heavy query, and what GitHub answers
# a heavy query with, when it is busy, is a timeout (a 502, or "Something went wrong while
# executing your query"). A smaller page usually goes through.
RETRY_WAITS = (2, 8, 30)
# Both queries page by creation date, which nothing changes: by last update, the board's own
# writes landing mid-read would move an issue from after the cursor to before it, and it would
# never be read. Newest first, so that recent work is what a board filling in shows first; an
# issue opened during the read is behind the cursor, and the next refresh finds it. The page
# sorts its columns itself.

# What a card needs of an issue, for a page of them and for one read alone.
# `issueFieldValues` is a union, hence the per-type fragments; CROSS_REFERENCED_EVENT is how a
# pull request that mentions an issue without closing it shows up, which
# `closedByPullRequestsReferences` alone would miss.
ISSUE_FIELDS = """
      id number title url bodyHTML createdAt updatedAt
      author { login } comments { totalCount }
      assignees(first: 10) { nodes { login } }
      labels(first: 50) { nodes { name color } }
      milestone { number }
      issueType { name }
      parent { number title }
      subIssuesSummary { total completed }
      issueFieldValues(first: 20) { nodes { __typename
        ... on IssueFieldSingleSelectValue { name field { ... on IssueFieldSingleSelect { name } } }
        ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } }
        ... on IssueFieldTextValue { value field { ... on IssueFieldText { name } } }
        ... on IssueFieldNumberValue { value field { ... on IssueFieldNumber { name } } } } }
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
        nodes { number title state isDraft url } }
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: 30) { nodes {
        ... on CrossReferencedEvent { source { __typename
          ... on PullRequest { number title state isDraft url
                               repository { nameWithOwner } } } } } }
      projectItems(first: 1) { totalCount }
"""

# One page of open issues.
ISSUES_QUERY = """
query { repository(owner: "%(owner)s", name: "%(name)s") {
  issues(first: %(page)d, states: OPEN, after: %(after)s,
         orderBy: {field: CREATED_AT, direction: DESC}) {
    totalCount pageInfo { hasNextPage endCursor }
    nodes {""" + ISSUE_FIELDS + """} } } }
"""

PR_FIELDS = """
      id number title url bodyHTML createdAt updatedAt isDraft additions deletions
      author { login } comments { totalCount }
      assignees(first: 10) { nodes { login } }
      labels(first: 50) { nodes { name color } }
      milestone { number }
      closingIssuesReferences(first: 50) {
        nodes { number title state repository { nameWithOwner } } }
      headRefName isCrossRepository headRepositoryOwner { login } baseRefName
      baseRef { associatedPullRequests(states: OPEN, first: 1) {
        nodes { number title state isDraft url } } }
"""

PRS_QUERY = """
query { repository(owner: "%(owner)s", name: "%(name)s") {
  pullRequests(first: %(page)d, states: OPEN, after: %(after)s,
               orderBy: {field: CREATED_AT, direction: DESC}) {
    totalCount pageInfo { hasNextPage endCursor }
    nodes {""" + PR_FIELDS + """} } } }
"""


def _pr_ref(pr: dict) -> dict:
    return {"number": pr["number"], "title": pr["title"], "state": pr["state"],
            "draft": pr["isDraft"], "url": pr["url"]}


def _common(node: dict) -> dict:
    return {
        "id": node["id"],
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "bodyHTML": node["bodyHTML"],
        "created": node["createdAt"],
        "updated": node["updatedAt"],
        # A deleted account's issues come back with a null author.
        "author": (node.get("author") or {}).get("login"),
        "comments": node["comments"]["totalCount"],
        "assignees": [a["login"] for a in node["assignees"]["nodes"]],
        "labels": [{"name": l["name"], "color": l["color"]} for l in node["labels"]["nodes"]],
        "milestone": (node.get("milestone") or {}).get("number"),
    }


def norm_issue(node: dict, repo: str) -> dict:
    """A card for an issue. `fields` is the organisation's issue fields by name, a single
    select as its option's name; `mentionedPrs` leaves out the ones in `closingPrs`, and any
    pull request in another repo, whose number would mean nothing here."""
    closing = [_pr_ref(pr) for pr in node["closedByPullRequestsReferences"]["nodes"]]
    numbers = {pr["number"] for pr in closing}
    mentioned = {}
    for event in node["timelineItems"]["nodes"]:
        source = (event or {}).get("source") or {}
        if (source.get("__typename") == "PullRequest"
                and source["repository"]["nameWithOwner"].lower() == repo.lower()
                and source["number"] not in numbers):
            mentioned[source["number"]] = _pr_ref(source)
    fields = {}
    for value in node["issueFieldValues"]["nodes"]:
        name = ((value or {}).get("field") or {}).get("name")
        if name:
            fields[name] = value.get("name", value.get("value"))
    return {
        **_common(node),
        "kind": "issue",
        "type": (node.get("issueType") or {}).get("name"),
        "parent": node.get("parent"),
        "subIssues": node.get("subIssuesSummary") or {"total": 0, "completed": 0},
        "fields": fields,
        "closingPrs": closing,
        "mentionedPrs": sorted(mentioned.values(), key=lambda pr: pr["number"]),
        "projects": node["projectItems"]["totalCount"],
    }


def norm_pr(node: dict, repo: str) -> dict:
    """A card for a pull request. `closes` is the open-or-closed issues its description
    closes, in this repo only — the ones a move can carry, since milestone numbers are per
    repo. `additions` and `deletions` are its lines added and removed, from which the page
    estimates its effort. `branch` is its head branch, as `owner:branch` when it comes from a
    fork; `base` is the branch it targets, and `basePr` the open pull request whose head is
    that branch — the one below it in a stack — or None. A base branch's
    `associatedPullRequests` lists only the pull requests with it as their head."""
    owner = (node.get("headRepositoryOwner") or {}).get("login")
    branch = node["headRefName"]
    if node.get("isCrossRepository"):
        # A deleted fork leaves no owner to name.
        branch = f"{owner or '(deleted fork)'}:{branch}"
    below = ((node.get("baseRef") or {}).get("associatedPullRequests") or {}).get("nodes") or []
    return {
        **_common(node),
        "kind": "pr",
        "draft": node["isDraft"],
        "additions": node["additions"],
        "deletions": node["deletions"],
        "closes": [{"number": i["number"], "title": i["title"], "state": i["state"]}
                   for i in node["closingIssuesReferences"]["nodes"]
                   if i["repository"]["nameWithOwner"].lower() == repo.lower()],
        "branch": branch,
        "base": node["baseRefName"],
        "basePr": _pr_ref(below[0]) if below else None,
    }


def norm_milestone(m: dict) -> dict:
    """A REST milestone, cut down to what a column needs. `due` is the date alone: `due_on`
    is an instant, written at midday UTC so that it reads back on the day meant."""
    return {"number": m["number"], "title": m["title"], "state": m["state"],
            "due": m["due_on"][:10] if m.get("due_on") else None,
            "description": m.get("description") or "", "url": m["html_url"]}


def due_instant(day: str | None) -> str | None:
    return f"{day}T12:00:00Z" if day else None


def column_order(milestones: list[dict], buckets: list[str], sort_key) -> list[int]:
    """Open milestones' numbers in the order the columns stand: dated ones by date, then
    the buckets in config order, then the rest — the home page's order."""
    return [m["number"] for m in sorted((m for m in milestones if m["state"] == "open"),
                                        key=lambda m: sort_key(m["title"], m["due"], buckets))]


def carried_by(pr: dict, cards: dict[tuple[str, int], dict], parked: int | None) -> list[int]:
    """The issues that move with a pull request: open ones it closes that have no
    milestone, or sit on the Needs triage milestone (`parked`, its number, where the repo
    has one). An issue someone already placed stays where they put it."""
    numbers = []
    for issue in pr.get("closes") or []:
        card = cards.get(("issue", issue["number"]))
        if card and (card["milestone"] is None or card["milestone"] == parked):
            numbers.append(issue["number"])
    return numbers


def _pages(query: str, owner: str, name: str, connection: str, sleep=time.sleep):
    """Each page of a connection as `{what, cards: [node], seen, total}`, retrying a page that
    fails (`RETRY_WAITS`) at the same cursor with a smaller page. If the last try fails too,
    the last thing yielded is `{what, gap: why, seen, total}`, and the rest of the connection
    goes unread: one bad page costs what is after it, not what was read before."""
    after, seen, total, size = "null", 0, None, PAGE
    while True:
        for wait in (*RETRY_WAITS, None):
            try:
                data = gh.graphql(query % {"owner": owner, "name": name, "page": size,
                                           "after": after}, owner)
                break
            except SystemExit as exit:
                why = str(exit).strip().splitlines()[-1]
                if wait is None:
                    yield {"what": connection, "gap": why, "seen": seen, "total": total}
                    return
                size = max(size // 2, 5)
                say(f"{owner}/{name}: GitHub failed reading {connection} after {seen} ({why}); "
                    f"trying again in {wait} s, {size} at a time", "warn")
                sleep(wait)
        repository = data.get("repository")
        if repository is None:
            raise SystemExit(f"No such repository, or not one this token can read: {owner}/{name}")
        page = repository[connection]
        seen, total = seen + len(page["nodes"]), page["totalCount"]
        yield {"what": connection, "cards": [n for n in page["nodes"] if n],
               "seen": seen, "total": total}
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = '"%s"' % page["pageInfo"]["endCursor"]


def cards_query(owner: str, name: str, numbers: list[int]) -> str:
    """One query for some issues and pull requests by number, each under its own alias. A
    number that has gone (deleted, or transferred away) comes back null with a partial
    error, which `gh.graphql` keeps."""
    items = " ".join(
        f"n{n}: issueOrPullRequest(number: {int(n)}) {{ __typename "
        f"... on Issue {{ state {ISSUE_FIELDS} }} ... on PullRequest {{ state {PR_FIELDS} }} }}"
        for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {items} }} }}'


def norm_card(node: dict | None, repo: str) -> dict | None:
    """A card for an issue or pull request read by number, or None for one that is no
    longer on the board: closed, merged, or gone."""
    if not node or node.get("state") != "OPEN":
        return None
    return norm_pr(node, repo) if node["__typename"] == "PullRequest" else norm_issue(node, repo)


def fetch_milestones(repo: str) -> list[dict]:
    return [norm_milestone(m) for m in
            gh.api(f"repos/{repo}/milestones?state=all&per_page=100", paginate=True)]


def fetch_cards(repo: str, numbers: list[int]) -> dict:
    """Some cards read again, `{number: card or None}`, and the repo's milestones with them,
    since a card may have been put on one made since the board read them."""
    owner, name = repo.split("/")
    found = gh.graphql(cards_query(owner, name, numbers), owner).get("repository") or {}
    return {"cards": {n: norm_card(found.get(f"n{n}"), repo) for n in numbers},
            "milestones": fetch_milestones(repo)}


def fetch_frame(repo: str) -> dict:
    """What a board needs before its first card: GitHub's name for the repo (which follows a
    rename or a transfer), its milestones, labels and the owner's issue fields. Only the name
    and the milestones are needed: without labels or fields, a board still works."""
    found = gh.api(f"repos/{repo}")
    canonical = found["full_name"]
    frame = {"repo": canonical, "milestones": fetch_milestones(canonical),
             "labels": [], "fields": {}}
    try:
        frame["labels"] = [{"name": l["name"], "color": l["color"]} for l in
                           gh.api(f"repos/{canonical}/labels?per_page=100", paginate=True)]
        frame["fields"] = fetch_fields(canonical.split("/")[0])
    except SystemExit as exit:
        say(f"{canonical}: could not read its labels or issue fields "
            f"({str(exit).strip().splitlines()[-1]}); the board goes on without them", "warn")
    return frame


def card_pages(repo: str, sleep=time.sleep):
    """Every open issue's card, then every open pull request's, a page at a time, as
    `_pages` yields them. Issues come first so that a pull request is never on a board
    without the issues it closes, which a move of it carries along."""
    owner, name = repo.split("/")
    for query, connection, norm in ((ISSUES_QUERY, "issues", norm_issue),
                                    (PRS_QUERY, "pullRequests", norm_pr)):
        for page in _pages(query, owner, name, connection, sleep):
            if "cards" in page:
                page = {**page, "cards": [norm(n, repo) for n in page["cards"]]}
            yield page


def norm_fields(nodes: list[dict]) -> dict:
    """The organisation's single-select issue fields, as `{name: {id, options: {name: id}}}`.
    The board writes Priority and Effort; the rest ride along for a front end to show."""
    return {n["name"]: {"id": n["id"], "options": {o["name"]: o["id"] for o in n["options"]}}
            for n in nodes if n and n.get("__typename") == "IssueFieldSingleSelect"}


def fetch_fields(owner: str) -> dict:
    """`norm_fields` for a repo's owner; nothing for a user account, which has no issue
    fields and would answer the organisation query with NOT_FOUND."""
    if gh.api(f"users/{owner}")["type"] != "Organization":
        return {}
    found = gh.graphql(
        f'query {{ organization(login: "{owner}") {{ issueFields(first: 50) {{ nodes {{ '
        "__typename ... on IssueFieldSingleSelect { id name options { id name } } } } } }",
        owner)
    return norm_fields(found["organization"]["issueFields"]["nodes"])
