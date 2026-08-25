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

Create `~/.config/milestones.toml` by hand (the tool only ever reads it):

```toml
buckets = ["Soon", "Later", "Not urgent"]

repos = [
  "gaurav/milestones",
  "NCATSTranslator/Babel",
]
```

## Commands

```sh
uv run milestones status                        # all open milestones across configured repos,
                                                # sorted by due date; flags !OVERDUE and (empty)
uv run milestones triage [--repo OWNER/NAME]    # walk untriaged issues (no milestone) one at a
                                                # time and assign each to a milestone/bucket
uv run milestones rollover OWNER/NAME FROM TO [--close]
                                                # move all open issues from milestone FROM to TO
                                                # (by title); --close closes FROM once empty
uv run milestones setup OWNER/NAME              # create the standing buckets in a repo (idempotent)
uv run milestones discover                      # repos owned by your configured owners that have
                                                # issues/milestones but aren't in the config yet
```

`rollover` asks for confirmation before touching anything, and is the point of the tool: at
release time, roll what didn't make it into the next milestone instead of re-triaging by hand.

## Development

```sh
uv run pytest
```
