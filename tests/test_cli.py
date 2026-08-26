import datetime

import pytest

from milestones.cli import (
    build_search_queries, date_choices, excerpt, load_config, milestone_problems, parse_repo,
    sort_key,
    write_repos,
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


def test_build_search_queries_split_to_stay_under_the_cap():
    repos = [f"owner{i}/some-repository-name" for i in range(20)]
    queries = build_search_queries(repos)
    assert len(queries) > 1
    assert all(len(q) < 256 for q in queries)
    joined = " ".join(queries)
    assert all("repo:" + r in joined for r in repos)


def test_load_config_rejects_repos_that_are_not_owner_slash_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "milestones.toml").write_text(
        'repos = ["gaurav/milestones", "gaurav", "https://github.com/gaurav/x"]\n')
    with pytest.raises(SystemExit) as excinfo:
        load_config()
    listed = str(excinfo.value).split("OWNER/NAME: ")[1]
    assert listed == "gaurav, https://github.com/gaurav/x"  # the good repo is not named


def test_excerpt_collapses_whitespace_and_truncates():
    assert excerpt("## Heading\n\nsome   body\ttext") == "## Heading some body text"
    assert excerpt(None) == ""
    long = excerpt("word " * 100)
    assert len(long) == 201 and long.endswith("…")


def test_parse_repo_accepts_urls_and_shorthand():
    for text in ["NCATSTranslator/translator-diagram",
                 "https://github.com/NCATSTranslator/translator-diagram",
                 "https://github.com/NCATSTranslator/translator-diagram.git",
                 "  github.com/NCATSTranslator/translator-diagram/  "]:
        assert parse_repo(text) == "NCATSTranslator/translator-diagram"
    for bad in ["translator-diagram", "https://github.com/NCATSTranslator/x/issues", ""]:
        with pytest.raises(SystemExit):
            parse_repo(bad)


def test_write_repos_leaves_the_rest_of_the_config_alone(tmp_path):
    path = tmp_path / "milestones.toml"
    path.write_text('# a comment\n\nbuckets = ["Soon"]\n\nrepos = [\n  "a/b",\n]\n\n# trailing\n')
    write_repos(path, ["a/b", "c/d"])
    assert path.read_text() == (
        '# a comment\n\nbuckets = ["Soon"]\n\nrepos = [\n  "a/b",\n  "c/d",\n]\n\n# trailing\n')


def problems(title, due=None, open_issues=1, closed_issues=0, today="2026-08-25"):
    return milestone_problems(title, due, open_issues, closed_issues, BUCKETS, today)


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
    # Buckets are meant to sit empty between triage rounds.
    assert problems("Needed later", None, open_issues=0) == []


def test_date_choices_lands_on_the_next_monday():
    monday = datetime.date(2026, 8, 24)
    for offset in range(7):  # every weekday resolves to the *following* Monday
        chosen = dict((label, d) for _, label, d in date_choices(monday + datetime.timedelta(offset)))
        assert chosen["next Monday"] == monday + datetime.timedelta(7), offset
        assert chosen["next Monday"].weekday() == 0
