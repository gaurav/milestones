# milestones — agent notes

All GitHub access goes through `milestones/gh.py`, which shells out to the authenticated `gh`
CLI. Hard-won API facts (verified live, Aug 2026):

- `gh search issues` silently returns **zero results** — it uses the retired legacy search
  path. Use REST `search/issues?q=...&advanced_search=true` instead (see `gh.search_issues`).
- Advanced search **ANDs** repeated qualifiers (legacy OR'd them); OR them explicitly:
  `(repo:a/b OR repo:c/d)`. Query strings cap at 256 chars, so `build_search_queries`
  splits the configured repos across as many queries as that takes.
- `@me` works in REST advanced search, inside an OR group too (`(author:@me OR
  assignee:@me)`), and a search item carries `draft` and `milestone`, so `prs` lists pull
  requests from the one search with no second call. Unlike `triage`, it is not scoped to the
  config — that is its point.
- Milestone writes are REST-only; no GraphQL mutations exist. Reads are fine in GraphQL.
- GraphQL `repositories` defaults `ownerAffiliations` to include collaborator repos — pass
  `ownerAffiliations: OWNER`. Transferred repos can still echo under their old owner; dedupe.
- `repository(owner:, name:)` follows renames and normalises case, so the `nameWithOwner` coming
  back need not match the config's spelling. Key anything per-repo off the returned name, never
  off the configured string — `fetch_milestones` returns the names for exactly that reason.
- A milestone's `open_issues` counts **pull requests** too, so it never agrees with an
  issues-only listing; don't use it as an "is this empty" check. The `triage` menu shows it
  anyway, on purpose — a PR on a milestone is work sitting on that milestone.
- GraphQL is the other way round: `Milestone.issues` **excludes** pull requests, which are their
  own `pullRequests` connection, so `fetch_milestones` asks for both and every decision over
  "is there work on this" adds them. And `milestones(states:)` defaults to open ones; CLOSED
  has to be asked for by name.
- `gh api graphql` exits **nonzero on any GraphQL error**, including a partial one, but still
  prints the whole response — resolved data and all — on stdout. An aliased multi-repo query
  where one repo is gone therefore looks like total failure unless you keep that stdout; see
  `gh.graphql`'s `partial_ok`.
- A PR's `closingIssuesReferences` is GraphQL-only, so the PR walk fetches it in one aliased
  query per fifty PRs, `pN: issueOrPullRequest(number: N)` under each repo alias — that field
  rather than `pullRequest`, so `assign` can ask about a ref it can't tell is a PR; a number
  that has gone since the search comes back null with a partial error, which `gh.graphql` keeps.
  The references can name an issue in **another repo**: each carries its own `repository`, and
  only one in the PR's own repo is carried (`carries`), since milestone numbers are per repo. Editing a PR
  body updates its references within seconds, but bumps its `updated_at`, which reorders a
  freshest-first walk — mind that when driving one from a pipe.
- A milestone's manual (drag) order is readable — GraphQL `Milestone.issues` returns it by
  default, REST does not — but **nothing writes it**: no REST parameter, no mutation. The walk's
  `^`/`$` modifiers are parsed for that day and refuse until it comes (#25).
- Issue fields (`Priority`, `Effort`, …) are an **organisation** feature and an **issue** feature:
  `users/{login}/issue-fields` is 404 for a user and `PullRequest` has no `issueFieldValues`.
  Read them with `organization(login:) { issueFields { nodes { ... on IssueFieldSingleSelect
  { id name options { id name } } } } }` (a union; `options` is a plain list) and write one with
  `setIssueFieldValue(input: {issueId, issueFields: [{fieldId, singleSelectOptionId}]})`;
  `[{fieldId, delete: true}]` clears it. The REST `orgs/{org}/issue-fields` listing carries the
  same node ids. `data.fetch_fields` asks `users/{owner}` for the account type first, so a user
  owner never triggers the NOT_FOUND warning the organisation query would print. Because a pull
  request can't hold one, **priority and effort are labels** (below); the fields are only read,
  and cleared.
- Advanced search takes `milestone:"Some title"` (quoted, spaces and all) alongside OR'd repos,
  and matches by **title alone, whatever the milestone's state** — so `triage --from X` where X
  is closed in one repo walks that repo's stranded items, with no `(current)` entry in the menu.
  `(no:milestone OR milestone:"X")` in one query is accepted too and returns exactly the sum
  (verified live), but `fetch_untriaged` runs one query set per source on purpose: the walk
  wants its sources in order, `--repo` goes through REST `issues?milestone=<number>` and is
  per source anyway, and each query keeps its own 1000-result headroom. Nothing escapes a `"`
  inside the qualifier, so `triage_sources` refuses such a title.
- `gh api --paginate --slurp` on `search/*` yields one **dict** per page (each wrapping
  `items`), not a list; `gh.api(paginate=True)` flattens both shapes.
- Milestone `due_on` takes a full ISO 8601 instant; send midday UTC (`...T12:00:00Z`) so it
  reads back on the day you meant. Clearing one needs `due_on: null`, which `gh.api` can't
  send — use `echo '{"due_on":null}' | gh api -X PATCH ... --input -`.
- Issue listings lag writes by up to ~10s: a rollover straight after another, or a triage run
  straight after filing an issue, can see a stale list. Rerunning works; not worth retry logic.

`TRIAGE_BUCKET` ("Needs triage") is the one bucket the walk reads from as well as writes to: by
default `triage` walks the no-milestone items and then the parked ones, `--from` (argparse
`dest="sources"`, since `from` is a keyword) replaces that set, and every item carries `milestone`
and `source`. Picking an item's current milestone is logged as a skip, no PATCH. A PR carries an
issue parked there onto its own milestone as it does an unplaced one (`carries`'s `parked`), in
the walk and in `assign` alike — but only while the config lists the bucket, so a config that
names its buckets by hand and hasn't added it treats "Needs triage" as any other milestone.

`triage`'s `c` prompt completes milestone titles with `readline`, which on macOS is libedit under
the same module name: `parse_and_bind("tab: complete")` does nothing there, so `ask_completing`
checks `readline.__doc__` for "libedit" and binds `^I rl_complete` instead. The completer works on
the whole line buffer (delimiters set to none), since titles have spaces. Nothing of this reaches a
piped run, which `input()` reads plainly.

The `triage` walk ends on `triage_summary`, printed from a `finally` so that a `q`, a ^C in
`ask` or a failed write still says what got written. It reads the walk's `done` log, so a new
write in the walk needs an entry there, or the summary will under-report it.

`check -i` reads single keypresses, but falls back to whole lines when stdin is not a tty, so
`printf 's\ns\ne\n2026-09-03\nq\n' | milestones check -i` drives it end to end. Its findings
are collected once up front and one milestone can raise several, so anything a walk does to a
milestone has to be carried across to its other findings by hand (`walk_findings`'s `rename` and
`gone`) — the list is never re-fetched mid-run.

`MILESTONES.md` holds the conventions this tool encodes: what each bucket means, and how release
milestones are used. `LABELS.md` does the same for the priority and effort labels. Both are written
for agents working in *other* repos, so keep them free of CLI detail. When `DEFAULT_BUCKETS` or `OPTIONAL_BUCKETS` changes, update it too;
`test_milestones_md_lists_the_default_buckets` catches a table that has fallen behind the list.

`assign` confirms only when stdin is a tty: piped refs (`triage --list | fzf -m | assign`, or a
coding agent) run unprompted, since `--yes` would kill the bare pipeline (EOF on the drained
pipe) and `/dev/tty` can't go into `ask()` while `check -i` is driven from a pipe. It is the
only write that never closes or deletes anything, which is what makes that safe. A PR ref is
accepted on purpose — the issues endpoint sets a PR's milestone too — and `triage --prs --list`
lists them; like the walk, it carries the issues a PR closes, counted before the prompt. To exercise the prompt itself, `expect -c 'spawn milestones assign …; expect
"Proceed?"; send "y\r"; expect eof'` works; `script -q /dev/null` with a piped answer does not —
it hands the program an EOF before the answer, so `ask()` aborts whatever you send.


Buckets are per repo, and a repo may use any subset of them: nothing here treats a missing
bucket as a problem. `free_buckets` is the configured buckets a repo hasn't got, and nothing at all
for a repo that has none of them. It is only the walk's rename targets — the buckets a repo already
has are exactly the ones a rename would 422 on. An optional bucket (`OPTIONAL_BUCKETS`, just
"Critical") is one `status` hides while nothing is open on it.

A closed standing bucket is invisible to `status` and to the `triage` menu, so nothing here may
close or delete one. That invariant is enforced in four places (`setup` reopens, `rollover` refuses
both a closed destination and `--close` on a bucket, and `milestone_problems` returns early for a
bucket, so `check` never raises a `done`, `empty`, `rename` or `undated` finding against one); add
the guard when you add a path that could close a milestone. A closed milestone of any kind with
work still on it is `check`'s `stranded` finding — the only one raised against a closed milestone,
buckets included — and `check -i`'s `[p]` reopens it, as `setup` would for a bucket. `status`
fetches closed milestones too, purely to count what is stranded in one closing line.

`print_table` measures every cell in characters, and there are two ways to make that lie. Colour
is one — `visible()` discounts the escapes, so a cell may carry its own, and `paint` is the only
thing that should put them there. Glyph width is the other, and nothing in the code can detect
it: a character whose East Asian Width is `A` (ambiguous) is drawn two columns wide in a
CJK-configured terminal, which is why the focus marker is U+2726 and not the obvious U+2605, and
why `★ ☆ ● ◆ ♥ •` are all unusable here. A glyph must be Neutral or Narrow *and* outside the emoji
set, since an emoji-presentation character is double-width whatever its width property says
(`⚔` and `✳` fail that second test while passing the first). `test_focus_marker_is_one_column_wide`
guards the marker; anything else you add to a table needs the same two checks by hand.

Colour is decided once at import (`COLOR`, from `sys.stdout.isatty()` and `NO_COLOR`), so a piped
run is plain and you cannot see the escapes you are debugging. `script -q /dev/null milestones
status` gives it a pty and shows the real thing; pipe that through `cat -v` to read the codes.

`write_repo_list` puts a top-level key the config hasn't got above the first `[table]` header and
above the comment block introducing it — a key appended to the end of the file would land *inside*
whatever table came last, so `ignore` would read back as `colors.ignore`. Keep new config keys
top-level and this keeps working; a new `[table]` of your own goes after every scalar key.

To exercise a command against one repo only, point `XDG_CONFIG_HOME` at a scratch config — but
symlink `~/.config/gh` into it too, since `gh` reads its auth from the same variable. This is not
optional when piping answers into `check -i` or `triage`: findings and items are ordered across
every tracked repo, so a keypress meant for a scratch milestone in `gaurav/milestones` lands on
whichever repo sorts first. One such run reopened a closed Babel milestone and rolled 27 issues.

Verify live against `gaurav/milestones` itself — `setup` is idempotent, and issues #1/#2 sit in
the standing buckets for exercising `rollover`/`triage`. `check` finds nothing there, though: its
only milestones are the four buckets, and a bucket raises nothing but `overdue`. To exercise the
walk's write paths, create a scratch milestone with no due date and no issues — that one milestone
raises `rename`, `undated` and `empty` together, so a single walk reaches the rename, the date and
the delete prompts, and `[d]` clears it up at the end. Tests (`uv run pytest`) cover only the
pure functions; keep it that way rather than mocking `gh`.

Every write goes through `gh._refuse_write`: `gh.api` with any method but GET, and `gh.mutate`
for a GraphQL mutation. That is what makes `MILESTONES_PERMISSIONS=read` trustworthy, so never
send a mutation through `gh.graphql`, which can't tell a write from a read. The refusal is
`gh.ReadOnly`, a `SystemExit` like every other failed call, so `check -i` and the priority writes
report it and carry on as they do for a failure; `rollover`, `assign`, `setup` and the `triage`
walk ask `gh.read_only()` first and print a preview instead, because a refusal at their first
write would show one line of a plan that has many.

Tokens are per owner (`env.token_key`), because a fine-grained token belongs to one resource
owner: `gh.api` reads the owner off the REST path (`gh.path_owner`), and `gh.graphql`/`gh.mutate`
take it as an argument — pass it for any query about a single owner. `doctor` tells a classic
token from a fine-grained one by the `X-OAuth-Scopes` header, which only a classic token sends.
Not yet verified live: whether a fine-grained token can read or write organisation issue fields
(`doctor`'s "issue fields" probe answers the read half the day someone runs it with one).

`gh.settings()` is cached for the run, so a test that wants a mode monkeypatches `gh.settings`
rather than the environment.

## The board

`milestones board` is `milestones/board/` (stdlib server) plus `ui/` (Preact, built by `npm run
build` into `milestones/board/static/`, which is gitignored). They share only the API in
`docs/BoardAPI.md`: change an endpoint and that file changes in the same commit. Keep `cli.py` out
of it beyond the subparser — the board has its own fetch (`board/data.py`), because a card needs
far more than `_norm_issue` keeps.

- The `Board` applies a change to its cards first and writes after, from one worker thread, in
  order. A failed write leaves the new value on the card (`load()` re-applies unsettled actions
  after a refetch for the same reason); only `revert` puts it back.
- One page of fifty issues with every card field takes about 3.5 s, so Babel's 539 issues and 60
  PRs load in under a minute. `Issue.issueFieldValues` is a union needing a fragment per value
  type, and each value's `field` is a union too. `closedByPullRequestsReferences` misses a PR that
  mentions an issue without closing it; those come from `timelineItems(itemTypes:
  [CROSS_REFERENCED_EVENT])`. `projectItems { totalCount }` is readable without `read:project`;
  a project's title and fields are not.
- Taking an issue off its milestone is `PATCH` with `{"milestone": null}`, which needs
  `gh.api(body=...)` (`--input -`); `-F milestone=null` sends the string.
- There is no browser tool in an agent session unless the Chrome extension is connected.
  `tools/board-check.mjs` (`npm run drive`) is the headless check: it starts its own read-only
  board on a scratch `XDG_STATE_HOME`, drives the URL, the menus and the card panel, and prints
  what held. Playwright is not a dependency here; `PLAYWRIGHT_FROM=~/code/prcoder` borrows that
  checkout's. Add a check there when you change something only a browser can show, and build
  first (`npm run build`): it drives `milestones/board/static/`, not `ui/`. It covers no write.
  Three things scripts get wrong that a person never notices: a key sent straight after a click
  arrives before a plain `useEffect` has run (hence the layout effects in `Menu` and `Choice`);
  Escape straight after opening a dialog can beat the field's focus, so wait a moment first; and
  Playwright's `dragTo` scrolls the target into view mid-drag, so a card in a column that is
  off-screen picks up whatever card ends under the pointer — drag between columns that are both
  visible.
- While the user has a board open from this checkout under `npm run dev`, the watcher rebuilds
  `static/` from whatever is checked out, and `npm run build` empties it first. So don't build
  (the watcher's output is what `npm run drive` should use) and don't check out another branch.
  To restack or test a lower branch of the stack, use a scratch `git worktree` with
  `node_modules` symlinked in. Build and drive there, then `git reset --keep` this checkout to
  the restacked tip once its tree matches the old one.
- Start a board the user will work in while you change the code with `--reload` (in the
  background, from this checkout: `uv run milestones board OWNER/NAME --reload`). When any
  `milestones/**/*.py` changes and then stays put for a couple of seconds, it waits for queued
  writes, then `exec`s itself, keeping its pid, its listening socket and its token, so the URL
  and `milestones suggest`'s session file stay good. The user refreshes the page when they want
  the change. It refetches the repo on every reload (about a minute for Babel), and a request in
  the meantime waits in the socket's backlog rather than failing. It keeps only the undo history
  and failed writes' log in memory, so those go; action ids carry on, so a stale page's undo
  finds nothing rather than the wrong action. Code that doesn't import, or that has no
  `--reload` (a branch below the one that added it, checked out to restack), is reported and
  not loaded, so the board stays on the code it had until you are back. Without `--reload`, a Python change needs a restart, which gives a new port and URL.
- An action's kind decides how its value is read off a card and put on one: `card_value` and
  `set_card_value` in `server.py`, mirrored by `cardValue` and `withValue` in `ui/shared/logic.ts`.
  A new kind needs all four, a branch in `Board.normalise` and `Board._write`, a row in
  `docs/BoardAPI.md`, and `describe` in `logic.ts`.
- `Board.submit` normalises each request twice: once up front so a bad one refuses the whole
  batch, and again as it is applied, against the card as the earlier requests left it. Computing
  every value from the starting card made two label changes in one batch overwrite each other.
- Priority and effort are the labels `Priority: Urgent|High|Medium|Low` and
  `Effort: XS|S|M|L|XL`, on issues and pull requests alike: `reconcile.KINDS` is the one table
  (options, colours, meanings, and what older spellings and the fields' values mean), used by the
  board, by `triage`'s `!` `+` `-`, `assign --priority` and `setup` (`cli.py` imports it; it is
  pure), and repeated for people in `LABELS.md`, which `test_labels_md_matches_the_label_table`
  holds to it. A request of kind `priority` or `effort` becomes a `labels` action in
  `Board.normalise`, so no action is ever of those kinds. Organisation fields were tried first and
  dropped: `PullRequest` has no field values, so pull requests could not be sorted with issues.
- Everything else that says a priority or effort is read and never written: `Priority: Critical`
  and `Size: M` labels, the config's per-repo `[labels]` aliases (which are never removed either:
  they are the repo's own), and the organisation's fields. The kinds `priority-field` and
  `effort-field` exist only to clear a field (and to put it back on undo). **Clearing a field from
  the board has not been run against a real organisation**: `gaurav` is a user account with none.
  It is `setIssueFieldValue` with `delete: true`.
- A standard label the repo lacks is created with its colour and description before the write that
  uses it (`reconcile.missing_standard`), and outlives an undo. That is fine in
  `gaurav/milestones`, where `setup` has made them all anyway.
- GitHub does not say when an issue field was last changed, so `reconcile.rule_suggestions` can't
  prefer "whichever is newer" between a field and a label; the standard label wins, because it is
  what this tool writes, and the reason says so.
- The board keeps undecided outside suggestions and all rejections in
  `$XDG_STATE_HOME/milestones/board-<owner>-<repo>.json` — the tool's only local state. Point
  `XDG_STATE_HOME` at a scratch directory when testing, or a test rejection will hide a real
  suggestion later.
- `milestones suggest` finds a running board through the session file `board/client.py` writes
  (`board-<owner>-<repo>.session.json` beside the suggestions file, mode 0600, removed on exit;
  SIGTERM is turned into the Ctrl-C path so `kill` cleans up too). `server.run` refuses to start
  a second board for a repo whose session file names a live process: two would overwrite each
  other's saved suggestions. It only ever POSTs
  suggestions, so it is safe for an agent to run unprompted and in a read-only run alike.
- `client.BUCKET_MEANINGS` repeats MILESTONES.md's bucket table for `board --brief`, and
  `test_brief_buckets_match_milestones_md` fails when either moves without the other.
- The board deliberately embeds no agent. An agent pointed at a bare issue has too little
  context; the one in the repo's checkout, in a conversation, has plenty — so the board hands it
  a prompt (`askPrompt` in `ui/shared/logic.ts`) and takes its answer back as suggestions.
- What the columns are is a front-end notion (`DimKind` in `ui/shared/logic.ts`): a dimension is
  an action kind whose values can be listed, so a drop is `{kind, value}` and the server needed
  nothing new but `labelValues` in the snapshot — `reconcile.KINDS` with the repo's aliases, so
  the page sorts `Priority: Critical` under Urgent by the same table the rules use, and takes its
  priority and effort columns from the table's `options`. Don't copy that table into
  `ui/`. A new dimension needs an entry in `DIMENSIONS`, branches in `dimColumns`, `columnFor`
  and `columnLabel`, and a server kind to write it.
- `grouped` is one flat, ordered list per column (grouping only changes the sort), which is what
  lets `step` and `nextFocus` ignore grouping; `sections` cuts that list into headed runs for
  drawing. `submit` in `ui/shared/state.ts` moves the focus on when a change takes the focused card
  out of its column, in any view.
- The page's layout (view, grouping, ticked milestones) is saved through `POST /api/v1/view` into
  the state file, not `localStorage`: each run is on a new port, which a browser treats as a new
  origin. `initialView` in `logic.ts` decides precedence: flags, then saved, then defaults.
  The flags arrive in the URL's query string (`server.page_url`), and the page writes the query
  back as the view changes (`viewQuery`, from `rememberView`), so "flags" on a reload means the
  view as it was left. `by` and `group` are always written, never left to the saved view, since
  the POST may still be in flight when the page reloads; a new view setting that a flag can set
  needs the same. The filters and the search are in the query too, and nowhere else: they are
  not part of the saved view, so they last for the tab. Only the token is in the fragment, and it stays there: it is what lets a
  request write to GitHub, and a fragment is never sent in a request line or a Referer.
- A toolbar dropdown is `Menu` in `ui/board/main.tsx`, not `<details>`, which closes on nothing
  but its own summary. One `openMenu` signal means one open at a time, and `onKey` stands down
  while it is set. The card panel's description is a sandboxed iframe whose clicks never reach
  the document, so `main.menu-open iframe` gets `pointer-events: none` for as long as a menu is
  open; a listener for window `blur` was tried for that and does not fire in headless Chromium.
  Its listeners go on in a layout effect: a plain effect waits a frame, and a scripted Escape
  sent straight after the click was missed.
- The card panel and the cards change a card only by opening the dialog a key opens (`Places` in
  `ui/board/main.tsx` sets `modal`; on a card, `onCard` focuses that card first), never with a
  control of their own: the dialogs already act on `targets()`, ask before reopening a closed
  milestone, and are undoable, and a second way in would have to repeat all three. A card leaves
  off what `shownByLayout` says the columns or the grouping already show. `isValueLabel` in
  `logic.ts` is what hides a label a button already speaks for; it reads the prefixes from the
  snapshot's `labelValues`.
- A dialog's field takes the keyboard through `ref={focusOnMount}`, not `autoFocus`: a browser
  ignores the attribute while another element has focus, so a dialog opened by clicking a button
  (rather than by its key) got no typing and no Escape. Use it for any field a click can open.
- A read-only board saves its changes to a plan file (`board/plan.py`; `Board.save_plan` after
  every submit and resolve). The plan is the **net** of the `would` actions, so an undone change
  is not in it. A writable board never applies a plan by itself: `review_plan` turns it into
  suggestions (source `plan`) and deletes the file — the suggestions persist in the state file
  from then on. Keep it that way; a plan is hours old and was made looking at a different board.
- `uv run` forwards SIGTERM to its child, so `pkill -f` on a board's command line delivers it
  twice, near enough at once; the first SIGTERM's handler ignores the rest. An ignored signal
  stays ignored across `exec`, so a reload puts SIGTERM back to the default first.
- Drive the write paths against `gaurav/milestones` only (`uv run --project .. milestones board
  gaurav/milestones` from `data/`, so no `.env` is picked up by accident), and look at Babel with
  `MILESTONES_PERMISSIONS=read`.
