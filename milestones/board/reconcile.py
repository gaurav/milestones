"""Read a decision from wherever it was written down; write it one way.

Priority and effort have been recorded as organisation issue fields, as `Priority: X` and
`Size: X` labels, and in project fields. The board reads the first two (a project's fields
need a token most people haven't got), shows whichever it finds, and writes only the field —
then suggests removing what the field has made redundant, so the next triage has one place
to look. All of this is pure: cards in, suggestions out."""

import re

# kind -> the organisation issue field of that name, the label prefix that used to carry it,
# and what each old label value means in the field's terms.
KINDS = {
    "priority": {"field": "Priority", "prefix": "priority",
                 "values": {"critical": "Urgent", "urgent": "Urgent", "high": "High",
                            "medium": "Medium", "low": "Low"}},
    "effort": {"field": "Effort", "prefix": "(?:size|effort)",
               "values": {"xs": "Low", "s": "Low", "small": "Low", "low": "Low",
                          "m": "Medium", "medium": "Medium", "l": "High", "large": "High",
                          "xl": "High", "high": "High"}},
}


def label_value(kind: str, name: str) -> str | None:
    """What a label says about `kind`, in the field's terms: "Priority: Critical" is Urgent,
    "Size: M" is Medium effort. None for a label that isn't about it, or says something the
    mapping doesn't know — which is left alone rather than guessed at."""
    spec = KINDS[kind]
    match = re.fullmatch(rf"{spec['prefix']}\s*:\s*(.+)", name.strip(), re.IGNORECASE)
    return spec["values"].get(match[1].strip().lower()) if match else None


def labels_about(kind: str, card: dict) -> list[str]:
    """The card's labels that record `kind`, mapped or not."""
    prefix = KINDS[kind]["prefix"]
    return [l["name"] for l in card["labels"]
            if re.match(rf"{prefix}\s*:", l["name"].strip(), re.IGNORECASE)]


def reconcile(kind: str, card: dict) -> dict:
    """Every record of `kind` on a card: `{field, labels: [(name, value)], agreed}`. `agreed`
    is the one value all of them give, or None when there is none or they differ."""
    field = (card.get("fields") or {}).get(KINDS[kind]["field"])
    labels = [(name, label_value(kind, name)) for name in labels_about(kind, card)]
    said = {value for _, value in labels if value} | ({field} if field else set())
    return {"field": field, "labels": labels, "agreed": said.pop() if len(said) == 1 else None}


def rule_suggestions(card: dict, fields: dict) -> list[dict]:
    """What the board itself proposes for a card, as `{issue, kind, value, reason, source}`.

    Only where the owner has the field to write to, and only for issues — a pull request has
    no fields. Where the records disagree the field wins: it is the newer mechanism, and
    GitHub does not say when a field was last changed, so "whichever is newer" can't be read.
    """
    if card["kind"] != "issue":
        return []
    made = []
    names = [l["name"] for l in card["labels"]]
    stale = []
    for kind, spec in KINDS.items():
        if spec["field"] not in fields:
            continue
        found = reconcile(kind, card)
        mapped = [(name, value) for name, value in found["labels"] if value]
        if not mapped:
            continue
        said = {value for _, value in mapped}
        if found["field"] is None:
            if len(said) == 1:
                name, value = mapped[0]
                made.append({"issue": card["number"], "kind": kind, "value": value,
                             "source": f"rule:{kind}-from-label",
                             "reason": f"The label '{name}' says {value}, and the "
                                       f"{spec['field']} field is empty."})
            continue  # two labels that disagree and no field: yours to settle
        agree = [name for name, value in mapped if value == found["field"]]
        differ = [name for name, value in mapped if value != found["field"]]
        if differ:
            stale.append((differ + agree, f"The {spec['field']} field says {found['field']}, "
                          f"but the label{'s' if len(differ) > 1 else ''} "
                          f"{', '.join(repr(n) for n in differ)} "
                          f"say{'' if len(differ) > 1 else 's'} otherwise. The field is kept."))
        else:
            stale.append((agree, f"The {spec['field']} field already says {found['field']}; "
                          f"the label{'s' if len(agree) > 1 else ''} "
                          f"{', '.join(repr(n) for n in agree)} "
                          f"repeat{'' if len(agree) > 1 else 's'} it."))
    if stale:
        # A change to the labels, not the set that results: the card's labels may have moved
        # on by the time this is accepted.
        gone = {name for remove, _ in stale for name in remove}
        made.append({"issue": card["number"], "kind": "labels",
                     "value": {"add": [], "remove": [n for n in names if n in gone]},
                     "source": "rule:remove-old-label",
                     "reason": " ".join(reason for _, reason in stale)})
    return made
