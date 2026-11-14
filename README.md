# milestones

I have measured out my life with GitHub milestones

A personal CLI for viewing and managing GitHub milestones across all my repositories.
Milestones are the single source of truth: dated milestones for "this release"/"next release",
plus standing undated buckets such as "Needed soon" that never close.
[MILESTONES.md](MILESTONES.md) lists the buckets and says what each one, and each kind of dated
milestone, means. Every issue's bucket is publicly visible on its GitHub issue page, so anyone can
see how it's triaged and complain in a comment if they disagree.

The buckets are per repo and opt-in: `setup` creates them where you want that much triage, and
a repo without them is simply left alone. Every command picks up whichever buckets a repo
actually has, and a repo is free to use only some of them.

## Setup

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and an authenticated
[`gh`](https://cli.github.com/) (all API calls go through it; a token of your own choosing is
optional, see [Tokens and read-only runs](#tokens-and-read-only-runs)).

```sh
uv tool install --editable .
```

That puts a `milestones` command on your PATH running the code in this working tree, so edits to the source take effect immediately; re-run it only if the dependencies or the entry point change. To run it out of the checkout without installing, prefix every command below with `uv run`.

Create `~/.config/milestones.toml` by hand. The commands that change it (`add`, `remove`,
`focus`, `unfocus`, `ignore` and `discover --ignore-remaining`) each rewrite the one list they
own and leave the rest of the file alone:

```toml
# Optional: this is the default, so most configs leave it out. A list written here
# doesn't grow when the default does, so a bucket added later ("Needs triage" was
# one) has to be added here by hand too, or the tool treats it as any other milestone.
buckets = ["Critical", "Needed soon", "Needed later", "Not urgent", "Upstream", "Needs triage"]

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

# Optional: a repo's own labels that say a priority or an effort, each with the standard
# label it stands for (see LABELS.md). They are read, never written or removed.
[labels."NCATSTranslator/Babel"]
"P0" = "Priority: Urgent"
"good first issue" = "Effort: S"
```

Colour names are `blue`, `cyan`, `teal`, `indigo`, `violet`, `magenta`, `purple`, `yellow`,
`green`, `rose`, `pink` and `grey`; a 256-colour number from 0 to 255 works too.

## First run

Every command is described below; this is the order they are useful in.

1. **Find your repos.** Put one or two in `repos` by hand, then `milestones discover` lists what
   else you own that has issues or milestones. `milestones add OWNER/NAME` tracks one, and
   `milestones ignore OWNER/NAME` (or a bare `OWNER`) hides what is not yours to triage, so the
   next `discover` only shows what is new.
2. **Give each repo its buckets.** `milestones setup OWNER/NAME` creates the standing buckets in
   a repo, and is safe to run again. Skip it for a repo that doesn't need that much triage: the
   rest of the tool treats a repo without buckets, or with only some, as fine.
3. **Triage.** `milestones triage` walks every open issue with no milestone, then everything
   parked on `Needs triage`, one keypress each; `milestones triage --prs` does the pull requests.
   For a backlog, `triage --list` piped through `grep` or `fzf` into `milestones assign` does
   hundreds at once ([below](#triaging-in-bulk)).
4. **Look.** `milestones status` (bare `milestones` too) is the table to keep open: every open
   milestone across your repos by due date, and at the bottom what is still untriaged.
   `milestones focus OWNER/NAME` stars the repos you are working on this week.
5. **Tidy.** `milestones check` says what is wrong with the milestones themselves — undated,
   misnamed, empty, done, or closed with work still on them — and `check -i` fixes each with a
   keypress.
6. **Release.** When a release goes out, `milestones rollover OWNER/NAME "v1.2" "v1.3" --close`
   moves what didn't make it onto the next milestone and closes the old one; `triage --from
   "v1.2"` is the same job one issue at a time, for when they shouldn't all go the same way.

[MILESTONES.md](MILESTONES.md) is the other half: what the buckets mean and how a release is
put together. [LABELS.md](LABELS.md) goes with it: the `Priority:` and `Effort:` labels that
order the work on a milestone. Both are written for the people and coding agents working in the
repos themselves, so they say next to nothing about this tool.

## Tokens and read-only runs

Out of the box every call is made as whoever `gh auth login` signed in, which usually means a
token that can push code as well as move issues. An env file narrows that. Copy
[`env.default`](env.default) to `.env` in the directory you run `milestones` from, or to
`~/.config/milestones.env`; a variable set in your shell wins over both, and only `MILESTONES_*`
lines are read from a file.

| Setting | What it does |
| --- | --- |
| `MILESTONES_GH_TOKEN` | The token `gh` is handed for every call. Unset: `gh`'s own login. |
| `MILESTONES_GH_TOKEN_<OWNER>` | A token for calls about one owner's repos (`NCATSTranslator` → `_NCATSTRANSLATOR`, `my-org` → `_MY_ORG`). |
| `MILESTONES_PERMISSIONS` | `write` (the default) or `read`. |

With `MILESTONES_PERMISSIONS=read` nothing is written, whatever the token could do: `setup` says
what it would create, `rollover` and `assign` list what they would move and stop, the `triage` walk
says where each issue would go, and `check -i` reports each fix it would have made. It is the way
to see what the tool suggests for your repos before letting it act.

A fine-grained personal access token keeps the tool to what it should touch — Issues and Pull
requests read and write, Metadata read, and organisation Projects read, with nothing for
Contents. Such a token belongs to **one owner**, so repos under a second owner need a second
token: that is what the per-owner setting is for. A search across every tracked repo goes out on
the default token, which only has to be able to *read* them.

`milestones doctor` shows which env files were found, the mode, which token each owner's calls
use (the setting's name, never the token), who that token signs in as, and whether it can read
projects and organisation issue fields.

## The board

```sh
npm install && npm run build      # once, and after pulling changes to ui/
milestones board NCATSTranslator/Babel --from Critical --from "Babel v1.19" --from none
```

`milestones board` opens one repo in your browser as a column per milestone, with every open
issue and pull request as a card. Run it with no repo from inside a checkout and it takes that
checkout's `origin`. `--from` chooses the columns shown to begin with (`none` is the issues with no
milestone); the rest are under **Columns**, including any closed milestone that still has open
work on it.

A card shows what has already been decided about it, wherever that was written down. Its title
comes first, alone; under it, the number, age, comments, sub-issues, a pull request's lines added
and removed, and who opened it (`@login`); then whether it is an issue or a pull request (a draft,
closed, its issue type), and beside that its priority, effort and milestone. Priority and effort
are its `Priority:` and `Effort:` labels ([LABELS.md](LABELS.md)); one read from anywhere else — an
older label such as `Size: M`, a label of the repo's own, the organisation's issue field — is in
italics. A pull request with no effort recorded shows one estimated from its lines changed instead,
`~M`, in the size bands prcoder uses; it is only shown, never written. Those three are buttons, the same as the pane's below, and a
card leaves off whichever the columns or the grouping already show. Then, small, who is assigned
(each with a link to their GitHub profile), the issues a pull request closes, and the pull requests
that close an issue or only mention it; and its other labels on a line of their own; the filter row
can narrow the board to one author. The pane on the right has the description and the rest. At its
top are the card's milestone, priority and effort as buttons — click one to change it, as `m`, `p`
and `e` do — and under them whatever has been suggested for that card. Collapse the pane (`i`) and
the same buttons run down the strip that is left, with a count of the suggestions waiting.

There are three ways to move a card, and they are the same write:

- **Keys**: `j`/`k` and `h`/`l` move the focus; `1`–`9` send the focused card to that column and
  `0` takes it off its milestone; `space` selects, and then a key moves the whole selection; `z`
  undoes; `o` opens the card on GitHub; `r` (or ↻ in the pane) reads it from GitHub again,
  without the rest of the repo, and takes it off the board if it has closed or merged; `/` searches.
- **Drag** a card, or a selection, onto a column.
- **`m`** opens a picker over every milestone in the repo, open or closed, for when there are more
  than nine or you want a new one. It asks before reopening a closed milestone or creating one.

Moving a pull request also moves the open issues it closes that nobody has placed yet, as the
`triage` walk does. A column's due date is a button: click it to change the date.

The same keys reach the rest of a card: `p` sets its priority and `e` its effort, `L` toggles
labels, `t` retitles it, `c` comments, and `x` closes or reopens it. Priority and effort are
written as labels, `Priority: High` and `Effort: M`, on issues and pull requests alike: a pull
request can't hold an organisation issue field, and the two need to sort together. A label the repo
hasn't got is made in its standard colour. All of them work on a selection, and `z` undoes all but
a comment. A number picks an option by its place in the dialog — `1` to `4` are Urgent, High,
Medium and Low, and for effort `1` to `5` are XL to XS; `0` clears either.

**Suggestions** (`s`, or the button) is the list of changes waiting for a yes or no. The board
puts its own there: where an older label (`Priority: Critical`, `Size: M`), a label of the repo's
own or the organisation's field records a priority or an effort, it suggests setting the standard
label, and then removing the older label and clearing the field that it has made redundant — read
from anywhere, written one way. Where they disagree it keeps the standard label and says so. Accept or reject them one at a time on the card, or a whole rule at once from the list;
accepting is an ordinary change, written and undone like any other, and a rejection is remembered.

Every move is written to GitHub as you make it, in order, while you carry on. A card whose write
is in flight says *saving…*; one whose write failed keeps the place you gave it and gains **Retry**
and **Revert**. Closing the tab with writes outstanding asks first, and stopping the server with
Ctrl-C lists anything that never reached GitHub.

### Rehearsing read-only

With `MILESTONES_PERMISSIONS=read` nothing is sent to GitHub, and the board says so in a banner
across the top and again the first time you change something. What you do is not lost: cards are
marked *planned*, and the net change to each is saved as you go to a **plan file**
(`~/.local/state/milestones/board-<owner>-<repo>.plan.json`), whose path the banner shows. It is
plain JSON with a line of English per change, so you — or a coding agent — can read what was
decided:

```json
{"issue": 62, "kind": "milestone", "value": 30, "previous": null,
 "title": "Add X as an identifier source", "summary": "#62: milestone → Needed soon (was none)"}
```

A change you undo leaves the plan, and a plan with nothing in it is removed. Start the board
read-only again and it picks the plan up where you left it. Start it writable and it tells you a
plan is waiting: **Review as suggestions** puts each planned change in the suggestions list, to
accept or reject like any other, and **Discard** deletes the plan. Nothing is applied without that
review. A change whose card has moved on since — someone set the priority by hand in the
meantime — says so beside the suggestion, and one that has already been made, or whose issue has
closed, is left out and counted.

### Sorting by priority and effort first

The columns need not be milestones. **Milestone | Priority | Effort** in the toolbar (or `v`)
changes what they are, and everything else stays the same: a number key, a drag, or `0` to clear
puts a card in a column, and that is written as the milestone, the priority or the effort. So a
backlog can be triaged in passes:

1. Tick the milestones you are working through under **Columns**. In the other two views that
   selection is the scope: only those milestones' cards are sorted.
2. Switch to **Priority**. The first column, *No priority*, is the inbox; `1`–`4` send the focused
   card to Urgent, High, Medium or Low and move on to the next, so a pass is a run of keys down
   that column. Its count is what is left.
3. Switch to **Effort** and do the same: `1`–`5` are XL to XS.
4. Back in **Milestone**, choose *Group: priority, effort*. Each column is now cut into
   groups — "Urgent · L", "High · M" — judged cards first, the largest effort first within a
   priority, so a milestone reads from what matters most and costs most down. Click a group's
   heading to select it, and one key sends the whole group to a milestone.

A card stands in the column of what was recorded about it, wherever that was: its standard label,
or else an older record read in the same terms (`Priority: Critical` under Urgent, the Effort
field's High under L), marked *from old label* or *from field*. Putting it in a column sets the
standard label. Pull requests are sorted with the issues.

The board opens the way you left it — the view, the grouping and the milestones ticked — unless
you say otherwise: `milestones board --by priority --group effort`, or `--from`, wins. `p`, `e` and `m` still set
priority, effort and milestone whatever the columns are.

The address bar keeps up with all three as you change them, and with the search and the filters,
so reloading the page shows the same cards in the same columns rather than what the command line
asked for. The filters last only as long as the tab: the next `milestones board` starts with none.

### Asking a coding agent

The board does not run an agent; it gives one a way in. Start your coding agent wherever it works
best — in the repo's own checkout, where it can read the code — and give it the briefing once:

```sh
milestones board --brief        # what the buckets mean, the rules, and the command below
```

Then, on a card (or a selection), press `a`: say what you think — "medium priority, medium effort,
because we can add this as its own identifier source that won't conflict with existing ones" — and
the board copies a prompt carrying the issue, where it stands now, and your view. Paste it into the
agent's session. The agent reads the issue and the code, argues if it disagrees, and when you have
settled it, proposes the changes:

```sh
milestones suggest 62 --priority medium --effort m --milestone "Needed soon" \
    --add-label "new data source" \
    --reason "Own identifier source; overlaps MeSH on about 40 IDs, see src/createcompendia" \
    --source opus
```

Each change arrives on the board as its own suggestion, with the reason and the source beside it,
for you to accept or reject; nothing is written to GitHub until you accept. `suggest` takes
`OWNER/NAME#N`, a URL, or a bare number inside the repo's checkout, and also `--remove-label`,
`--title`, `--comment` or `--comment-file`, and `--close`. `--source` (or `MILESTONES_SOURCE` in
the agent's environment) says who is suggesting, so two sessions on different models can look at
the same issue and you can compare what each proposed. `milestones suggest --json` reads a batch
from stdin. A board has to be running for the repo; `suggest` says so if one isn't. There is one
board per repo at a time: starting a second says where the first is.

The page and the server share nothing but an HTTP API, described in
[docs/BoardAPI.md](docs/BoardAPI.md), so a different front end — or a script — can use the same
server.

## Commands

```sh
milestones board [OWNER/NAME] [--from M]  # the triage board in your browser; see above
milestones suggest REF --priority high --reason "..."   # propose a change on a running
                                         # board, for the person there to accept or reject

milestones doctor                        # which token and permissions are in effect, and
                                         # what that token can read

milestones status [--json]               # the default command (bare `milestones` runs it):
                                         # all open milestones across configured repos by
                                         # due date, with open/closed issue counts, open
                                         # PRs and % done; flags !OVERDUE, (empty) and
                                         # (done), links each, then names any tracked repo
                                         # with nothing open, says how many closed
                                         # milestones still hold open work, and counts the
                                         # issues and PRs in each repo with no milestone,
                                         # and the ones parked on Needs triage.
                                         # Colour-coded on a terminal (see below); --json
                                         # prints the same thing for a script to read
milestones triage [--repo OWNER/NAME] [--prs] [--from MILESTONE ...] [--list|--json]
                                         # walk untriaged issues (no milestone) one at a
                                         # time, then the ones parked on Needs triage, and
                                         # assign each to a milestone/bucket; --prs walks
                                         # the pull requests instead; --from walks the open
                                         # items on that milestone instead (repeatable, in
                                         # that order; `none` is the no-milestone set);
                                         # --list prints them instead, one per line
                                         # (OWNER/NAME#N  title  [labels]), focused repos
                                         # first then freshest; --json likewise. A walk
                                         # opens with how many it found where, and ends —
                                         # on q, ^C or an error too — on what it assigned,
                                         # skipped, created and left to do
milestones assign MILESTONE [REF ...] [--priority urgent|high|medium|low]
                                         # put issues on the milestone of that title in
                                         # each of their repos; REF is OWNER/NAME#N or an
                                         # issue URL, or read from stdin one per line — a
                                         # `triage --list` line works as-is. Confirms on a
                                         # terminal; a repo without that milestone is
                                         # skipped, not fatal. A PR takes the issues it
                                         # closes along, as in the walk. --priority also sets
                                         # each one's `Priority:` label
milestones rollover OWNER/NAME FROM TO [--close]
                                         # move all open issues from milestone FROM to TO
                                         # (by title); --close closes FROM once empty
milestones setup OWNER/NAME              # create the standing buckets and the priority and
                                         # effort labels in a repo (idempotent); asks before
                                         # renaming or recolouring a label that is there
                                         # except Critical, which triage offers anyway and
                                         # creates the first time you pick it
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
                                         # closed milestones with work still on them, and
                                         # milestones to rename, date, close, delete or
                                         # roll over.
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
the start of next month, close, delete, roll over, or reopen — plus open, skip and quit. Once
you type a date of your own, the fourth date slot offers that date back for the rest of the session
(the one you have typed most often, most recent winning ties), since a run of milestones usually
wants the same day — "after the project meeting" is one keypress each after the first. A single
keypress acts immediately; the fixes that need more (a new title, a typed date, a milestone to roll
onto) then ask, and take blank as "skip". A milestone is well-formed if it is one of the repo's
standing buckets, or names a version or date (`v1.2`, `Babel v1.19`, `2026aug24`, `Week ending
2026-08-31`) *and* carries a due date — however far out, since an undated milestone never comes due
to roll over. A closed milestone with open issues or pull requests still on it is *stranded*: nothing
else can see that work, since `status` shows open milestones and `triage` only what has no
milestone or sits on Needs triage, so `check` leads with those and offers to roll the work onto a milestone that is
open, or to reopen the closed one. Pull requests count as work on a milestone throughout, so a
milestone whose issues are all closed but whose PRs are still open is not `(done)`.

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

### Triaging in bulk

The `triage` walk is one issue per keypress, which is fine for a week's arrivals and hopeless
for a backlog of hundreds. For that, `triage --list` and `assign` are the two halves of a
pipeline, and whatever sits between them does the choosing — `grep` on a label or a word in the
title, `jq` over `triage --json`, `fzf -m` for picking by hand, or a coding agent reading the
JSON and deciding:

```sh
milestones triage --list | grep 'new data source' | milestones assign "Needed later"
milestones triage --list | fzf -m --no-sort | milestones assign "Needed soon"
milestones triage --list --repo NCATSTranslator/Babel | grep -i duckdb | milestones assign "v1.20"
```

`assign` resolves the title in each issue's repo, so one command puts issues from several repos
onto their own "Needed later"; a repo that hasn't got the milestone is named and skipped. Parking
is the same pipeline with `Needs triage` as the title (`grep -i 'cannot reproduce'`, say), and
`triage --from "Needs triage" --list` is the parked set, ready to be piped back in once it is
clearer where each belongs. Given
refs on the command line at a terminal it asks first; fed from a pipe it doesn't, since the pipe
is the answer. `--priority urgent|high|medium|low` does for a pipeline what the walk's `!` `+` `-`
do for one item: the `Priority:` label is set on each issue or pull request after its milestone. The listing GitHub returns lags
writes by a few seconds, so a `--list` straight after an `assign` can still show what was just
moved — re-running is harmless.

### Parking, and re-triaging a milestone

Two things in the walk look alike and aren't. `s` skips: nothing is written, and the issue comes
round again next run. Picking `Needs triage` parks it: it has been looked at and can't be placed
until it has been reproduced, tested or investigated, and that is now visible on the issue's page
for everyone, as [MILESTONES.md](MILESTONES.md) describes. A walk covers both states, in that
order: every issue with no milestone, then everything parked, so a parked issue is offered again
each run but only after the new arrivals, and a `q` before then leaves it parked. Its header
says where it is (`on Needs triage, updated …`), that milestone is marked `(current)` in the
menu, and picking it again writes nothing.

`--from MILESTONE` walks the open items on any milestone instead, and can be given more than
once for several in that order. `--from none` is the no-milestone set on its own, the walk as it
was before Needs triage existed; `--from "Needed later"` re-triages a bucket that has grown;
`--from "Babel v1.19"` at release time is the one-at-a-time alternative to `rollover` for what
didn't make it. `--list` and `--json` take `--from` too, and a listed item that has a milestone
says so after its title (`(on Needs triage)`), while a no-milestone line reads exactly as before,
so a pipeline into `assign` is not disturbed. GitHub matches the title whatever the milestone's
state, so `--from` on a title that is closed in some repo walks that repo's stranded items too.

### Triaging pull requests

A pull request is work already under way, so every open one belongs on a milestone, and
`triage --prs` walks the ones that haven't got one (`--list` and `--json` take `--prs` too).
Each is marked `(PR)` or `(PR, draft)`, and shows the issues it closes and where they are:
`closes #12 (no milestone), #7 (v1.2)`. Putting the PR on a milestone puts each open issue it
closes that has no milestone, or is parked on Needs triage, on the same one, since the PR stands
in for them and a PR is as good a way of placing an issue as any; an issue already placed anywhere
else is left where it is, and so is one in another repo. `assign` does the same for a PR ref. For a PR that needs a closer look, `o` opens it in the browser and `s`
leaves it untriaged, so it comes round again next run. When none of the numbered milestones is the
one, `c` asks for a title, with Tab completing over every milestone the repo has, open or closed: an
open one is used as it is, a closed one is reopened and a new one created, each after asking. A
milestone picked that way joins the numbered list for the rest of the run. `milestones prs` is the wider view: every
open PR of yours anywhere on GitHub, grouped by whether its repo is tracked.

### Priority and order

An answer in either walk takes one modifier after the number (or after `c`), regex-style. `2!`
assigns milestone 2 and then sets the item's priority to Urgent, `2+` High, `2-` Low. Priority
is a label ([LABELS.md](LABELS.md)), `Priority: Urgent` and so on, which replaces any other
`Priority:` label on the item and is made in its standard colour if the repo hasn't got it; a pull
request takes it as an issue does. `assign --priority urgent|high|medium|low` is the same thing for
a pipeline. `2^` and `2$`, for the top and bottom of the milestone's order, are parsed
and refused: GitHub has no API that writes that order
([#25](https://github.com/gaurav/milestones/issues/25)), so the milestone is set and the message
links to it for dragging by hand.

### Reading the status table

The table is colour-coded so a long one can be skimmed rather than read. An owner is coloured
when the config names a colour for it, or when several of your repos share it, so a run of rows
from the same organisation lights up together. Version numbers in a milestone title are bold. A
due date runs red (overdue), orange (this week), yellow (this month) or grey (further out).
A bucket with work on it is coloured too: the urgency levels run down the same ramp, `Critical`
bold red, `Needed soon` orange, `Needed later` light green and `Not urgent` grey, and the two that
are states rather than levels sit off it, `Upstream` teal and `Needs triage` magenta. An empty
bucket stays plain, so a quiet `Needed soon` reads as quiet. `Critical` is optional — `setup` leaves it to `triage` to create, and
`status` leaves it out while nothing is open on it, so it only shows up when something is on fire.

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
`milestones` with `repo`, `title`, `due`, `open`, `closed`, `open_prs`, `closed_prs`,
`percent`, `flags`, `focus` and `url` (`open` and `closed` are issues, as GitHub counts them, and
`percent` is over issues and PRs together); `quiet_repos` in the same shape for the tracked repos
with no open milestone; `stranded`, the closed milestones with open work still on them; `untriaged`,
the per-repo counts of issues and PRs with no milestone, with `needs_triage` for what is parked on
that bucket; and the configured `focus` list itself. `milestones check --json` does the same for the findings, with
a `kinds` legend saying what each one's fix is.

## Why not something else

Nothing off the shelf manages milestones across repositories. `gh` itself declined milestone
support ([cli/cli#1200](https://github.com/cli/cli/issues/1200)), the `gh-milestone` extensions
are single-repo CRUD with no rollover, and ZenHub, Linear, Plane and Huly replace milestones with
planning objects of their own. A GitHub Project could mirror them, but its auto-add can't filter on
milestone, and it would be a second record that drifts from the first.

What it deliberately leaves out:

- **No local state or cache of GitHub.** Every command reads GitHub fresh, so nothing can go
  stale except GitHub's own listings, which lag writes by a few seconds. Every decision lives on
  GitHub; the one thing kept locally is what the board has *not* decided yet — suggestions still
  waiting, the ones you turned down, so that they aren't made again, the changes of a read-only
  session until they are reviewed — and how you last had the board laid out.
- **Nothing beyond GitHub milestones.** No other tracker, and no planning objects of its own.
- **No `repos` command.** "Which repos am I tracking, and which am I missing?" is one question, and
  `discover` answers both halves of it, so a `repos` subcommand beside `add` and `remove` would
  only split it in two.

## Development

```sh
uv run pytest                     # the Python: pure functions only, nothing calls gh
npm run typecheck && npm test     # the board's front end, in ui/
npm run dev                       # rebuild the front end on every change
uv run milestones board OWNER/NAME --reload   # and restart the board, same URL, on a Python change
PLAYWRIGHT_FROM=~/code/prcoder npm run drive   # drive a read-only board in a headless browser
```

The front end is TypeScript and Preact with signals, bundled by esbuild into
`milestones/board/static/` (not checked in) — the same stack as
[chaff](https://github.com/gaurav/triage), so that components can move between the two. Node is
needed to build it and for nothing else; the Python package has no runtime dependencies.
