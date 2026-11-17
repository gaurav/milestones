"""The app's terminal and its check of the built front end: the build checks against a
scratch `static/` and `ui/`, and the lines the log says about reads and writes."""

import os

from milestones.board import build, server


def page(root, rel, refs=()):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'<script src="{r}"></script>' for r in refs))
    return path


def sources(tmp_path):
    ui = tmp_path / "ui"
    for name in ("home", "board"):
        page(ui, f"{name}/index.html")
        (ui / name / "main.tsx").write_text("")
    (ui / "shared").mkdir()
    (ui / "shared" / "base.css").write_text("body {}")
    return ui


def test_a_whole_build_has_no_problems(tmp_path):
    ui, static = sources(tmp_path), tmp_path / "static"
    page(static, "index.html", ["/main.js", "/base.css"])
    page(static, "board/index.html", ["/board/main.js", "/base.css"])
    for rel in ("main.js", "base.css", "board/main.js"):
        (static / rel).write_text("")
    assert build.problems(static, ui) == []


def test_a_watcher_left_on_an_old_build_script_is_caught(tmp_path):
    # What `npm run dev` started before the pages had folders of their own leaves behind:
    # the board's new HTML at the root, naming files that only the new build makes.
    ui, static = sources(tmp_path), tmp_path / "static"
    page(static, "index.html", ["/board/main.js", "/base.css"])
    (static / "main.js").write_text("")
    assert build.problems(static, ui) == [
        "ui/board/ has not been built (no static/board/index.html)",
        "static/index.html loads /board/main.js, which the build doesn't have",
        "static/index.html loads /base.css, which the build doesn't have",
    ]


def test_an_empty_static_folder_is_a_problem_even_without_sources(tmp_path):
    assert build.problems(tmp_path / "static", tmp_path / "no-ui") == [
        "static/index.html is missing"]


def test_warnings_notice_a_copied_file_or_a_script_newer_than_the_build(tmp_path):
    ui, static = sources(tmp_path), tmp_path / "static"
    page(static, "index.html")
    page(static, "board/index.html")
    (static / "base.css").write_text("body { color: red }")
    for rel in ("main.js", "board/main.js"):
        (static / rel).write_text("")
        os.utime(static / rel, (1, 1))
    assert build.warnings(static, ui) == [
        "static/base.css differs from ui/shared/base.css",
        "2 scripts in ui/ changed since the last build (ui/board/main.tsx, …)",
    ]


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
