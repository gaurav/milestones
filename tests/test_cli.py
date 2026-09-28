import datetime
import io
import re
import tomllib
import unicodedata
from pathlib import Path

import pytest

from milestones.cli import (
    AHEAD, COLOR_NAMES, DEFAULT_BUCKETS, DISTANT, KINDS, LATE, SOON, TRIAGE_BUCKET,
    bucket_color, build_search_queries,
    _norm_issue, carries, closes_line, complete_titles, parse_answer, priority_options,
    pr_group, prs_query,
    date_choices, FOCUS_MARK, due_color, excerpt, favourite_date, fg, is_focused, issue_count,
    is_ignored, item_marker, load_config, triage_order,
    free_buckets, milestone_problems, org_colors, parse_ignore, parse_issue_ref, parse_repo,
    pct_color,
    print_findings, print_table, read_key, sort_key, triage_scope, triage_sources,
    triage_summary,
    untriaged_counts, visible,
    write_repo_list,
)

BUCKETS = ["Needed soon", "Needed later", "Not urgent"]


def test_sort_key_orders_dated_then_buckets_then_other():
    milestones = [
        ("Not urgent", None),
        ("Zebra ideas", None),
        ("v2026.09", "2026-09-15"),
        ("Needed soon", None),
        ("v2025.01", "2025-01-01"),  # overdue: sorts first among dated
        ("Needed later", None),
    ]
    milestones.sort(key=lambda m: sort_key(m[0], m[1], BUCKETS))
    assert [m[0] for m in milestones] == [
        "v2025.01", "v2026.09", "Needed soon", "Needed later", "Not urgent", "Zebra ideas",
    ]


def test_build_search_queries_scope_to_configured_repos_only():
    repos = [
        "gaurav/milestones", "gaurav/taxondna",
        "NCATSTranslator/Babel", "TranslatorSRI/babel-validation",
        "heal-data-stewards/heal-cdes", "helxplatform/dug", "phyloref/phyx.js",
    ]
    queries = build_search_queries(repos)
    joined = " ".join(queries)
    assert all("repo:" + r in joined for r in repos)
    assert "user:" not in joined  # owner-wide search let unconfigured repos crowd results out
    assert " OR " in queries[0]  # advanced search ANDs bare qualifiers
    assert all("no:milestone" in q and "is:issue" in q for q in queries)
    assert all(len(q) < 256 for q in queries)  # GitHub search query length cap
    assert all("is:pr" in q and "is:issue" not in q
               for q in build_search_queries(repos, kind="pr"))
    # None asks for both at once, which is how status counts what is untriaged.
    assert all("is:pr" not in q and "is:issue" not in q and "no:milestone" in q
               for q in build_search_queries(repos, kind=None))


def test_build_search_queries_ask_for_one_milestone_instead_of_none():
    repos = [f"owner{i}/some-repository-name" for i in range(20)]
    title = "A milestone whose title runs on for a good sixty characters or so"
    queries = build_search_queries(repos, milestone=title)
    assert all(f'milestone:"{title}"' in q and "no:milestone" not in q for q in queries)
    # A long qualifier just means fewer repos per query; every repo is still asked for.
    # The cap is inclusive, and the splitter fills right up to it.
    assert all(len(q) <= 256 for q in queries)
    assert all("repo:" + r in " ".join(queries) for r in repos)
    assert len(queries) > len(build_search_queries(repos))


def test_build_search_queries_split_to_stay_under_the_cap():
    repos = [f"owner{i}/some-repository-name" for i in range(20)]
    queries = build_search_queries(repos)
    assert len(queries) > 1
    assert all(len(q) < 256 for q in queries)
    joined = " ".join(queries)
    assert all("repo:" + r in joined for r in repos)


def test_load_config_rejects_repos_that_are_not_owner_slash_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "milestones.toml"
    # The ignore list is checked by the same rule; a bad entry there is just as fatal.
    config.write_text('repos = ["gaurav/milestones", "gaurav"]\n'
                      'ignore = ["https://github.com/gaurav/x"]\n')
    with pytest.raises(SystemExit) as excinfo:
        load_config()
    listed = str(excinfo.value).split("OWNER/NAME: ")[1]
    assert listed == "gaurav, https://github.com/gaurav/x"  # the good repo is not named

    config.write_text('repos = ["gaurav/milestones"]\n')
    assert load_config()["ignore"] == []  # optional, unlike repos, and empty by default


def test_excerpt_collapses_whitespace_and_truncates():
    assert excerpt("## Heading\n\nsome   body\ttext") == "## Heading some body text"
    assert excerpt(None) == ""
    long = excerpt("word " * 100)
    assert len(long) <= 200 and long.endswith(" …")


def test_parse_repo_accepts_urls_and_shorthand():
    for text in ["NCATSTranslator/translator-diagram",
                 "https://github.com/NCATSTranslator/translator-diagram",
                 "https://github.com/NCATSTranslator/translator-diagram.git",
                 "  github.com/NCATSTranslator/translator-diagram/  "]:
        assert parse_repo(text) == "NCATSTranslator/translator-diagram"
    for bad in ["translator-diagram", "https://github.com/NCATSTranslator/x/issues", ""]:
        with pytest.raises(SystemExit):
            parse_repo(bad)


def test_parse_issue_ref_reads_refs_urls_and_whole_list_lines():
    for text in ["NCATSTranslator/Babel#204",
                 "https://github.com/NCATSTranslator/Babel/issues/204",
                 "github.com/NCATSTranslator/Babel/pull/204/",
                 # A `triage --list` line, title and labels included; only the ref is read.
                 "NCATSTranslator/Babel#204  Fix the thing [again]  [bug, help wanted]"]:
        assert parse_issue_ref(text) == ("NCATSTranslator/Babel", 204), text
    for bad in ["NCATSTranslator/Babel", "#204", "a/b#x", "a/b#", "", "   "]:
        with pytest.raises(SystemExit):
            parse_issue_ref(bad)


def test_parse_ignore_reads_a_bare_owner_as_the_whole_owner():
    # `owner/*` is what the config stores, but an unquoted one is a shell glob, so a
    # bare owner — which can never be a repo — means the same thing.
    for text in ["gaurav", "gaurav/", "gaurav/*", "  gaurav/*  "]:
        assert parse_ignore(text) == "gaurav/*", text
    for text in ["gaurav/waif", "https://github.com/gaurav/waif", "github.com/gaurav/waif/"]:
        assert parse_ignore(text) == "gaurav/waif", text
    for bad in ["", "/", "https://github.com/gaurav/waif/issues"]:
        with pytest.raises(SystemExit):
            parse_ignore(bad)


def test_is_ignored_matches_a_repo_itself_or_its_owner():
    assert is_ignored("gaurav/waif", ["gaurav/waif"])
    assert is_ignored("gaurav/waif", ["gaurav/*"])
    assert is_ignored("gaurav/*", ["gaurav/*"])  # so a repeated owner isn't appended twice
    assert not is_ignored("gaurav/waif", ["gaurav/ideas", "other/*"])
    assert not is_ignored("gaurav/waif", [])
    # An owner entry says nothing about the repos already listed under it, and vice versa.
    assert not is_ignored("gaurav/*", ["gaurav/waif"])
    # Both sides fold case; GitHub answers with the canonical spelling, the list is typed.
    assert is_ignored("GAURAV/Waif", ["gaurav/waif"]) and is_ignored("GAURAV/Waif", ["Gaurav/*"])


def test_write_repo_list_leaves_the_rest_of_the_config_alone(tmp_path):
    path = tmp_path / "milestones.toml"
    path.write_text('# a comment\n\nbuckets = ["Soon"]\n\nrepos = [\n  "a/b",\n]\n\n# trailing\n')
    write_repo_list(path, "repos", ["a/b", "c/d"])
    assert path.read_text() == (
        '# a comment\n\nbuckets = ["Soon"]\n\nrepos = [\n  "a/b",\n  "c/d",\n]\n\n# trailing\n')


def test_write_repo_list_appends_an_optional_list_the_config_hasnt_got(tmp_path):
    path = tmp_path / "milestones.toml"
    path.write_text('repos = [\n  "a/b",\n]\n')
    write_repo_list(path, "ignore", ["c/d"])
    assert path.read_text() == 'repos = [\n  "a/b",\n]\n\nignore = [\n  "c/d",\n]\n'
    write_repo_list(path, "ignore", ["c/d", "e/f"])  # and rewrites it in place thereafter
    assert path.read_text() == (
        'repos = [\n  "a/b",\n]\n\nignore = [\n  "c/d",\n  "e/f",\n]\n')


def test_write_repo_list_refuses_a_list_it_cannot_rewrite(tmp_path):
    # Present but not as a bracketed list: appending a second one would be silent
    # corruption, so this is a hand-edit rather than a guess.
    path = tmp_path / "milestones.toml"
    path.write_text('repos = [\n  "a/b",\n]\nignore = "c/d"\n')
    with pytest.raises(SystemExit):
        write_repo_list(path, "ignore", ["c/d", "e/f"])


def problems(title, due=None, open_issues=1, closed_issues=0, today="2026-08-25", **prs):
    return milestone_problems(title, due, open_issues, closed_issues, BUCKETS, today, **prs)


def test_milestone_problems_accepts_versions_dates_and_buckets():
    for title, due in [("Babel v1.19", "2026-09-01"), ("Phyx.js v1.2.2", "2026-09-01"),
                       ("2026aug24", "2026-08-30"), ("Week ending 2026-08-31", "2026-08-31"),
                       ("Needed soon", None), ("Not urgent", None)]:
        assert problems(title, due) == [], title


def kinds(*args, **kwargs):
    return [kind for kind, _ in problems(*args, **kwargs)]


def test_milestone_problems_flags_names_dates_and_stale_milestones():
    assert kinds("Next release", "2026-09-01") == ["rename"]
    assert kinds("v2.0", "2026-08-19") == ["overdue"]
    # Two independent fixes: an undated milestone needs a date even if it also needs a name.
    assert kinds("Next release") == ["rename", "undated"]
    # A finished milestone is worth closing whether or not it is also overdue.
    assert problems("v2.0", "2026-09-01", open_issues=0, closed_issues=3) == [("done", "all 3 closed")]
    assert problems("v2.0", "2026-08-19", open_issues=4) == [
        ("overdue", "due 2026-08-19, 4 still open")]
    assert kinds("v2.0", "2026-09-01", open_issues=0) == ["empty"]
    # Buckets are meant to sit empty between triage rounds, and to outlive the
    # issues they held: closing one hides it from status and triage.
    assert problems("Needed later", None, open_issues=0) == []


def test_milestone_problems_counts_pull_requests_as_work():
    # GraphQL's issue counts leave PRs out, and a milestone holding only open PRs was
    # read as done — and closed with the PRs still on it. Every decision is over both.
    assert kinds("v2.0", "2026-09-01", open_issues=0, closed_issues=3, open_prs=2) == []
    assert problems("v2.0", "2026-08-19", open_issues=1, open_prs=2) == [
        ("overdue", "due 2026-08-19, 3 still open")]
    assert problems("v2.0", "2026-09-01", open_issues=0, closed_prs=2) == [("done", "all 2 closed")]
    assert kinds("v2.0", "2026-09-01", open_issues=0, open_prs=1) == []


def test_milestone_problems_reports_a_closed_milestone_only_as_stranded():
    # Work on a closed milestone is invisible everywhere else, so it is the one thing
    # check says about a closed milestone — whatever else is wrong with its name or date,
    # and whether or not it is a bucket.
    assert problems("Needs tests", None, open_issues=50, closed=True) == [
        ("stranded", "closed with 50 still open")]
    assert kinds("Improved testing", None, open_issues=0, open_prs=3, closed=True) == ["stranded"]
    assert kinds("Needed soon", None, open_issues=2, closed=True) == ["stranded"]
    assert kinds("Babel v1.18", "2026-07-20", open_issues=0, closed_issues=13, closed=True) == []
    assert kinds("Old plans", None, open_issues=0, closed_issues=0, closed=True) == []
    assert problems("Needed later", None, open_issues=0, closed_issues=3) == []


def test_free_buckets_offers_renames_only_in_a_repo_that_uses_buckets():
    # A repo using none of them has opted out of this much triage, and a rename is no way
    # to opt it back in. Config order survives, so the walk numbers them in config order.
    assert free_buckets(BUCKETS, set()) == []
    assert free_buckets(BUCKETS, {"v1.2", "Backlog"}) == []
    assert free_buckets(BUCKETS, {"Not urgent"}) == ["Needed soon", "Needed later"]
    assert free_buckets(BUCKETS, {"Needed later", "v1.2"}) == ["Needed soon", "Not urgent"]
    assert free_buckets(BUCKETS, set(BUCKETS)) == []
    assert free_buckets([], {"v1.2"}) == []  # buckets switched off in the config entirely
    # Critical is a bucket like the others here: a target, and enough to count as using them.
    assert free_buckets(["Critical", *BUCKETS], set(BUCKETS)) == ["Critical"]
    assert free_buckets(["Critical", *BUCKETS], {"Critical"}) == BUCKETS


def test_date_choices_lands_on_the_next_monday_and_the_first_of_next_month():
    monday = datetime.date(2026, 8, 24)
    for offset in range(7):  # every weekday resolves to the *following* Monday
        chosen = dict((label, d) for _, label, d in date_choices(monday + datetime.timedelta(offset)))
        assert chosen["next Monday"] == monday + datetime.timedelta(7), offset
        assert chosen["next Monday"].weekday() == 0
        assert chosen["start of next month"] == datetime.date(2026, 9, 1), offset
    # December has to roll the year over, and February is the short month.
    assert dict((l, d) for _, l, d in date_choices(datetime.date(2026, 12, 31)))[
        "start of next month"] == datetime.date(2027, 1, 1)
    assert dict((l, d) for _, l, d in date_choices(datetime.date(2028, 2, 29)))[
        "start of next month"] == datetime.date(2028, 3, 1)


def test_date_choices_offers_back_the_session_favourite():
    today, meeting = datetime.date(2026, 8, 24), datetime.date(2026, 9, 3)
    key, label, date = date_choices(today, meeting)[-1]
    assert (key, date) == ("x", meeting) and "Thu" in label  # 2026-09-03 is a Thursday
    # The presets stay put; only the fourth slot follows the session.
    assert date_choices(today, meeting)[:3] == date_choices(today)[:3]


def test_favourite_date_prefers_the_most_used_then_the_most_recent():
    a, b = datetime.date(2026, 9, 3), datetime.date(2026, 9, 10)
    assert favourite_date([]) is None
    assert favourite_date([a, b, a]) == a
    assert favourite_date([a, b]) == b  # tie: whichever was typed last


def test_issue_count_reads_naturally():
    assert [issue_count(n) for n in (0, 1, 42)] == ["(0 issues)", "(1 issue)", "(42 issues)"]
    assert issue_count(0, 1) == "(0 issues, 1 PR)"
    assert issue_count(3, 2) == "(3 issues, 2 PRs)"


def test_read_key_takes_one_character_from_a_piped_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("skip\n"))
    assert read_key("choose: ") == "s"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # closed stdin ends the walk
    with pytest.raises(SystemExit):
        read_key("choose: ")


def finding(kind, repo, title, detail="", issues=0, prs=0):
    return {"kind": kind, "repo": repo, "title": title, "detail": detail, "issues": issues,
            "prs": prs, "url": f"https://github.com/{repo}/milestone/1", "number": 1}


def test_print_findings_groups_by_fix_and_aligns_within_a_group(capsys):
    print_findings([
        finding("rename", "owner/one", "Next release", issues=3),
        finding("rename", "owner/a-much-longer-repo", "Later", issues=1),
        finding("overdue", "owner/one", "v2.0", "due 2026-08-19, 4 still open", issues=12),
    ])
    out = capsys.readouterr().out
    assert out.startswith("3 fixes across 2 repos.")
    assert KINDS["rename"] in out and KINDS["overdue"] in out
    # Same group, so the counts line up under each other whatever the repo name's length.
    rename_lines = [l for l in out.splitlines() if "(3 issues)" in l or "(1 issue)" in l]
    assert len(rename_lines) == 2
    assert len({l.index("(") for l in rename_lines}) == 1
    assert "  https://github.com/owner/one/milestone/1" in out  # a URL under every finding


def test_org_colors_only_colours_owners_with_more_than_one_repo():
    repos = ["a/one", "b/only", "a/two", "c/x", "c/y"]
    colors = org_colors(repos)
    assert set(colors) == {"a", "c"}
    assert colors["a"] != colors["c"]  # distinct hues
    # The caller hands these over sorted by due date, so an owner must not change colour
    # just because a milestone came or went.
    assert org_colors(list(reversed(repos))) == colors


def test_org_colors_honours_the_config_even_for_a_single_repo():
    colors = org_colors(["a/one", "b/only", "c/x", "c/y"],
                        {"B": COLOR_NAMES["pink"], "c": COLOR_NAMES["pink"]})
    # A hand-picked colour applies however few repos the owner has, and two owners may
    # share one; "a" is left plain, owning only one repo and named by nobody.
    assert colors == {"b": fg(COLOR_NAMES["pink"]), "c": fg(COLOR_NAMES["pink"])}


def test_due_color_bands():
    today = "2026-09-08"
    assert due_color(None, today) is None
    assert due_color("2026-09-07", today) == LATE
    assert due_color("2026-09-08", today) == SOON       # due today is still "this week"
    assert due_color("2026-09-15", today) == SOON       # exactly 7 days
    assert due_color("2026-09-16", today) == AHEAD
    assert due_color("2026-10-08", today) == AHEAD      # exactly 30 days
    assert due_color("2026-10-09", today) == DISTANT


def test_bucket_color_covers_every_default_bucket_only_while_it_holds_work():
    # Every standing bucket has a colour, so a row is never plain by accident; an empty one is
    # plain on purpose, so that a quiet "Needed soon" reads as quiet.
    assert all(bucket_color(b, 1) for b in DEFAULT_BUCKETS)
    assert not any(bucket_color(b, 0) for b in DEFAULT_BUCKETS)
    # Needs triage is a state, not a level of urgency, so it is off the due-date ramp.
    assert bucket_color(TRIAGE_BUCKET, 1) not in {"1;" + LATE, LATE, SOON, AHEAD, DISTANT}
    assert bucket_color("Some release v1.2", 5) is None


def test_pct_color_ramps_up_from_halfway():
    assert pct_color(0, 0) is None                      # nothing ever filed
    # Nothing under halfway is coloured as progress, and none of it as a warning.
    grey = pct_color(0, 10)
    assert grey == pct_color(49, 100) == DISTANT
    greens = [pct_color(n, 100) for n in (50, 65, 80, 95)]
    assert len(set(greens)) == 4 and grey not in greens  # a four-step ramp above halfway
    assert pct_color(64, 100) == greens[0]               # each band runs up to the next
    assert pct_color(4, 4) == greens[-1]                 # everything closed is the brightest


def test_print_table_aligns_around_escape_sequences(capsys):
    print_table([("plain", 7), ("\x1b[38;5;196mabc\x1b[0m", 7)], ("NAME", "N"), right=("N",))
    lines = capsys.readouterr().out.splitlines()
    # The coloured cell pads out to the three characters you can see, not to the dozen
    # bytes it takes to say them, so every row is the same width on screen.
    assert {visible(line) for line in lines} == {len("plain  N")}


def test_write_repo_list_adds_a_missing_key_above_any_table(tmp_path):
    config = tmp_path / "milestones.toml"
    config.write_text('repos = ["a/one"]\n\n# What the colours are.\n[colors]\n'
                      'gaurav = "purple"\n')
    write_repo_list(config, "ignore", ["b/two"])
    text = config.read_text()
    # Under the [colors] header it would have been read back as colors.ignore — and
    # between the comment and the header it would look like the comment described it.
    assert text.index("ignore = [") < text.index("# What the colours are.")
    assert tomllib.loads(text)["ignore"] == ["b/two"]


def test_is_focused_folds_case_and_needs_the_whole_name():
    focus = ["gaurav/Milestones"]
    assert is_focused("gaurav/milestones", focus)   # GitHub's spelling vs the config's
    assert not is_focused("gaurav/milestones-old", focus)
    assert not is_focused("gaurav/other", focus)
    assert not is_focused("gaurav/anything", [])


def test_print_table_drops_a_column_that_is_empty_all_the_way_down(capsys):
    # The focus column with nothing focused: it should cost no indent at all.
    print_table([("", "a/one"), ("", "b/two")], ("", "REPO"))
    assert [line[0] for line in capsys.readouterr().out.splitlines()] == ["R", "a", "b"]


def test_focus_marker_is_one_column_wide():
    # An East Asian Ambiguous glyph — U+2605 BLACK STAR, say — is drawn two columns wide
    # in a CJK-configured terminal, which shifts every focused row by one. print_table
    # measures in characters and cannot see that happen.
    assert len(FOCUS_MARK) == 1
    assert unicodedata.east_asian_width(FOCUS_MARK) in ("N", "Na")


def test_load_config_resolves_colour_names_and_rejects_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "milestones.toml"
    config.write_text('repos = ["gaurav/milestones"]\n'
                      '[colors]\ngaurav = "purple"\nphyloref = 186\n')
    # Names and raw 256-colour numbers both come back as numbers, so nothing downstream
    # has to know which the config used.
    assert load_config()["colors"] == {"gaurav": COLOR_NAMES["purple"], "phyloref": 186}

    # A colour that can't resolve is fatal at load, rather than a stray escape sequence
    # in the middle of the table.
    # `true` included: isinstance(True, int) would otherwise make it colour 1.
    for bad in ('"chartreuse"', "256", "-1", "true"):
        config.write_text(f'repos = ["gaurav/milestones"]\n[colors]\ngaurav = {bad}\n')
        with pytest.raises(SystemExit) as excinfo:
            load_config()
        assert "gaurav" in str(excinfo.value) and "purple" in str(excinfo.value)


def test_load_config_defaults_focus_to_nothing_and_checks_its_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "milestones.toml"
    config.write_text('repos = ["gaurav/milestones"]\n')
    assert load_config()["focus"] == []

    config.write_text('repos = ["gaurav/milestones"]\nfocus = ["gaurav"]\n')
    with pytest.raises(SystemExit) as excinfo:
        load_config()
    assert str(excinfo.value).endswith("OWNER/NAME: gaurav")


def test_triage_order_puts_focused_repos_first_without_reshuffling_the_rest():
    issues = [{"repo": "a/one", "updated": "2026-09-01", "n": 1},
              {"repo": "b/two", "updated": "2026-09-03", "n": 2},
              {"repo": "a/one", "updated": "2026-09-05", "n": 3},
              {"repo": "b/two", "updated": "2026-09-02", "n": 4}]
    assert [i["n"] for i in triage_order(issues, [])] == [3, 2, 4, 1]
    # b/two's two issues come first, and stay freshest-first among themselves; the rest
    # keep the order they had. Sorting on one compound key with reverse=True would put
    # the *unfocused* repos first instead.
    assert [i["n"] for i in triage_order(issues, ["B/Two"])] == [2, 4, 3, 1]


def test_milestones_md_lists_the_default_buckets():
    # MILESTONES.md is what agents in other repos read to learn the buckets, so its table
    # has to name the same ones, in the same order, as the code creates.
    doc = (Path(__file__).parent.parent / "MILESTONES.md").read_text()
    assert re.findall(r"^\| `([^`]+)`", doc, re.M) == DEFAULT_BUCKETS


def test_prs_query_is_authored_by_default_and_ors_in_the_rest():
    assert prs_query() == "is:pr is:open (author:@me)"
    assert prs_query(assigned=True, review_requested=True, mentions=True) == (
        "is:pr is:open (author:@me OR assignee:@me OR review-requested:@me OR mentions:@me)")


def test_pr_group_prefers_tracked_then_ignored():
    config = {"repos": ["gaurav/milestones"], "ignore": ["gaurav/milestones", "phyloref/*"]}
    assert pr_group("Gaurav/Milestones", config) == "tracked"
    assert pr_group("phyloref/klados", config) == "ignored"
    assert pr_group("rambaut/figtree", config) == "untracked"


def test_item_marker_names_prs_and_drafts_only():
    assert item_marker({"pr": False, "draft": False}) == ""
    assert item_marker({"pr": True, "draft": False}) == "(PR)"
    assert item_marker({"pr": True, "draft": True}) == "(PR, draft)"


def test_untriaged_counts_splits_issues_from_prs_per_repo():
    items = [{"repo": "b/two", "pr": True}, {"repo": "A/one", "pr": False},
             {"repo": "b/two", "pr": False}, {"repo": "b/two", "pr": False}]
    assert untriaged_counts(items) == [{"repo": "A/one", "issues": 1, "prs": 0},
                                       {"repo": "b/two", "issues": 2, "prs": 1}]
    assert untriaged_counts([]) == []


def test_closes_line_says_where_each_linked_issue_is():
    assert closes_line([], "A/one") == ""
    assert closes_line([{"number": 12, "state": "OPEN", "milestone": None, "repo": "A/one"},
                        {"number": 7, "state": "OPEN", "milestone": "v1.2", "repo": "a/ONE"},
                        {"number": 3, "state": "CLOSED", "milestone": None, "repo": "A/one"},
                        {"number": 5, "state": "OPEN", "milestone": None, "repo": "B/two"}],
                       "A/one") == (
        "closes #12 (no milestone), #7 (v1.2), #3 (closed), B/two#5 (no milestone)")


def test_a_pr_carries_only_open_unplaced_issues_in_its_own_repo():
    def issue(state="OPEN", milestone=None, repo="A/one"):
        return {"state": state, "milestone": milestone, "repo": repo}
    assert carries(issue(), "A/one")
    assert carries(issue(repo="a/One"), "A/one")  # GitHub's spelling needn't match the config's
    assert not carries(issue(milestone="v1.2"), "A/one")  # somebody already placed it
    assert not carries(issue(state="CLOSED"), "A/one")
    # Its milestone number here would name some other milestone there, or none.
    assert not carries(issue(repo="B/two"), "A/one")


def test_complete_titles_matches_a_prefix_of_the_whole_title_ignoring_case():
    titles = ["Babel v1.19", "Babel v1.20", "Needed soon", "Not urgent"]
    assert complete_titles(titles, "babel v1.2") == ["Babel v1.20"]
    # The whole line is the prefix, spaces included, so "n" is not two words.
    assert complete_titles(titles, "N") == ["Needed soon", "Not urgent"]
    assert complete_titles(titles, "") == titles
    assert complete_titles(titles, "v1") == []


def test_parse_answer_splits_a_choice_from_its_one_modifier():
    assert parse_answer("2") == ("2", "")
    assert parse_answer("12!") == ("12", "!")
    assert parse_answer(" C- ") == ("c", "-")
    assert parse_answer("2^") == ("2", "^")
    assert parse_answer("c$") == ("c", "$")
    for other in ["s", "o", "q", "", "2!!", "!2", "x", "2 3"]:
        assert parse_answer(other) is None, other


def test_priority_options_needs_the_whole_single_select_priority_field():
    def field(name, options, kind="IssueFieldSingleSelect"):
        return {"__typename": kind, "id": f"F_{name}", "name": name,
                "options": [{"id": f"O_{o}", "name": o} for o in options]}
    # NCATSTranslator's shape: Urgent/High/Medium/Low, alongside an Effort field.
    fields = [field("Effort", ["High", "Medium", "Low"]),
              field("Priority", ["Urgent", "High", "Medium", "Low"])]
    assert priority_options(fields) == {"!": ("F_Priority", "O_Urgent"),
                                        "+": ("F_Priority", "O_High"),
                                        "-": ("F_Priority", "O_Low")}
    assert priority_options([field("priority", ["urgent", "high", "low"])]) is not None
    # Half a field is no field: a key that can't be honoured is not offered.
    assert priority_options([field("Priority", ["High", "Low"])]) is None
    assert priority_options([{"__typename": "IssueFieldText", "id": "F", "name": "Priority"}]) is None
    assert priority_options([]) is None


def test_norm_issue_reads_a_pr_and_its_draft_flag_from_either_api_shape():
    base = {"number": 7, "title": "T", "body": None, "labels": [{"name": "bug"}],
            "updated_at": "2026-09-27T00:00:00Z", "html_url": "u", "node_id": "I_x"}
    issue = _norm_issue(base, "a/b")
    assert (issue["pr"], issue["draft"], issue["labels"], issue["id"]) == (False, False, ["bug"], "I_x")
    # A PR is the same record with a `pull_request` key; `draft` rides along on it.
    assert _norm_issue({**base, "pull_request": {}, "draft": True}, "a/b")["draft"] is True
    assert _norm_issue({**base, "pull_request": {}}, "a/b")["pr"] is True
    # The milestone an item is already on, for a walk that starts from one.
    assert issue["milestone"] is None
    assert _norm_issue({**base, "milestone": None}, "a/b")["milestone"] is None
    assert _norm_issue({**base, "milestone": {"title": "Needs triage", "number": 3}},
                       "a/b")["milestone"] == "Needs triage"


def test_triage_sources_default_to_none_then_the_triage_bucket_where_configured():
    assert triage_sources({"buckets": DEFAULT_BUCKETS}, None) == [None, TRIAGE_BUCKET]
    # A config that lists its buckets by hand and hasn't added the new one walks as before.
    assert triage_sources({"buckets": ["Needed soon"]}, None) == [None]
    # --from replaces the default outright, keeps its order, and folds duplicates.
    assert triage_sources({"buckets": DEFAULT_BUCKETS}, ["v1.2", "None", "v1.2"]) == ["v1.2", None]
    assert triage_sources({"buckets": DEFAULT_BUCKETS}, ["none"]) == [None]
    with pytest.raises(SystemExit, match="quote"):
        triage_sources({"buckets": DEFAULT_BUCKETS}, ['Say "when"'])


def test_triage_scope_names_one_repo_by_url_and_counts_several_in_walk_order():
    items = [{"repo": "B/two"}, {"repo": "A/one"}, {"repo": "B/two"}]
    assert triage_scope(items, "PR", "A/one", 9) == (
        "3 PRs without a milestone in https://github.com/A/one", "")
    assert triage_scope(items, "issue", None, 9) == (
        "3 issues without a milestone in 2 of 9 tracked repos", "B/two (2), A/one (1)")
    assert triage_scope([], "issue", None, 1) == (
        "no issues without a milestone in 1 tracked repo", "")


def _done(**entries):
    done = {key: [] for key in ("assigned", "carried", "passed", "priority", "unprioritised",
                                "failed", "skipped", "created", "reopened", "unordered")}
    return done | entries


def test_triage_summary_counts_by_milestone_and_says_what_is_left():
    done = _done(assigned=[("A/one#1", "Needed soon"), ("A/one#2", "v1.2"),
                           ("A/one#3", "Needed soon")],
                 carried=[("A/one#9", "v1.2")],
                 priority=[("A/one#1", "Low"), ("A/one#3", "Urgent"), ("A/one#9", "Low")],
                 skipped=["A/one#4"],
                 created=[("A/one", "v1.2"), ("A/one", "Critical")],
                 unordered=[("v1.2", "https://x/1", "top")])
    assert triage_summary("7 PRs without a milestone in X", 7, done, ["Critical"],
                          "milestones triage --prs") == [
        "Of 7 PRs without a milestone in X:",
        "  - Assigned 3: 2 to Needed soon, 1 to v1.2",
        "  - Also assigned 1 issue they close to v1.2",
        # In the keys' order, Urgent first, not in the order they were set.
        "  - Set priority on 3: 1 Urgent, 2 Low",
        "  - Skipped 1: A/one#4",
        "  - Stopped at 5 of 7, with 3 left undecided",
        "  - Created v1.2 in A/one (undated: `milestones check` will ask for a date)",
        "  - Created Critical in A/one",
        "  - Not moved 1 to the top of v1.2: GitHub has no API for that "
        "(gaurav/milestones#25), so drag it at https://x/1",
        "  - 4 still without a milestone: `milestones triage --prs` walks them again, "
        "after ~10s for GitHub to catch up",
    ]


def test_triage_summary_of_a_walk_that_assigned_nothing_or_everything():
    skipped = [f"A/one#{n}" for n in range(1, 8)]
    assert triage_summary("7 issues", 7, _done(skipped=skipped), [], "milestones triage") == [
        "Of 7 issues:",
        "  - Assigned none",
        "  - Skipped 7: A/one#1, A/one#2, A/one#3, A/one#4, A/one#5 and 2 more",
        # No lag to wait out when nothing was written.
        "  - 7 still without a milestone: `milestones triage` walks them again",
    ]
    done = _done(assigned=[("A/one#1", "Upstream"), ("A/one#2", "Upstream")])
    assert triage_summary("2 issues", 2, done, [], "milestones triage") == [
        "Of 2 issues:", "  - Assigned 2 to Upstream"]
