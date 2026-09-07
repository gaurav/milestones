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
```

## Commands

```sh
milestones status                        # the default command (bare `milestones` runs it):
                                         # all open milestones across configured repos by
                                         # due date, with open and closed issue counts;
                                         # flags !OVERDUE, (empty) and (done), links each,
                                         # then names any tracked repo with nothing open
milestones triage [--repo OWNER/NAME]    # walk untriaged issues (no milestone) one at a
                                         # time and assign each to a milestone/bucket
milestones rollover OWNER/NAME FROM TO [--close]
                                         # move all open issues from milestone FROM to TO
                                         # (by title); --close closes FROM once empty
milestones setup OWNER/NAME              # create the standing buckets in a repo (idempotent)
milestones discover [--tracked-only]     # the repos you track, then repos owned by your
                                         # configured owners that have issues/milestones
                                         # but aren't in the config yet;
                                         # --tracked-only stops after the first list
milestones check [-i|--interactive]      # everything that needs fixing, grouped by fix:
                                         # milestones to rename, date, close, delete or
                                         # roll over, and repos that use standing buckets
                                         # but have one missing or closed.
                                         # -i then walks the list and applies your answers
milestones add REPO                      # track a repo (OWNER/NAME or a github.com URL);
                                         # rewrites the repos list in the config
milestones remove REPO                   # stop tracking a repo, same syntax
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

`rollover` asks for confirmation before touching anything, and is the point of the tool: at
release time, roll what didn't make it into the next milestone instead of re-triaging by hand.

## Development

```sh
uv run pytest
```
