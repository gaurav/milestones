"""What the board shows: one repo's milestones and every open issue and pull request, with
all the places an earlier triage decision might have been written down.

The normalisers here are pure, and are what `docs/BoardAPI.md` describes as a card."""

from .. import gh

PAGE = 50

# One page of open issues. `issueFieldValues` is a union, hence the per-type fragments;
# CROSS_REFERENCED_EVENT is how a pull request that mentions an issue without closing it
# shows up, which `closedByPullRequestsReferences` alone would miss.
ISSUES_QUERY = """
query { repository(owner: "%(owner)s", name: "%(name)s") {
  nameWithOwner
  issues(first: %(page)d, states: OPEN, after: %(after)s,
         orderBy: {field: UPDATED_AT, direction: DESC}) {
    totalCount pageInfo { hasNextPage endCursor }
    nodes {
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
    } } } }
"""

PRS_QUERY = """
query { repository(owner: "%(owner)s", name: "%(name)s") {
  pullRequests(first: %(page)d, states: OPEN, after: %(after)s,
               orderBy: {field: UPDATED_AT, direction: DESC}) {
    totalCount pageInfo { hasNextPage endCursor }
    nodes {
      id number title url bodyHTML createdAt updatedAt isDraft
      author { login } comments { totalCount }
      assignees(first: 10) { nodes { login } }
      labels(first: 50) { nodes { name color } }
      milestone { number }
      closingIssuesReferences(first: 50) {
        nodes { number title state repository { nameWithOwner } } }
    } } } }
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
    repo."""
    return {
        **_common(node),
        "kind": "pr",
        "draft": node["isDraft"],
        "closes": [{"number": i["number"], "title": i["title"], "state": i["state"]}
                   for i in node["closingIssuesReferences"]["nodes"]
                   if i["repository"]["nameWithOwner"].lower() == repo.lower()],
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
    the buckets in config order, then the rest — `status`'s order."""
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


def _pages(query: str, owner: str, name: str, connection: str, progress):
    after, seen = "null", 0
    while True:
        data = gh.graphql(query % {"owner": owner, "name": name, "page": PAGE, "after": after},
                          owner)
        repository = data.get("repository")
        if repository is None:
            raise SystemExit(f"No such repository, or not one this token can read: {owner}/{name}")
        page = repository[connection]
        seen += len(page["nodes"])
        progress(connection, seen, page["totalCount"])
        yield repository.get("nameWithOwner"), page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = '"%s"' % page["pageInfo"]["endCursor"]


def fetch_board(repo: str, progress=lambda *a: None) -> dict:
    """Everything `GET /api/v1/board` returns but the session's own state."""
    owner, name = repo.split("/")
    cards = []
    canonical = repo
    for name_with_owner, nodes in _pages(ISSUES_QUERY, owner, name, "issues", progress):
        # The name GitHub answers with, which follows a rename or a transfer.
        canonical = name_with_owner or canonical
        cards += [norm_issue(n, canonical) for n in nodes if n]
    for _, nodes in _pages(PRS_QUERY, owner, name, "pullRequests", progress):
        cards += [norm_pr(n, canonical) for n in nodes if n]
    milestones = [norm_milestone(m) for m in
                  gh.api(f"repos/{canonical}/milestones?state=all&per_page=100", paginate=True)]
    labels = [{"name": l["name"], "color": l["color"]} for l in
              gh.api(f"repos/{canonical}/labels?per_page=100", paginate=True)]
    return {"repo": canonical, "milestones": milestones, "cards": cards, "labels": labels,
            "fields": fetch_fields(canonical.split("/")[0])}


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
