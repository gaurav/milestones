from milestones.cli import build_search_query, sort_key

BUCKETS = ["Soon", "Later", "Not urgent"]


def test_sort_key_orders_dated_then_buckets_then_other():
    milestones = [
        ("Not urgent", None),
        ("Zebra ideas", None),
        ("v2026.09", "2026-09-15"),
        ("Soon", None),
        ("v2025.01", "2025-01-01"),  # overdue: sorts first among dated
        ("Later", None),
    ]
    milestones.sort(key=lambda m: sort_key(m[0], m[1], BUCKETS))
    assert [m[0] for m in milestones] == [
        "v2025.01", "v2026.09", "Soon", "Later", "Not urgent", "Zebra ideas",
    ]


def test_build_search_query_dedupes_owners_and_fits_search_cap():
    repos = [
        "gaurav/milestones", "gaurav/taxondna",
        "NCATSTranslator/Babel", "TranslatorSRI/babel-validation",
        "heal-data-stewards/heal-cdes", "helxplatform/dug", "phyloref/phyx.js",
    ]
    q = build_search_query(repos)
    assert q.count("user:gaurav") == 1
    assert "(user:NCATSTranslator OR" in q  # advanced search ANDs bare qualifiers
    assert "no:milestone" in q and "is:issue" in q
    assert len(q) < 256  # GitHub search query length cap
