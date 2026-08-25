# milestones — agent notes

All GitHub access goes through `milestones/gh.py`, which shells out to the authenticated `gh`
CLI. Hard-won API facts (verified live, Aug 2026):

- `gh search issues` silently returns **zero results** — it uses the retired legacy search
  path. Use REST `search/issues?q=...&advanced_search=true` instead (see `gh.search_issues`).
- Advanced search **ANDs** repeated qualifiers (legacy OR'd them); OR owners explicitly:
  `(user:a OR user:b)`.
- Milestone writes are REST-only; no GraphQL mutations exist. Reads are fine in GraphQL.
- GraphQL `repositories` defaults `ownerAffiliations` to include collaborator repos — pass
  `ownerAffiliations: OWNER`. Transferred repos can still echo under their old owner; dedupe.
- Issue listings lag writes by a few seconds: a rollover immediately after another can see a
  stale (empty) issue list. Rerunning works; not worth retry logic.

Verify live against `gaurav/milestones` itself — `setup` is idempotent, and issues #1/#2 sit in
the standing buckets for exercising `rollover`/`triage`. Tests (`uv run pytest`) cover only the
pure functions; keep it that way rather than mocking `gh`.
