"""The board's pure parts: the card normalisers, and the Board's bookkeeping with the
fetch and the write swapped for fakes. Nothing here calls gh."""

import pytest

from milestones import gh
from milestones.board import data, server
from milestones.cli import remote_repo, sort_key


def issue_node(number, **extra):
    node = {
        "id": f"I_{number}", "number": number, "title": f"Issue {number}", "url": f"u/{number}",
        "bodyHTML": "<p>x</p>", "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-02-01T00:00:00Z", "author": {"login": "a"},
        "comments": {"totalCount": 2}, "assignees": {"nodes": [{"login": "g"}]},
        "labels": {"nodes": [{"name": "bug", "color": "ff0000"}]}, "milestone": None,
        "issueType": None, "parent": None, "subIssuesSummary": {"total": 0, "completed": 0},
        "issueFieldValues": {"nodes": []}, "closedByPullRequestsReferences": {"nodes": []},
        "timelineItems": {"nodes": []}, "projectItems": {"totalCount": 0},
    }
    node.update(extra)
    return node


def pr(number, repo="o/r"):
    return {"__typename": "PullRequest", "number": number, "title": f"PR {number}",
            "state": "OPEN", "isDraft": False, "url": f"p/{number}",
            "repository": {"nameWithOwner": repo}}


def test_norm_issue_reads_fields_and_keeps_mentions_apart_from_closers():
    node = issue_node(
        7, author=None, milestone={"number": 44}, issueType={"name": "Bug"},
        issueFieldValues={"nodes": [
            {"__typename": "IssueFieldSingleSelectValue", "name": "Urgent",
             "field": {"name": "Priority"}},
            {"__typename": "IssueFieldDateValue", "value": "2026-12-01",
             "field": {"name": "Target date"}},
            {"__typename": "IssueFieldMultiSelectValue"}]},
        closedByPullRequestsReferences={"nodes": [pr(10)]},
        timelineItems={"nodes": [{"source": pr(10)}, {"source": pr(11)}, {"source": pr(11)},
                                 {"source": pr(12, "other/repo")},
                                 {"source": {"__typename": "Issue"}}, {}]})
    card = data.norm_issue(node, "O/R")
    assert card["kind"] == "issue" and card["milestone"] == 44 and card["author"] is None
    assert card["type"] == "Bug" and card["comments"] == 2 and card["assignees"] == ["g"]
    assert card["fields"] == {"Priority": "Urgent", "Target date": "2026-12-01"}
    assert [p["number"] for p in card["closingPrs"]] == [10]
    assert [p["number"] for p in card["mentionedPrs"]] == [11]


def test_norm_pr_carries_only_the_issues_it_closes_in_its_own_repo():
    node = issue_node(20, isDraft=True, closingIssuesReferences={"nodes": [
        {"number": 7, "title": "a", "state": "OPEN", "repository": {"nameWithOwner": "o/r"}},
        {"number": 8, "title": "b", "state": "OPEN", "repository": {"nameWithOwner": "x/y"}}]})
    card = data.norm_pr(node, "o/r")
    assert card["kind"] == "pr" and card["draft"] is True
    assert [c["number"] for c in card["closes"]] == [7]


def test_milestone_dates_go_out_at_midday_and_come_back_as_days():
    m = data.norm_milestone({"number": 1, "title": "v1", "state": "open", "html_url": "u",
                             "due_on": "2026-12-24T12:00:00Z", "description": None})
    assert m["due"] == "2026-12-24" and m["description"] == ""
    assert data.due_instant("2026-12-24") == "2026-12-24T12:00:00Z"
    assert data.due_instant(None) is None


def test_columns_stand_in_statuss_order_and_leave_closed_milestones_out():
    ms = [{"number": 1, "title": "Needed soon", "state": "open", "due": None},
          {"number": 2, "title": "v2", "state": "open", "due": "2026-12-01"},
          {"number": 3, "title": "Critical", "state": "open", "due": None},
          {"number": 4, "title": "Old", "state": "closed", "due": "2020-01-01"},
          {"number": 5, "title": "Misc", "state": "open", "due": None}]
    assert data.column_order(ms, ["Critical", "Needed soon"], sort_key) == [2, 3, 1, 5]


def test_remote_repo_reads_githubs_remote_spellings():
    assert remote_repo("git@github.com:NCATSTranslator/Babel.git\n") == "NCATSTranslator/Babel"
    assert remote_repo("https://github.com/gaurav/milestones") == "gaurav/milestones"
    assert remote_repo("ssh://git@github.com/a/b.git") == "a/b"
    assert remote_repo("git@gitlab.com:a/b.git") is None


def test_only_loopback_hosts_on_the_servers_own_port_are_served():
    assert server.allowed_host("127.0.0.1:8000", 8000)
    assert server.allowed_host("localhost:8000", 8000)
    assert not server.allowed_host("evil.example:8000", 8000)
    assert not server.allowed_host("127.0.0.1:9000", 8000)
    assert not server.allowed_host(None, 8000)


def card(number, kind="issue", milestone=None, **extra):
    return {"number": number, "kind": kind, "milestone": milestone, **extra}


def make_board(monkeypatch, cards, read_only=False, buckets=("Needs triage",), write=None):
    monkeypatch.setattr(gh, "read_only", lambda: read_only)
    fetched = {"repo": "o/r", "cards": cards, "milestones": [
        {"number": 1, "title": "Soon", "state": "open", "due": None},
        {"number": 2, "title": "Needs triage", "state": "open", "due": None},
        {"number": 3, "title": "Old", "state": "closed", "due": None}]}
    board = server.Board("o/r", list(buckets), sort_key, fetch=lambda repo, progress: fetched,
                         write=write or (lambda action: None))
    board.load()
    return board


def run_queue(board):
    """One pass of the writer over everything queued, without its thread."""
    while not board.todo.empty():
        action = board.actions[board.todo.get()]
        try:
            board.write(action)
            action["status"] = "done"
        except SystemExit as exit:
            action.update(status="failed", error=str(exit))


def test_a_move_shows_at_once_and_is_queued(monkeypatch):
    board = make_board(monkeypatch, [card(5)])
    (action,) = board.submit([{"issue": 5, "kind": "milestone", "value": 1}])
    assert action["status"] == "pending" and action["previous"] is None
    assert board.cards[5]["milestone"] == 1
    assert board.submit([{"issue": 5, "kind": "milestone", "value": 1}]) == []  # already there
    run_queue(board)
    assert board.actions[1]["status"] == "done" and board.unsettled() == []


def test_a_bad_request_refuses_the_whole_batch(monkeypatch):
    board = make_board(monkeypatch, [card(5), card(6)])
    for bad, status in (({"issue": 6, "kind": "milestone", "value": 99}, 404),
                        ({"issue": 6, "kind": "milestone", "value": 3}, 409),   # closed
                        ({"issue": 9, "kind": "milestone", "value": 1}, 404),
                        ({"issue": 6, "kind": "title", "value": "x"}, 400)):
        with pytest.raises(server.Refused) as refused:
            board.submit([{"issue": 5, "kind": "milestone", "value": 1}, bad])
        assert refused.value.status == status
    assert board.cards[5]["milestone"] is None and board.actions == {}


def test_a_pull_request_carries_unplaced_and_parked_issues_but_not_placed_ones(monkeypatch):
    closes = [{"number": n, "title": "", "state": "OPEN"} for n in (5, 6, 7, 99)]
    board = make_board(monkeypatch, [card(5), card(6, milestone=2), card(7, milestone=1),
                                     card(20, "pr", closes=closes)])
    made = board.submit([{"issue": 20, "kind": "milestone", "value": 1}])
    assert [(a["issue"], a["carriedBy"]) for a in made] == [(20, None), (5, 20), (6, 20)]
    assert board.cards[6]["milestone"] == 1 and board.cards[7]["milestone"] == 1


def test_parked_issues_stay_put_when_the_config_has_no_needs_triage_bucket(monkeypatch):
    closes = [{"number": 6, "title": "", "state": "OPEN"}]
    board = make_board(monkeypatch, [card(6, milestone=2), card(20, "pr", closes=closes)],
                       buckets=())
    assert [a["issue"] for a in board.submit([{"issue": 20, "kind": "milestone", "value": 1}])] == [20]


def test_a_failed_write_keeps_the_new_value_until_retried_or_reverted(monkeypatch):
    fail = {"on": True}

    def write(action):
        if fail["on"]:
            raise SystemExit("gh api failed:\nboom")

    board = make_board(monkeypatch, [card(5)], write=write)
    board.submit([{"issue": 5, "kind": "milestone", "value": 1}])
    run_queue(board)
    assert board.actions[1]["status"] == "failed" and "boom" in board.actions[1]["error"]
    assert board.cards[5]["milestone"] == 1  # what you asked for stays on screen
    assert [a["id"] for a in board.unsettled()] == [1]
    board.load()  # a refetch must not put the card back either
    assert board.cards[5]["milestone"] == 1

    fail["on"] = False
    assert board.retry(1)["status"] == "pending"
    run_queue(board)
    assert board.actions[1]["status"] == "done"
    with pytest.raises(server.Refused):
        board.revert(1)  # only a failed action can be given up on

    fail["on"] = True
    board.submit([{"issue": 5, "kind": "milestone", "value": None}])
    run_queue(board)
    assert board.revert(2)["status"] == "reverted"
    assert board.cards[5]["milestone"] == 1 and board.unsettled() == []


def test_a_read_only_board_moves_cards_and_queues_nothing(monkeypatch):
    board = make_board(monkeypatch, [card(5)], read_only=True,
                       write=lambda action: pytest.fail("a write was made"))
    (action,) = board.submit([{"issue": 5, "kind": "milestone", "value": 1}])
    assert action["status"] == "would" and board.todo.empty()
    assert board.cards[5]["milestone"] == 1 and board.snapshot()["permissions"] == "read"
    with pytest.raises(server.Refused, match="would create milestone 'New'") as refused:
        board.ensure_milestone("New")
    assert refused.value.status == 403
    with pytest.raises(server.Refused, match="would set milestone 1's due date"):
        board.set_due(1, "2026-12-24")


def test_ensure_milestone_never_makes_a_second_one_of_a_name(monkeypatch):
    board = make_board(monkeypatch, [])
    assert board.ensure_milestone(" soon ")["number"] == 1
    with pytest.raises(server.Refused) as refused:
        board.ensure_milestone("old")
    assert refused.value.status == 409
    with pytest.raises(server.Refused):
        board.ensure_milestone("  ")
    with pytest.raises(server.Refused, match="YYYY-MM-DD"):
        board.set_due(1, "soon")


def test_closing_lines_say_what_never_reached_github():
    snapshot = {"repo": "o/r", "milestones": [{"number": 1, "title": "Soon"}], "actions": [
        {"issue": 5, "value": 1, "status": "done", "error": None},
        {"issue": 6, "value": 1, "status": "failed", "error": "gh failed:\nboom"},
        {"issue": 7, "value": None, "status": "would", "error": None}]}
    lines = server.closing_lines(snapshot)
    assert "1 written to o/r." in lines
    assert "  #6 → Soon  — gh failed:" in lines and "  #7 → no milestone" in lines
