"""Read a decision from wherever it was written down; write it one way.

Priority and effort are written as labels — `Priority: High`, `Effort: M` — because a label
is the one record an issue and a pull request can both carry: the organisation's issue fields
of those names are issues-only. They have also been recorded in those fields, as
`Priority: Critical` and `Size: M` labels, and under labels of a repo's own (the config's
`[labels]`). All of those are read; only the standard label is written, and the rules below
suggest moving whatever else is found onto it, so the next triage has one place to look.
All of this is pure: cards in, suggestions out. LABELS.md says the same to people."""

import re

# kind -> the standard labels (`<field>: <option>`, options in column order), their colours
# and meanings; the label prefixes that have carried it; and what each spelling found under
# those prefixes, or in the organisation field named `field`, means as an option. LABELS.md
# repeats the options, colours and meanings, and a test holds the two together.
KINDS = {
    "priority": {
        "field": "Priority", "prefix": "priority",
        "options": ["Urgent", "High", "Medium", "Low"],
        "colors": {"Urgent": "d2042d", "High": "b60205", "Medium": "fbca04", "Low": "009800"},
        "meanings": {"Urgent": "Drop other work for this.",
                     "High": "Do it before ordinary work.",
                     "Medium": "Ordinary work, in its turn.",
                     "Low": "Worth doing once the rest is done."},
        "values": {"critical": "Urgent", "urgent": "Urgent", "high": "High",
                   "medium": "Medium", "low": "Low"}},
    "effort": {
        "field": "Effort", "prefix": "(?:size|effort)",
        # Largest first, as priority runs from Urgent down: the two read the same way.
        "options": ["XL", "L", "M", "S", "XS"],
        "colors": {"XS": "00ff00", "S": "0e8a16", "M": "fbca04", "L": "b60205", "XL": "ff0000"},
        "meanings": {"XS": "Under an hour.", "S": "About half a day.", "M": "A day or two.",
                     "L": "About a week.",
                     "XL": "More than a week, or needs splitting up first."},
        "values": {"xs": "XS", "s": "S", "small": "S", "low": "S", "m": "M", "medium": "M",
                   "l": "L", "large": "L", "high": "L", "xl": "XL"}},
}


def standard_label(kind: str, value: str) -> str:
    return f"{KINDS[kind]['field']}: {value}"


STANDARD = {standard_label(kind, option).lower(): (kind, option)
            for kind, spec in KINDS.items() for option in spec["options"]}


def label_value(kind: str, name: str, aliases: dict[str, str] | None = None) -> str | None:
    """What a label says about `kind`, as one of its options: "Priority: Critical" is Urgent,
    "Size: M" is M, and a label the repo's `aliases` name says what its standard label does.
    None for a label that isn't about it, or says something the table doesn't know — which
    is left alone rather than guessed at."""
    name = name.strip()
    name = (aliases or {}).get(name.lower(), name)
    spec = KINDS[kind]
    match = re.fullmatch(rf"{spec['prefix']}\s*:\s*(.+)", name, re.IGNORECASE)
    return spec["values"].get(match[1].strip().lower()) if match else None


def labels_about(kind: str, card: dict) -> list[str]:
    """The card's labels that record `kind` under its prefixes, mapped or not: what setting
    it replaces. An alias is not among them — it is a repo's own label, read and left."""
    prefix = KINDS[kind]["prefix"]
    return [l["name"] for l in card["labels"]
            if re.match(rf"{prefix}\s*:", l["name"].strip(), re.IGNORECASE)]


def reconcile(kind: str, card: dict, aliases: dict[str, str] | None = None) -> dict:
    """Every record of `kind` on a card: `{standard, others: [(name, value)], field, agreed}`.
    `standard` is the options its standard labels give (one, unless someone set two);
    `others` the remaining labels about it and the aliases; `field` the organisation
    field's value as an option, and `agreed` the one value everything outside the standard
    label gives, or None when there is none or they differ."""
    spec = KINDS[kind]
    standard, others = [], []
    for label in card["labels"]:
        name = label["name"].strip()
        if STANDARD.get(name.lower(), ("",))[0] == kind:
            standard.append(STANDARD[name.lower()][1])
        elif name in labels_about(kind, card) or label_value(kind, name, aliases):
            others.append((label["name"], label_value(kind, name, aliases)))
    raw = (card.get("fields") or {}).get(spec["field"])
    field = spec["values"].get(raw.lower()) if isinstance(raw, str) else None
    said = {value for _, value in others if value} | ({field} if field else set())
    return {"standard": standard, "others": others, "field": field, "raw_field": raw,
            "agreed": said.pop() if len(said) == 1 else None}


def missing_standard(names: list[str], existing: list[str]) -> list[dict]:
    """The standard labels among `names` that a repo with the labels `existing` hasn't got,
    as the bodies that create them: GitHub would make each on first use anyway, but grey and
    with no description."""
    have = {name.lower() for name in existing}
    made = []
    for name in names:
        kind, option = STANDARD.get(name.lower(), (None, None))
        if kind and name.lower() not in have:
            made.append({"name": standard_label(kind, option),
                         "color": KINDS[kind]["colors"][option],
                         "description": KINDS[kind]["meanings"][option]})
    return made


def rule_suggestions(card: dict, aliases: dict[str, str] | None = None) -> list[dict]:
    """What the board itself proposes for a card, as `{issue, kind, value, reason, source}`.

    The standard label is the record. Where there is none and everything else agrees — an
    old label, a repo's own, the organisation field — the suggestion is to set it, which
    replaces the old labels. Where there is one, what repeats or contradicts it is suggested
    away: old labels removed, the field cleared. The label wins a disagreement because it is
    what this tool writes; GitHub does not say when a field was last changed, so "whichever
    is newer" can't be read."""
    made = []
    stale = []
    for kind, spec in KINDS.items():
        found = reconcile(kind, card, aliases)
        if len(found["standard"]) > 1:
            continue  # two standard labels: yours to settle
        if not found["standard"]:
            if found["agreed"]:
                sources = [repr(name) for name, value in found["others"] if value]
                if found["field"]:
                    sources.append(f"{spec['field']} field")
                made.append({"issue": card["number"], "kind": kind, "value": found["agreed"],
                             "source": f"rule:{kind}-label",
                             "reason": f"The {' and the '.join(sources)} say"
                                       f"{'s' if len(sources) == 1 else ''} {found['agreed']}, "
                                       f"but there is no '{standard_label(kind, found['agreed'])}'"
                                       " label, which is where it is kept."})
            continue  # records that disagree and no label: yours to settle
        value = found["standard"][0]
        label = standard_label(kind, value)
        old = [name for name, mapped in found["others"]
               if mapped and name in labels_about(kind, card)]
        if old:
            differ = [n for n in old if label_value(kind, n) != value]
            stale.append((old, f"'{label}' is set; the label{'s' if len(old) > 1 else ''} "
                          f"{', '.join(repr(n) for n in old)} "
                          + (f"say{'' if len(old) > 1 else 's'} otherwise. '{label}' is kept."
                             if differ else f"repeat{'' if len(old) > 1 else 's'} it.")))
        if found["raw_field"]:
            same = found["field"] == value
            made.append({"issue": card["number"], "kind": f"{kind}-field", "value": None,
                         "source": f"rule:clear-{kind}-field",
                         "reason": f"'{label}' is set, and the {spec['field']} field "
                                   + ("repeats it" if same else
                                      f"says {found['raw_field']}. The label is kept")
                                   + ": a label is the one record a pull request can carry too."})
    if stale:
        # A change to the labels, not the set that results: the card's labels may have moved
        # on by the time this is accepted.
        gone = {name for remove, _ in stale for name in remove}
        made.append({"issue": card["number"], "kind": "labels",
                     "value": {"add": [], "remove": [l["name"] for l in card["labels"]
                                                     if l["name"] in gone]},
                     "source": "rule:remove-old-label",
                     "reason": " ".join(reason for _, reason in stale)})
    return made
