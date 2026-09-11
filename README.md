# milestones

I have measured out my life with GitHub milestones

A personal CLI for viewing and managing GitHub milestones across all my repositories.
Milestones are the single source of truth: dated milestones for "this release"/"next release",
plus standing undated buckets ("Needed soon", "Needed later", "Not urgent", "Upstream" — for
work that belongs in someone else's tracker) that never close. Every issue's
bucket is publicly visible on its GitHub issue page, so anyone can see how it's triaged and
complain in a comment if they disagree.

The buckets are per repo and opt-in: `setup` creates them where you want that much triage, and
a repo without them is simply left alone. Every command picks up whichever buckets a repo
actually has, and `check` asks for a missing one only where the repo already uses the others.

## Setup

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and an authenticated
[`gh`](https://cli.github.com/) (all API calls go through it; there is no other auth).

```sh
uv tool install --editable .
```

That puts a `milestones` command on your PATH running the code in this working tree, so edits to the source take effect immediately; re-run it only if the dependencies or the entry point change. To run it out of the checkout without installing, prefix every command below with `uv run`.

Create `~/.config/milestones.toml` by hand (only `add` and `remove` ever write to it,
and they rewrite just the `repos` list):

```toml
buckets = ["Needed soon", "Needed later", "Not urgent", "Upstream"]

repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]

# Optional: repos you own but don't triage, so `discover` stops offering them.
# `OWNER/*` covers every repo of an owner you haven't tracked.
ignore = [
  "NCATSTranslator/Planning-Committee",
  "TranslatorSRI/*",
]

# Optional: the repos you're working on right now. `status` stars their rows;
# `milestones focus` and `unfocus` rewrite this list.
focus = ["gaurav/milestones"]

# Optional: fixed colours for an owner's rows in `status`. Any owner left out that
# owns several tracked repos gets one from the palette. Sibling organisations can
# share a colour to read as one.
[colors]
NCATSTranslator = "blue"
TranslatorSRI = "blue"
phyloref = "yellow"
```

Colour names are `blue`, `cyan`, `teal`, `indigo`, `violet`, `magenta`, `purple`, `yellow`,
`green`, `rose`, `pink` and `grey`; a 256-colour number from 0 to 255 works too.

## Commands

```sh
milestones status [--json]               # the default command (bare `milestones` runs it):
                                         # all open milestones across configured repos by
                                         # due date, with open/closed counts and % done;
                                         # flags !OVERDUE, (empty) and (done), links each,
                                         # then names any tracked repo with nothing open.
                                         # Colour-coded on a terminal (see below); --json
                                         # prints the same thing for a script to read
milestones triage [--repo OWNER/NAME] [--prs]
                                         # walk untriaged issues (no milestone) one at a
                                         # time and assign each to a milestone/bucket;
                                         # --prs walks the pull requests instead
milestones rollover OWNER/NAME FROM TO [--close]
                                         # move all open issues from milestone FROM to TO
                                         # (by title); --close closes FROM once empty
milestones setup OWNER/NAME              # create the standing buckets in a repo (idempotent)
milestones discover [--tracked-only|--list-ignored|--ignore-remaining]
                                         # the repos you track, then repos owned by your
                                         # configured owners that have issues/milestones
                                         # but aren't in the config yet; ignored repos are
                                         # counted in one closing line rather than listed;
                                         # --tracked-only stops after the first list,
                                         # --list-ignored spells out the ignored ones, and
                                         # --ignore-remaining ignores the ones suggested
milestones prs [--assigned] [--review-requested] [--mentions] [--json]
                                         # every open PR you authored, anywhere on GitHub,
                                         # in three tables: PRs in tracked repos with no
                                         # milestone (then run `triage --prs`), PRs in
                                         # repos not in the config (`add` the repo, or
                                         # put the PR on your TODO list), and PRs in
                                         # ignored repos. Drafts and last-updated dates
                                         # are marked, nothing is left out; the flags add
                                         # PRs assigned to you, awaiting your review, or
                                         # mentioning you
milestones check [-i|--json]             # everything that needs fixing, grouped by fix:
                                         # milestones to rename, date, close, delete or
                                         # roll over, and repos that use standing buckets
                                         # but have one missing or closed.
                                         # -i then walks the list and applies your answers;
                                         # --json prints them for a script instead
milestones add REPO                      # track a repo (OWNER/NAME or a github.com URL);
                                         # rewrites the repos list in the config
milestones remove REPO                   # stop tracking a repo, same syntax
milestones focus [REPO ...]              # mark the repos you're working on right now, so
                                         # `status` stars their rows; no arguments lists them
milestones unfocus REPO [...]            # stop marking them, once your attention moves on;
                                         # focused repos also come first in `triage`
milestones ignore REPO|OWNER [...]       # hide repos from discover without tracking them;
                                         # a bare OWNER hides everything of that owner's
                                         # you don't track; appends to the config
```

`check` reports only; every finding carries the milestone's URL, so a fix is one click away.
`--interactive` walks the same findings one at a time, offering the fixes that fit each one — rename
to a standing bucket the repo hasn't got yet, set the due date to today / tomorrow / next Monday /
the start of next month, close, delete, roll over, or run `setup` — plus open, skip and quit. Once
you type a date of your own, the fourth date slot offers that date back for the rest of the session
(the one you have typed most often, most recent winning ties), since a run of milestones usually
wants the same day — "after the project meeting" is one keypress each after the first. A single
keypress acts immediately; the fixes that need more (a new title, a typed date, a milestone to roll
onto) then ask, and take blank as "skip". A milestone is well-formed if it is one of the repo's
standing buckets, or names a version or date (`v1.2`, `Babel v1.19`, `2026aug24`, `Week ending
2026-08-31`) *and* carries a due date — however far out, since an undated milestone never comes due
to roll over.

Owning a repo is not the same as triaging it, and `discover` searches by owner, so most of what it
turns up is someone else's to manage. `ignore` is the third state beside tracked and untracked:
an ignored repo is dropped from the suggestions and counted in a closing line naming the config, so
the list stays short without hiding that anything was left out. Take the names straight from
`discover`'s output — `ignore` accepts several at once. Un-ignoring is a hand-edit of that list,
or `add`, which tracks the repo and so outranks it.

`milestones ignore OWNER` covers a whole owner, which is the other way round: track the few repos
you do triage, then ignore the rest of the organisation in one entry, and any repo that appears
there later is ignored too. It is stored as `OWNER/*`, but type the bare owner — an unquoted
`OWNER/*` is a glob your shell tries to expand. Tracked beats ignored, so the repos you have added
keep showing up, and per-repo entries under an ignored owner are simply redundant, not wrong.

`milestones discover --list-ignored` spells the hidden repos out in a table of their own instead of
only counting them, busiest first, so reconsidering one is `milestones add` on the name in the first
column — after which it moves up into the tracked list and stops being counted.

The two kinds of entry answer different questions, and `discover --ignore-remaining` writes the
second kind in bulk: it adds every repo it has just suggested to the ignore list, after confirming.
Use `OWNER/*` for an organisation you will never triage, and the sweep for owners whose repos you do
care about — you have looked at these and said no, so clear them off the list, but any repo created
there later is a fresh suggestion rather than something the glob silently swallows. After a sweep
`discover` reads as a change feed: "Nothing new" until something appears.

`rollover` asks for confirmation before touching anything, and is the point of the tool: at
release time, roll what didn't make it into the next milestone instead of re-triaging by hand.

### Reading the status table

The table is colour-coded so a long one can be skimmed rather than read. An owner is coloured
when the config names a colour for it, or when several of your repos share it, so a run of rows
from the same organisation lights up together. Version numbers in a milestone title are bold. A
due date runs red (overdue), orange (this week), yellow (this month) or grey (further out).

A repo you have said you are working on is starred with a gold `✦`, in a column of its own.
The glyph carries the meaning and the colour only makes it easier to find, so a pipe loses
nothing. The rows do **not** move: a repo's milestones include its standing buckets, and
floating `Not urgent` above someone else's overdue release would make the table worse. The star
says "this is mine right now" while the order goes on meaning "this is what's due next".

`%` is grey up to halfway and then a lightening green, so the milestones near the end stand
out. Nothing below halfway is coloured as a warning: a milestone at 10% is not one in trouble,
just one somebody has only started. An undated milestone stays plain, for the same reason.

Colour is switched off when the output is not a terminal, so `milestones status | grep …` and
`milestones status > notes.txt` behave, and `NO_COLOR=1` turns it off in a terminal too. For a
script — or a coding agent — `milestones status --json` prints the same data as JSON:
`milestones` with `repo`, `title`, `due`, `open`, `closed`, `percent`, `flags`, `focus` and
`url`; `quiet_repos` in the same shape for the tracked repos with no open milestone; and the
configured `focus` list itself. `milestones check --json` does the same for the findings, with
a `kinds` legend saying what each one's fix is.

## Development

```sh
uv run pytest
```
