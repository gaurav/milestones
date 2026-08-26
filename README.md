# milestones

I have measured out my life with GitHub milestones

A personal CLI for viewing and managing GitHub milestones across all my repositories.
Milestones are the single source of truth: dated milestones for "this release"/"next release",
plus standing undated buckets ("Soon", "Later", "Not urgent") that never close. Every issue's
bucket is publicly visible on its GitHub issue page, so anyone can see how it's triaged and
complain in a comment if they disagree.

## Setup

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and an authenticated
[`gh`](https://cli.github.com/) (all API calls go through it; there is no other auth).

```sh
uv sync
```

Create `~/.config/milestones.toml` by hand (only `add` and `remove` ever write to it,
and they rewrite just the `repos` list):

```toml
buckets = ["Needed soon", "Needed later", "Not urgent"]

repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]
```

## Commands

```sh
uv run milestones status                        # the default command (bare `milestones` runs it):
                                                # all open milestones across configured repos by
                                                # due date; flags !OVERDUE and (empty), links each
uv run milestones triage [--repo OWNER/NAME]    # walk untriaged issues (no milestone) one at a
                                                # time and assign each to a milestone/bucket
uv run milestones rollover OWNER/NAME FROM TO [--close]
                                                # move all open issues from milestone FROM to TO
                                                # (by title); --close closes FROM once empty
uv run milestones setup OWNER/NAME              # create the standing buckets in a repo (idempotent)
uv run milestones discover                      # repos owned by your configured owners that have
                                                # issues/milestones but aren't in the config yet
uv run milestones check                         # everything that needs fixing: milestones to
                                                # rename, date, close, or roll over, and repos
                                                # missing their standing buckets
uv run milestones add REPO                      # track a repo (OWNER/NAME or a github.com URL);
                                                # rewrites the repos list in the config
uv run milestones remove REPO                   # stop tracking a repo, same syntax
```

`check` reports only; it changes nothing. A milestone is well-formed if it is one of the standing
buckets, or names a version or date (`v1.2`, `Babel v1.19`, `2026aug24`, `Week ending 2026-08-31`)
*and* carries a due date — however far out, since an undated milestone never comes due to roll over.

`rollover` asks for confirmation before touching anything, and is the point of the tool: at
release time, roll what didn't make it into the next milestone instead of re-triaging by hand.

## Development

```sh
uv run pytest
```

To get a `milestones` command on your PATH that always runs the code in this working tree:

```sh
uv tool install --editable .
```

Re-run it only if the dependencies or the entry point change; edits to the source take effect
immediately.
