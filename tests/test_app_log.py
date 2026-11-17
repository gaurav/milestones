"""The app's terminal: the lines the log says about reads and writes."""

from milestones.board import server


def test_describe_overview_says_each_half_of_the_read():
    first = {"repos": ["a/b", "c/d"], "milestones": [{}] * 5, "findings": [{}],
             "untriaged_fresh": False}
    assert server.describe_overview(first) == (
        "5 open milestones across 2 tracked repositories; 1 thing to fix")
    rows = [{"repo": "a/b", "issues": 3, "prs": 1, "needs_triage": 2},
            {"repo": "c/d", "issues": 0, "prs": 0, "needs_triage": 4}]
    assert server.describe_overview({"untriaged": rows, "untriaged_fresh": True}) == (
        "3 issues and 1 pull request on no milestone, in 1 repository; "
        "6 parked on Needs triage")


def test_describe_prs_counts_repos_whatever_their_case():
    prs = [{"repo": "a/b", "group": "tracked", "draft": False},
           {"repo": "A/B", "group": "tracked", "draft": True},
           {"repo": "x/y", "group": "untracked", "draft": False}]
    assert server.describe_prs({"prs": prs}) == (
        "Found 3 open pull requests across 2 repositories: 2 in tracked ones, 1 draft")


def test_action_text_names_the_milestone_and_the_pr_that_carried_it():
    titles = {4: "Needed soon"}
    assert server.action_text({"issue": 7, "kind": "milestone", "value": 4,
                               "carriedBy": 12}, titles) == "#7 → Needed soon (with #12)"
    assert server.action_text({"issue": 7, "kind": "milestone", "value": None}, titles) == (
        "#7 → no milestone")
    assert server.action_text({"issue": 7, "kind": "labels", "value": ["bug"]}, titles) == (
        '#7 labels: ["bug"]')


def test_page_name():
    assert server.page_name("index.html", "/") == "the home page"
    assert server.page_name("board/index.html", "/r/o/n/") == "the board for o/n"
    assert server.page_name("prs/index.html", "/prs/") == "your pull requests"
