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

A closed standing bucket is invisible to `status` and to the `triage` menu, and only `setup`
brings it back — so nothing here may close or delete one. That invariant is enforced in four
places (`setup` reopens, `rollover` refuses both a closed destination and `--close` on a bucket,
`check` skips buckets in its `done`, `empty`, `rename` and `undated` rules); add the guard when
you add a path that could close a milestone.

To exercise a command against one repo only, point `XDG_CONFIG_HOME` at a scratch config — but
symlink `~/.config/gh` into it too, since `gh` reads its auth from the same variable.

Verify live against `gaurav/milestones` itself — `setup` is idempotent, and issues #1/#2 sit in
the standing buckets for exercising `rollover`/`triage`. Tests (`uv run pytest`) cover only the
pure functions; keep it that way rather than mocking `gh`.
