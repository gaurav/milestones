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
- A milestone's `open_issues` counts **pull requests** too, so it never agrees with an
  issues-only listing; don't use it as an "is this empty" check.
- `gh api --paginate --slurp` on `search/*` yields one **dict** per page (each wrapping
  `items`), not a list, so `gh.api(paginate=True)`'s flattening does not apply.
- Milestone `due_on` takes a full ISO 8601 instant; send midday UTC (`...T12:00:00Z`) so it
  reads back on the day you meant. Clearing one needs `due_on: null`, which `gh.api` can't
  send — use `echo '{"due_on":null}' | gh api -X PATCH ... --input -`.
- Issue listings lag writes by up to ~10s: a rollover straight after another, or a triage run
  straight after filing an issue, can see a stale list. Rerunning works; not worth retry logic.

`check -i` reads single keypresses, but falls back to whole lines when stdin is not a tty, so
`printf 's\ns\ne\n2026-09-03\nq\n' | milestones check -i` drives it end to end. Its findings
are collected once up front, so a title renamed mid-walk still shows its old name later in the
same run.

To exercise a command against one repo only, point `XDG_CONFIG_HOME` at a scratch config — but
symlink `~/.config/gh` into it too, since `gh` reads its auth from the same variable.

Verify live against `gaurav/milestones` itself — `setup` is idempotent, and issues #1/#2 sit in
the standing buckets for exercising `rollover`/`triage`. Tests (`uv run pytest`) cover only the
pure functions; keep it that way rather than mocking `gh`.
