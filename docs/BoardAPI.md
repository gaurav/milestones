# The board's HTTP API

`milestones board` starts a server on `127.0.0.1` and prints a URL. Everything the page does goes
through the API below, and nothing else is shared between the server and the page — so a front end
that looks and behaves quite differently can be built against it without touching the Python, and
a script or a coding agent can drive the same endpoints.

The front end in this repo lives in `ui/`; `ui/shared/api.ts` is the only file in it that knows a
URL, and is the place to start a new one.

## Reaching it

- The URL's fragment carries a token that is new on every start: `#t=<token>&from=<title>…`.
  `from` repeats once per `--from` given on the command line, and is only a hint about which
  columns to show first; `by` and `group` (from `--by` and `--group`) are hints of the same kind.
- Every `/api/` request must send the token as the `X-Milestones-Token` header. The event stream
  alone also accepts it as `?token=`, since a browser's `EventSource` cannot set a header.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`.
- Request and response bodies are JSON. An error is `{"error": "<message>"}` with a 4xx or 5xx
  status.

## Objects

**Card** — an open issue or pull request.

| Field | |
| --- | --- |
| `number`, `id`, `title`, `url` | `id` is the GraphQL node id |
| `kind` | `"issue"` or `"pr"` |
| `milestone` | the milestone's number, or `null` |
| `bodyHTML` | the description as GitHub renders it |
| `created`, `updated`, `author`, `comments`, `assignees` | `author` is `null` for a deleted account |
| `labels` | `[{name, color}]` |
| `type`, `parent`, `subIssues`, `projects` | issues only: issue type name, `{number, title}`, `{total, completed}`, how many projects it is in |
| `fields` | issues only: the organisation's issue fields by name, e.g. `{"Priority": "High"}` |
| `closingPrs`, `mentionedPrs` | issues only: `[{number, title, state, draft, url}]`; a pull request appears in one list, never both |
| `draft`, `closes` | pull requests only: `closes` is `[{number, title, state}]`, issues in the same repo |
| `closed` | absent until this session closes the card, then the reason; a refresh drops closed cards |

**Milestone** — `{number, title, state, due, description, url}`; `state` is `"open"` or
`"closed"`, `due` is `YYYY-MM-DD` or `null`.

**Action** — one change to one card, and whether GitHub has it yet.

| Field | |
| --- | --- |
| `id` | counts up from 1 for the session |
| `issue`, `kind`, `value`, `previous` | what changed, by kind, below; `previous` is what the card held before |
| `carriedBy` | the pull request whose move made this one, or `null` |
| `status` | `pending` (queued or being written), `done`, `failed`, `would` (a read-only run: shown, never written), `reverted` (a failed action given up on) |
| `error` | what `gh` said, for a `failed` action |

A card always shows, for each kind, the `value` of its latest action of that kind that is not
`reverted`, whatever that action's status: a failed write keeps what you asked for on screen until
you retry it or revert it.

**Kinds** — what an action can change, and the `value` each takes.

| `kind` | `value` | Written as |
| --- | --- | --- |
| `milestone` | a milestone's number, or `null` for none | the issue's milestone |
| `priority`, `effort` | an option's name (any case), or `null` to clear | the organisation's Priority or Effort issue field. Where the owner has no such field, a `Priority: X` or `Effort: X` label instead — the action that comes back is then of kind `labels`. A pull request has no fields: `400`. |
| `labels` | the whole list of names, or a change `{add: [...], remove: [...]}` | the issue's label set. The action's `value` is always the resulting list. A name the repo hasn't got is created. |
| `title` | text | the title |
| `state` | `"completed"` or `"not_planned"` to close, `null` to reopen | the issue's state |
| `comment` | Markdown | a new comment. Not card state: `previous` is `null`, and it cannot be undone. |

**Suggestion** — a change someone proposes, for a person to accept or reject.

| Field | |
| --- | --- |
| `id`, `issue`, `kind`, `value` | as an action's; a `labels` suggestion usually carries a change (`{add, remove}`), so that it still applies if the labels have moved on |
| `reason` | why, in a sentence or two — shown beside it |
| `source` | who proposes it: an agent's or a model's name, or `rule:<name>` for the board's own |
| `status` | `open`, `accepted`, `rejected`, or `withdrawn` (a rule's suggestion that no longer applies) |
| `action` | the action an accepted suggestion became |

The board makes suggestions of its own from each card (`milestones/board/reconcile.py`): fill an
empty Priority or Effort field from a `Priority:` or `Size:` label, then remove a label the field
repeats or contradicts. Undecided suggestions from outside, and every rejection, are kept in
`$XDG_STATE_HOME/milestones/board-<owner>-<repo>.json` (default `~/.local/state`), so a restart
loses neither and a rejected suggestion is not made again.

## Endpoints

| | |
| --- | --- |
| `GET /api/v1/board` | `{repo, permissions, buckets, milestones, columns, cards, actions, labels, fields, suggestions}`. `permissions` is `"read"` or `"write"`; `columns` is the open milestones' numbers in display order; `labels` is every label in the repo; `fields` is the owner's single-select issue fields as `{name: [option names]}`, empty for a user account; `suggestions` leaves out rejected ones; `labelValues` is what a label means in a field's terms, as `{priority: {field, prefix, values}, effort: {…}}` — `prefix` is a regular expression for the label's name before its colon, and `values` maps what follows it, lower-cased, to an option's name; `view` is what was last saved with `POST /api/v1/view`, or `{}`; `plan` is `{path, count}` for the plan file, below. |
| `POST /api/v1/actions` | Body `{actions: [{issue, kind, value}]}`. Applies them to the cards at once and in order, queues the writes, and returns `{actions: [Action]}` — including any made for the issues a moved pull request closes. A change that changes nothing makes no action. One bad request refuses the whole batch: `400` unknown kind or bad value, `404` unknown issue or milestone, `409` closed milestone. |
| `POST /api/v1/actions/{id}/retry` | Queue a `failed` action again. `409` if it is not failed. |
| `POST /api/v1/actions/{id}/revert` | Give up on a `failed` action; the card goes back to `previous` unless a later action on it stands. |
| `POST /api/v1/suggestions` | Body `{suggestions: [{issue, kind, value, reason, source}]}`. Here alone a `milestone` value may be a **title** (matched ignoring case), for a caller with no way to know the numbers. Each is checked as an action would be, so one that could never be accepted is refused with why. Returns `{suggestions}`: the ones added — one already open, or already rejected, is skipped. Writes nothing to GitHub, in any mode. |
| `POST /api/v1/suggestions/resolve` | Body `{ids: [...], decision: "accept" \| "reject"}`. Returns `{suggestions, actions}`: the suggestions that changed (including any of the board's own that accepting made due or moot) and the actions accepting made. |
| `POST /api/v1/plan/review` | On a writable board, turn the plan a read-only session left into suggestions with source `plan`, and remove the plan file. Returns `{suggestions, dropped}`: `dropped` counts changes already made, or no longer possible. `409` on a read-only board. |
| `POST /api/v1/plan/discard` | Delete the plan file without reviewing it. `409` on a read-only board, where the plan is the session's own changes: undo them instead. |
| `POST /api/v1/view` | Body `{by, group, shown}`: what the columns are (`milestone`, `priority` or `effort`), the dimensions cards are grouped by, and the milestone numbers ticked (`0` for none). Kept in the repo's state file and handed back as `view` in the board, so the next run can open the same way. A front end's own business: the server only stores it. |
| `POST /api/v1/milestones` | Body `{title, reopen?}`. Returns `{milestone}`: the open milestone of that title (matched ignoring case), created if there is none. A closed one is `409` unless `reopen` is true. |
| `PATCH /api/v1/milestones/{number}` | Body `{due: "YYYY-MM-DD"}` or `{due: null}`. Returns `{milestone}`. |
| `POST /api/v1/refresh` | Read everything from GitHub again; returns the board. Changes not yet written stay on their cards. |
| `GET /api/v1/events` | Server-sent events, below. |

Milestone writes are made before the response and fail with `502` and GitHub's message; in a
read-only run they are `403` with what would have been done. Nothing in the API closes or deletes
a milestone.

## Events

| Event | Data | When |
| --- | --- | --- |
| `action` | an Action | it is made, and each time its status changes |
| `milestone` | a Milestone | one is created, reopened or re-dated |
| `suggestion` | a Suggestion | one is added, accepted, rejected or withdrawn |
| `plan` | `{path, count}` | the plan file changed: after any action on a read-only board, and after a review or a discard |
| `reload` | `{}` | a refresh finished; fetch the board again |

A comment line arrives every 15 seconds to keep the connection open.

## The plan file

On a read-only board every action has status `would` and none is written. The net of them — per
card and kind, from what the card held before the first to what the last left it with; nothing
for a change that was undone; every comment — is saved after each change to
`$XDG_STATE_HOME/milestones/board-<owner>-<repo>.plan.json`:

```json
{"about": "…", "repo": "o/r", "saved": "2026-11-11T22:39:38-05:00",
 "changes": [{"issue": 2, "kind": "milestone", "value": null, "previous": 3,
              "title": "…", "summary": "#2: milestone → none (was Not urgent)"}]}
```

`issue`, `kind` and `value` are what `POST /api/v1/actions` takes; `title` and `summary` are for
whoever reads the file. A read-only board applies the file to its cards when it starts. A writable
board never applies it: `plan.count` in the board says one is waiting, and `/plan/review` makes
suggestions of it. A labels change becomes `{add, remove}` on review, so labels added since the
plan survive it.

## Finding a running board

While it runs, a board keeps `$XDG_STATE_HOME/milestones/board-<owner>-<repo>.session.json`
(mode 0600) holding `{repo, url, token, pid}`, and removes it when it stops. That is how
`milestones suggest` finds the board for a repo; anything else on the same machine can do the same.
A file left behind by a board that was killed outright points at a port nothing answers on.

## Trying it

```sh
milestones board gaurav/milestones --no-open --port 8765     # prints the URL with its token
T=<token>
curl -s -H "X-Milestones-Token: $T" http://127.0.0.1:8765/api/v1/board | jq '.columns'
curl -s -H "X-Milestones-Token: $T" -H 'Content-Type: application/json' \
     -d '{"actions":[{"issue":1,"kind":"milestone","value":3}]}' \
     http://127.0.0.1:8765/api/v1/actions
curl -sN "http://127.0.0.1:8765/api/v1/events?token=$T"
```
