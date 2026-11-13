"""The board's pure parts: the card normalisers, and the Board's bookkeeping with the
fetch and the write swapped for fakes. Nothing here calls gh."""

import pytest

from milestones import gh
from pathlib import Path

from milestones.board import client, data, plan, reconcile, server
from milestones.cli import DEFAULT_BUCKETS, remote_repo, sort_key, suggest_ref


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
    node = issue_node(20, isDraft=True, additions=120, deletions=30, closingIssuesReferences={"nodes": [
        {"number": 7, "title": "a", "state": "OPEN", "repository": {"nameWithOwner": "o/r"}},
        {"number": 8, "title": "b", "state": "OPEN", "repository": {"nameWithOwner": "x/y"}}]})
    card = data.norm_pr(node, "o/r")
    assert card["kind"] == "pr" and card["draft"] is True
    assert (card["additions"], card["deletions"]) == (120, 30)
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


def test_the_page_url_asks_for_the_view_in_its_query_and_keeps_the_token_in_its_fragment():
    assert server.page_url(8000, "tok") == "http://127.0.0.1:8000/#t=tok"
    assert (server.page_url(8000, "tok", ["Babel v1.19 & after", "none"], "priority", "effort")
            == "http://127.0.0.1:8000/?by=priority&group=effort&from=Babel%20v1.19%20%26%20after&from=none#t=tok")


def test_only_loopback_hosts_on_the_servers_own_port_are_served():
    assert server.allowed_host("127.0.0.1:8000", 8000)
    assert server.allowed_host("localhost:8000", 8000)
    assert not server.allowed_host("evil.example:8000", 8000)
    assert not server.allowed_host("127.0.0.1:9000", 8000)
    assert not server.allowed_host(None, 8000)


def card(number, kind="issue", milestone=None, **extra):
    return {"number": number, "id": f"N{number}", "kind": kind, "milestone": milestone,
            "title": f"Card {number}", "labels": [], "comments": 0, **extra}


ORG_FIELDS = {"Priority": {"id": "F_P", "options": {"Urgent": "o1", "High": "o2",
                                                   "Medium": "o3", "Low": "o4"}},
              "Effort": {"id": "F_E", "options": {"High": "e1", "Medium": "e2", "Low": "e3"}}}


def make_board(monkeypatch, cards, read_only=False, buckets=("Needs triage",), write=None,
               fields=None, state_file=None, plan_file=None):
    monkeypatch.setattr(gh, "read_only", lambda: read_only)
    fetched = {"repo": "o/r", "cards": cards, "fields": fields or {},
               "labels": [{"name": "bug", "color": "ff0000"}], "milestones": [
        {"number": 1, "title": "Soon", "state": "open", "due": None},
        {"number": 2, "title": "Needs triage", "state": "open", "due": None},
        {"number": 3, "title": "Old", "state": "closed", "due": None}]}
    board = server.Board("o/r", list(buckets), sort_key, fetch=lambda repo, progress: fetched,
                         write=write or (lambda action: None), state_file=state_file,
                         plan_file=plan_file)
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
                        ({"issue": 6, "kind": "title", "value": " "}, 400),
                        ({"issue": 6, "kind": "state", "value": "wontfix"}, 400),
                        ({"issue": 6, "kind": "labels", "value": "bug"}, 400),
                        ({"issue": 6, "kind": "assignee", "value": "x"}, 400)):
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


# --- fields, labels and the other kinds ---------------------------------------------------

def labels(*names):
    return [{"name": n, "color": "cccccc"} for n in names]


def test_a_priority_or_an_effort_is_a_label_on_an_issue_or_a_pull_request(monkeypatch):
    board = make_board(monkeypatch, [card(5, labels=labels("bug", "Priority: Critical")),
                                     card(20, "pr", labels=labels("Size: M"))], fields=ORG_FIELDS)
    (action,) = board.submit([{"issue": 5, "kind": "priority", "value": "high"}])
    assert (action["kind"], action["value"]) == ("labels", ["bug", "Priority: High"])
    assert action["previous"] == ["bug", "Priority: Critical"]
    # A label the repo knows keeps its colour; a new one is grey until GitHub answers.
    assert board.cards[5]["labels"] == [{"name": "bug", "color": "ff0000"},
                                        {"name": "Priority: High", "color": "ededed"}]
    assert board.cards[5].get("fields") is None   # the organisation field is not written
    (cleared,) = board.submit([{"issue": 5, "kind": "priority", "value": None}])
    assert cleared["value"] == ["bug"]
    # A pull request takes one as an issue does, and the old spelling goes as the new arrives.
    (sized,) = board.submit([{"issue": 20, "kind": "effort", "value": "xl"}])
    assert sized["value"] == ["Effort: XL"]
    assert board.submit([{"issue": 20, "kind": "effort", "value": "XL"}]) == []   # already so
    for bad in ("Asap", "High", 3):   # High is a priority, not an effort
        with pytest.raises(server.Refused):
            board.submit([{"issue": 20, "kind": "effort", "value": bad}])


def test_a_field_can_only_be_cleared_or_put_back(monkeypatch):
    board = make_board(monkeypatch, [card(5, fields={"Priority": "High"}), card(20, "pr")],
                       fields=ORG_FIELDS)
    (action,) = board.submit([{"issue": 5, "kind": "priority-field", "value": None}])
    assert (action["previous"], board.cards[5]["fields"]) == ("High", {})
    board.submit([{"issue": 5, "kind": "priority-field", "value": "High"}])   # an undo
    assert board.cards[5]["fields"] == {"Priority": "High"}
    for bad in ({"issue": 5, "kind": "priority-field", "value": "Asap"},
                {"issue": 20, "kind": "effort-field", "value": None}):   # a PR has no fields
        with pytest.raises(server.Refused):
            board.submit([bad])
    with pytest.raises(server.Refused):   # an owner with no such field
        make_board(monkeypatch, [card(5)]).submit(
            [{"issue": 5, "kind": "priority-field", "value": None}])


def test_a_standard_label_the_repo_lacks_is_made_in_its_colour(monkeypatch):
    calls = []

    def api(path, method="GET", body=None, **fields):
        calls.append((method, path, body))
        return [{"name": n, "color": "d2042d"} for n in body["labels"]] if method == "PUT" else {}
    monkeypatch.setattr(gh, "api", api)
    board = make_board(monkeypatch, [card(5)])
    board._write({"kind": "labels", "value": ["bug", "Priority: Urgent"], "issue": 5})
    assert calls[0] == ("POST", "repos/o/r/labels", {
        "name": "Priority: Urgent", "color": "d2042d", "description": "Drop other work for this."})
    assert calls[1][:2] == ("PUT", "repos/o/r/issues/5/labels") and len(calls) == 2
    assert reconcile.missing_standard(["priority: urgent", "bug"], ["Priority: Urgent"]) == []


def test_labels_take_a_whole_set_or_a_change_to_it(monkeypatch):
    board = make_board(monkeypatch, [card(5, labels=labels("a", "b"))])
    (action,) = board.submit([{"issue": 5, "kind": "labels",
                               "value": {"add": ["c", "a"], "remove": ["b"]}}])
    assert action["value"] == ["a", "c"]
    assert board.submit([{"issue": 5, "kind": "labels", "value": ["a", "c", "a"]}]) == []


def test_changes_in_one_batch_build_on_each_other(monkeypatch):
    board = make_board(monkeypatch, [card(5)])
    made = board.submit([{"issue": 5, "kind": "priority", "value": "high"},
                         {"issue": 5, "kind": "labels", "value": {"add": ["bug"]}}])
    assert [a["value"] for a in made] == [["Priority: High"], ["Priority: High", "bug"]]


def test_title_state_and_comment_are_actions_too(monkeypatch):
    board = make_board(monkeypatch, [card(5)])
    made = board.submit([{"issue": 5, "kind": "title", "value": "  Better  "},
                         {"issue": 5, "kind": "state", "value": "not_planned"},
                         {"issue": 5, "kind": "comment", "value": "why"},
                         {"issue": 5, "kind": "comment", "value": "why"}])
    assert [(a["kind"], a["value"], a["previous"]) for a in made] == [
        ("title", "Better", "Card 5"), ("state", "not_planned", None),
        ("comment", "why", None), ("comment", "why", None)]   # a comment is never a no-op
    assert board.cards[5]["title"] == "Better" and board.cards[5]["closed"] == "not_planned"


def test_reconcile_reads_every_record_and_says_when_they_agree():
    c = card(5, fields={"Priority": "High", "Effort": "Medium"},
             labels=labels("Priority: High", "Size: M", "bug"))
    found = reconcile.reconcile("priority", c)
    assert (found["standard"], found["others"], found["field"]) == (["High"], [], "High")
    found = reconcile.reconcile("effort", c)   # the field's Medium is an M
    assert (found["standard"], found["others"], found["agreed"]) == ([], [("Size: M", "M")], "M")
    c = card(5, fields={"Priority": "High"}, labels=labels("priority:critical"))
    assert reconcile.reconcile("priority", c)["agreed"] is None
    assert reconcile.label_value("priority", "Priority: Critical") == "Urgent"
    assert reconcile.label_value("effort", "Size: XL") == "XL"
    assert reconcile.label_value("effort", "Effort: High") == "L"
    assert reconcile.label_value("priority", "Priority: Someday") is None
    assert reconcile.label_value("priority", "bug") is None
    # A repo's own label says what the standard label it is configured to stand for says.
    aliases = {"p0": "Priority: Urgent"}
    assert reconcile.label_value("priority", "P0", aliases) == "Urgent"
    assert reconcile.label_value("effort", "P0", aliases) is None
    assert reconcile.reconcile("priority", card(5, labels=labels("P0")), aliases)["agreed"] == "Urgent"


def suggested(c, aliases=None):
    return [(s["kind"], s["value"], s["source"]) for s in reconcile.rule_suggestions(c, aliases)]


def test_rules_move_what_an_old_label_or_the_field_says_onto_the_standard_label():
    assert suggested(card(5, labels=labels("Priority: Critical", "bug"))) == [
        ("priority", "Urgent", "rule:priority-label")]
    assert suggested(card(5, fields={"Effort": "High"})) == [("effort", "L", "rule:effort-label")]
    # A pull request is no different, and a repo's own label counts.
    assert suggested(card(20, "pr", labels=labels("Size: S"))) == [("effort", "S", "rule:effort-label")]
    assert suggested(card(5, labels=labels("P0")), {"p0": "Priority: Urgent"}) == [
        ("priority", "Urgent", "rule:priority-label")]
    (s,) = reconcile.rule_suggestions(card(5, fields={"Priority": "Urgent"},
                                           labels=labels("Priority: Critical")))
    assert "'Priority: Critical' and the Priority field say Urgent" in s["reason"]


def test_rules_clear_what_repeats_or_contradicts_the_standard_label_and_say_so():
    c = card(5, fields={"Priority": "High", "Effort": "Low"},
             labels=labels("Priority: Medium", "Effort: S", "Size: S", "P2"))
    clear_p, clear_e, remove = reconcile.rule_suggestions(c, {"p2": "Priority: Low"})
    assert (clear_p["kind"], clear_p["value"]) == ("priority-field", None)
    assert "Priority field says High. The label is kept" in clear_p["reason"]
    assert clear_e["kind"] == "effort-field" and "repeats it" in clear_e["reason"]
    # An old label goes; a repo's own label is read, and never removed.
    assert remove["value"] == {"add": [], "remove": ["Size: S"]}
    assert "'Effort: S' is set; the label 'Size: S' repeats it." in remove["reason"]


def test_rules_leave_alone_what_they_cannot_settle():
    assert suggested(card(5, labels=labels("Priority: High", "Priority: Low"))) == []
    assert suggested(card(5, labels=labels("Size: S", "Size: L"))) == []
    assert suggested(card(5, fields={"Priority": "Low"}, labels=labels("Priority: Critical"))) == []
    assert suggested(card(5, labels=labels("Priority: Someday"))) == []
    assert suggested(card(5, labels=labels("Priority: High", "Effort: M", "bug"))) == []


def test_accepting_a_suggestion_makes_the_action_and_the_next_suggestion(monkeypatch):
    board = make_board(monkeypatch, [card(5, fields={"Priority": "Urgent"},
                                          labels=labels("Priority: Critical", "bug"))],
                       fields=ORG_FIELDS)
    (first,) = board.snapshot()["suggestions"]
    assert (first["kind"], first["value"], first["status"]) == ("priority", "Urgent", "open")
    changed, actions = board.resolve([first["id"]], "accept")
    assert actions[0]["kind"] == "labels"
    assert [l["name"] for l in board.cards[5]["labels"]] == ["bug", "Priority: Urgent"]
    assert first["status"] == "accepted" and first["action"] == actions[0]["id"]
    # The label is now set, so the field repeating it is the next thing to go.
    (second,) = [s for s in board.snapshot()["suggestions"] if s["status"] == "open"]
    assert second["source"] == "rule:clear-priority-field" and second in changed
    board.resolve([second["id"]], "accept")
    assert board.cards[5]["fields"] == {}
    assert [s for s in board.snapshot()["suggestions"] if s["status"] == "open"] == []


def test_a_rejected_suggestion_stays_rejected_across_runs(monkeypatch, tmp_path):
    state = tmp_path / "state" / "board.json"
    cards = lambda: [card(5, labels=labels("Priority: Critical"))]  # noqa: E731
    board = make_board(monkeypatch, cards(), fields=ORG_FIELDS, state_file=state)
    (rule,) = board.snapshot()["suggestions"]
    (agent,) = board.suggest([{"issue": 5, "kind": "milestone", "value": 1,
                               "reason": "blocks the release", "source": "opus"}])
    assert board.suggest([{"issue": 5, "kind": "milestone", "value": 1, "source": "opus"}]) == []
    board.resolve([rule["id"]], "reject")
    board.load()   # a refetch must not bring it back
    assert [s["source"] for s in board.snapshot()["suggestions"]] == ["opus"]

    again = make_board(monkeypatch, cards(), fields=ORG_FIELDS, state_file=state)
    (kept,) = again.snapshot()["suggestions"]   # the agent's, restored; the rule's, not remade
    assert (kept["source"], kept["reason"], kept["status"]) == ("opus", "blocks the release", "open")


def test_a_suggestion_that_could_never_be_accepted_is_refused(monkeypatch):
    board = make_board(monkeypatch, [card(5)])
    for bad in ({"issue": 5, "kind": "milestone", "value": 3, "source": "a"},     # closed
                {"issue": 9, "kind": "title", "value": "x", "source": "a"},
                {"issue": 5, "kind": "title", "value": "x", "source": "rule:mine"}):
        with pytest.raises(server.Refused):
            board.suggest([bad])
    with pytest.raises(server.Refused):
        board.resolve([1], "maybe")


def test_state_path_is_per_repo_under_the_state_home():
    assert server.state_path("o/r", {"XDG_STATE_HOME": "/s"}) == Path("/s/milestones/board-o-r.json")


# --- milestones suggest -------------------------------------------------------------------

def test_one_command_becomes_one_suggestion_per_change():
    made = client.proposals(62, "own source, no conflicts", "opus", milestone="Needed soon",
                            priority="medium", effort="none", add_labels=["new data source"],
                            title="Add X", comment="note", close="not_planned")
    assert [(p["kind"], p["value"]) for p in made] == [
        ("milestone", "Needed soon"), ("priority", "medium"), ("effort", None),
        ("labels", {"add": ["new data source"], "remove": []}), ("title", "Add X"),
        ("comment", "note"), ("state", "not_planned")]
    assert {(p["issue"], p["reason"], p["source"]) for p in made} == {
        (62, "own source, no conflicts", "opus")}
    assert client.proposals(1, "r", "s", milestone="none")[0]["value"] is None
    with pytest.raises(SystemExit, match="Nothing to suggest"):
        client.proposals(1, "r", "s")


def test_a_suggestion_names_its_milestone_by_title(monkeypatch):
    board = make_board(monkeypatch, [card(5)])
    (s,) = board.suggest([{"issue": 5, "kind": "milestone", "value": " soon ",
                           "reason": "r", "source": "opus"}])
    assert s["value"] == 1
    with pytest.raises(server.Refused, match="the open ones are: Needs triage, Soon"):
        board.suggest([{"issue": 5, "kind": "milestone", "value": "v9", "source": "opus"}])
    with pytest.raises(server.Refused, match="closed"):
        board.suggest([{"issue": 5, "kind": "milestone", "value": "Old", "source": "opus"}])


def test_suggest_refs_are_bare_numbers_inside_a_checkout():
    assert suggest_ref("62", "o/r") == ("o/r", 62)
    assert suggest_ref("#62", "o/r") == ("o/r", 62)
    assert suggest_ref("a/b#7", "o/r") == ("a/b", 7)
    assert suggest_ref("https://github.com/a/b/issues/7", None) == ("a/b", 7)
    with pytest.raises(SystemExit, match="names no repo"):
        suggest_ref("62", None)


def test_a_running_board_is_found_by_repo_whatever_the_case(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="No board is running for o/r"):
        client.find_session("o/r")
    path = client.write_session("O/R", "http://127.0.0.1:1", "tok")
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.find_session("o/r")["token"] == "tok"
    with pytest.raises(SystemExit, match="not answering"):
        client.post(client.find_session("o/r"), "/api/v1/suggestions", {})


def test_a_board_counts_as_running_only_while_its_process_is(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert client.running_session("o/r") is None
    path = client.write_session("o/r", "http://127.0.0.1:1", "tok")   # this process: alive
    assert client.running_session("O/R")["url"] == "http://127.0.0.1:1"
    # A pid no process has: what a board killed outright leaves behind.
    path.write_text('{"repo": "o/r", "url": "u", "token": "t", "pid": 2147483646}')
    assert client.running_session("o/r") is None
    path.write_text('{"repo": "o/r", "url": "u", "token": "t"}')
    assert client.running_session("o/r") is None


def test_brief_buckets_match_milestones_md():
    table = Path(__file__).parent.parent.joinpath("MILESTONES.md").read_text()
    for bucket in DEFAULT_BUCKETS:
        assert f"| `{bucket}`" in table
        assert client.BUCKET_MEANINGS[bucket] in " ".join(table.split())
    text = client.brief(DEFAULT_BUCKETS)
    assert "milestones suggest REF" in text and "- Critical: Must go into the next release" in text
    assert "Urgent, High, Medium, Low" in text


def test_labels_md_matches_the_label_table():
    doc = " ".join(Path(__file__).parent.parent.joinpath("LABELS.md").read_text().split())
    brief = client.brief(DEFAULT_BUCKETS)
    for kind, spec in reconcile.KINDS.items():
        for option in spec["options"]:
            label, meaning = reconcile.standard_label(kind, option), spec["meanings"][option]
            assert f"| `{label}` | `#{spec['colors'][option]}` | {meaning} |" in doc
            assert f"- {spec['field']} {option}: {meaning}" in brief
    assert "XS, S, M, L, XL" in brief


def test_the_board_hands_the_page_its_label_table(monkeypatch):
    monkeypatch.setattr(gh, "read_only", lambda: False)
    board = server.Board("o/r", [], sort_key, aliases={"p0": "Priority: Urgent", "easy": "Effort: S"})
    table = board.snapshot()["labelValues"]
    assert table["priority"]["values"]["critical"] == "Urgent"
    assert table["priority"]["options"] == ["Urgent", "High", "Medium", "Low"]
    assert table["effort"]["options"] == ["XS", "S", "M", "L", "XL"]
    assert (table["priority"]["aliases"], table["effort"]["aliases"]) == ({"p0": "Urgent"}, {"easy": "S"})


def test_the_view_is_remembered_between_runs(monkeypatch, tmp_path):
    state = tmp_path / "board.json"
    board = make_board(monkeypatch, [], state_file=state)
    assert board.snapshot()["view"] == {}
    view = {"by": "priority", "group": ["effort"], "shown": [0, 1]}
    assert board.set_view({**view, "extra": "dropped"}) == {**view, "detail": True}
    view = {**view, "detail": False}
    assert board.set_view(view) == view
    for bad in ({"by": "labels", "group": [], "shown": []}, {"by": "priority", "group": ["x"], "shown": []},
                {"by": "priority", "group": [], "shown": ["Soon"]}, {"by": "priority", "group": [], "shown": [True]},
                {"by": "priority", "group": [], "shown": [], "detail": "no"}, {}):
        with pytest.raises(server.Refused):
            board.set_view(bad)
    assert make_board(monkeypatch, [], state_file=state).snapshot()["view"] == view


# --- the plan: a read-only session's changes ----------------------------------------------

def would(issue, kind, value, previous, status="would"):
    return {"issue": issue, "kind": kind, "value": value, "previous": previous, "status": status}


def test_a_plan_is_the_net_change_to_each_card():
    entries = plan.net([
        would(5, "milestone", 1, None), would(5, "milestone", 2, 1),      # two moves: one change
        would(6, "priority", "High", None), would(6, "priority", None, "High"),   # undone: none
        would(7, "comment", "a", None), would(7, "comment", "a", None),   # comments each stand
        would(8, "title", "x", "y", status="done")])                      # written: not a plan
    assert entries == [
        {"issue": 5, "kind": "milestone", "value": 2, "previous": None},
        {"issue": 7, "kind": "comment", "value": "a", "previous": None},
        {"issue": 7, "kind": "comment", "value": "a", "previous": None}]


def test_plan_lines_read_without_the_board():
    titles = {1: "Needed soon"}
    assert plan.summary(would(62, "milestone", 1, None), titles) == \
        "#62: milestone → Needed soon (was none)"
    assert plan.summary(would(62, "labels", ["a", "b"], []), titles) == "#62: labels → a, b (was none)"
    assert plan.summary(would(62, "state", "not_planned", None), titles) == \
        "#62: state → closed as not_planned (was open)"
    assert plan.summary(would(62, "comment", "hello", None), titles) == "#62: comment: hello"


def test_a_read_only_board_saves_its_changes_and_picks_them_up_again(monkeypatch, tmp_path):
    path = tmp_path / "state" / "board.plan.json"
    cards = lambda: [card(5), card(6), card(7)]  # noqa: E731
    board = make_board(monkeypatch, cards(), read_only=True, plan_file=path)
    board.submit([{"issue": 5, "kind": "milestone", "value": 1},
                  {"issue": 6, "kind": "title", "value": "Better"},
                  {"issue": 7, "kind": "milestone", "value": 2}])
    board.submit([{"issue": 7, "kind": "milestone", "value": None}])   # and back again
    board.save_plan()
    saved = plan.read(path)
    assert saved["repo"] == "o/r" and board.plan_info() == {"path": str(path), "count": 2}
    assert [(c["issue"], c["summary"], c["title"]) for c in saved["changes"]] == [
        (5, "#5: milestone → Soon (was none)", "Card 5"),
        (6, "#6: title → Better (was Card 6)", "Better")]   # the card as the plan leaves it

    again = make_board(monkeypatch, cards()[:1], read_only=True, plan_file=path)   # #6 has closed
    restored, skipped = again.restore_plan()
    assert restored == 1 and again.cards[5]["milestone"] == 1
    assert len(skipped) == 1 and "#6: title → Better" in skipped[0]
    assert [c["issue"] for c in plan.read(path)["changes"]] == [5]

    again.submit([{"issue": 5, "kind": "milestone", "value": None}])
    again.save_plan()
    assert not path.exists()   # nothing left to plan
    with pytest.raises(server.Refused, match="read-only"):
        again.review_plan()
    with pytest.raises(server.Refused, match="undo"):
        again.discard_plan()


def test_a_writable_board_offers_a_plan_back_as_suggestions(monkeypatch, tmp_path):
    path = tmp_path / "board.plan.json"
    path.write_text(__import__("json").dumps({"repo": "o/r", "saved": "2026-11-11T20:00:00-05:00", "changes": [
        {"issue": 5, "kind": "milestone", "value": 1, "previous": None},      # still applies
        {"issue": 6, "kind": "milestone", "value": 1, "previous": None},      # already there
        {"issue": 7, "kind": "priority", "value": "High", "previous": None},  # set by hand since
        {"issue": 8, "kind": "labels", "value": ["a", "c"], "previous": ["a", "b"]},
        {"issue": 9, "kind": "title", "value": "x", "previous": "y"}]}))      # closed since
    board = make_board(monkeypatch, [
        card(5), card(6, milestone=1), card(7, labels=labels("Priority: Low")),
        card(8, labels=labels("a", "b", "new"))], fields=ORG_FIELDS, plan_file=path)
    assert board.plan_info()["count"] == 5 and board.cards[5]["milestone"] is None   # not applied
    added, dropped = board.review_plan()
    assert dropped == 2 and not path.exists() and board.plan_info()["count"] == 0
    assert [(s["issue"], s["kind"], s["value"], s["source"]) for s in added] == [
        (5, "milestone", 1, "plan"), (7, "priority", "High", "plan"),
        (8, "labels", {"add": ["c"], "remove": ["b"]}, "plan")]
    assert added[0]["reason"] == "Planned in a read-only session on 2026-11-11."
    assert "It was none then and is Low now" in added[1]["reason"]
    board.resolve([added[2]["id"]], "accept")
    assert [l["name"] for l in board.cards[8]["labels"]] == ["a", "new", "c"]   # "new" survives


def test_a_plan_can_be_discarded_and_a_bad_file_is_no_plan(monkeypatch, tmp_path):
    path = tmp_path / "board.plan.json"
    path.write_text("{not json")
    board = make_board(monkeypatch, [card(5)], plan_file=path)
    assert board.plan_info()["count"] == 0 and board.review_plan() == ([], 0)
    path.write_text('{"changes": [{"issue": 5, "kind": "milestone", "value": 1}]}')
    assert board.plan_info()["count"] == 1
    board.discard_plan()
    assert not path.exists()
    assert server.plan_path("o/r", {"XDG_STATE_HOME": "/s"}) == Path("/s/milestones/board-o-r.plan.json")
