# milestones — agent notes

All GitHub access goes through `milestones/gh.py`, which shells out to the authenticated `gh`
CLI. Hard-won API facts (verified live, Aug 2026):

- `gh search issues` silently returns **zero results** — it uses the retired legacy search
  path. Use REST `search/issues?q=...&advanced_search=true` instead (see `gh.search_issues`).
- Advanced search **ANDs** repeated qualifiers (legacy OR'd them); OR them explicitly:
  `(repo:a/b OR repo:c/d)`. Query strings cap at 256 chars, so `build_search_queries`
  splits the configured repos across as many queries as that takes.
- Milestone writes are REST-only; no GraphQL mutations exist. Reads are fine in GraphQL.
- GraphQL `repositories` defaults `ownerAffiliations` to include collaborator repos — pass
  `ownerAffiliations: OWNER`. Transferred repos can still echo under their old owner; dedupe.
- `repository(owner:, name:)` follows renames and normalises case, so the `nameWithOwner` coming
  back need not match the config's spelling. Key anything per-repo off the returned name, never
  off the configured string — `fetch_milestones` returns the names for exactly that reason.
- A milestone's `open_issues` counts **pull requests** too, so it never agrees with an
  issues-only listing; don't use it as an "is this empty" check. The `triage` menu shows it
  anyway, on purpose — a PR on a milestone is work sitting on that milestone.
- `gh api graphql` exits **nonzero on any GraphQL error**, including a partial one, but still
  prints the whole response — resolved data and all — on stdout. An aliased multi-repo query
  where one repo is gone therefore looks like total failure unless you keep that stdout; see
  `gh.graphql`'s `partial_ok`.
- `gh api --paginate --slurp` on `search/*` yields one **dict** per page (each wrapping
  `items`), not a list; `gh.api(paginate=True)` flattens both shapes.
- Milestone `due_on` takes a full ISO 8601 instant; send midday UTC (`...T12:00:00Z`) so it
  reads back on the day you meant. Clearing one needs `due_on: null`, which `gh.api` can't
  send — use `echo '{"due_on":null}' | gh api -X PATCH ... --input -`.
- Issue listings lag writes by up to ~10s: a rollover straight after another, or a triage run
  straight after filing an issue, can see a stale list. Rerunning works; not worth retry logic.

`check -i` reads single keypresses, but falls back to whole lines when stdin is not a tty, so
`printf 's\ns\ne\n2026-09-03\nq\n' | milestones check -i` drives it end to end. Its findings
are collected once up front and one milestone can raise several, so anything a walk does to a
milestone has to be carried across to its other findings by hand (`walk_findings`'s `rename` and
`gone`) — the list is never re-fetched mid-run.

`assign` confirms only when stdin is a tty: piped refs (`triage --list | fzf -m | assign`, or a
coding agent) run unprompted, since `--yes` would kill the bare pipeline (EOF on the drained
pipe) and `/dev/tty` can't go into `ask()` while `check -i` is driven from a pipe. It is the
only write that never closes or deletes anything, which is what makes that safe. A PR ref is
accepted on purpose — the issues endpoint sets a PR's milestone too — but `triage --list` never
lists one. To exercise the prompt itself, `expect -c 'spawn milestones assign …; expect
"Proceed?"; send "y\r"; expect eof'` works; `script -q /dev/null` with a piped answer does not —
it hands the program an EOF before the answer, so `ask()` aborts whatever you send.

Buckets are per repo. `missing_buckets` is the one answer: the configured buckets a repo hasn't
got, and nothing at all for a repo that has none of them, since using none is a choice rather than
a gap. It feeds both the `buckets` finding and the walk's rename options — the buckets a repo
already has are exactly the ones a rename would 422 on.

A closed standing bucket is invisible to `status` and to the `triage` menu, and only `setup`
brings it back — so nothing here may close or delete one. That invariant is enforced in four
places (`setup` reopens, `rollover` refuses both a closed destination and `--close` on a bucket,
and `milestone_problems` returns early for a bucket, so `check` never raises a `done`, `empty`,
`rename` or `undated` finding against one); add the guard when you add a path that could close a
milestone.

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
symlink `~/.config/gh` into it too, since `gh` reads its auth from the same variable.

Verify live against `gaurav/milestones` itself — `setup` is idempotent, and issues #1/#2 sit in
the standing buckets for exercising `rollover`/`triage`. `check` finds nothing there, though: its
only milestones are the four buckets, and a bucket raises nothing but `overdue`. To exercise the
walk's write paths, create a scratch milestone with no due date and no issues — that one milestone
raises `rename`, `undated` and `empty` together, so a single walk reaches the rename, the date and
the delete prompts, and `[d]` clears it up at the end. Tests (`uv run pytest`) cover only the
pure functions; keep it that way rather than mocking `gh`.
