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
  columns to show first.
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

**Milestone** — `{number, title, state, due, description, url}`; `state` is `"open"` or
`"closed"`, `due` is `YYYY-MM-DD` or `null`.

**Action** — one change to one card, and whether GitHub has it yet.

| Field | |
| --- | --- |
| `id` | counts up from 1 for the session |
| `issue`, `kind`, `value`, `previous` | `kind` is `"milestone"`; `value` and `previous` are milestone numbers or `null` |
| `carriedBy` | the pull request whose move made this one, or `null` |
| `status` | `pending` (queued or being written), `done`, `failed`, `would` (a read-only run: shown, never written), `reverted` (given up on: a failed action reverted, or one that a later change to the same card replaced before or after its write failed) |
| `error` | what `gh` said, for a `failed` action |
| `rev` | the number of the last event about it (`0` before its first): of two copies of one action, the one with the higher `rev` is newer |

A card shows the `value` of its latest action that is not `reverted` while that action is
`pending`, `failed` or `would`: a failed write keeps what you asked for on screen until you retry it
or revert it. Once that action is `done`, or none is left standing, the card shows what GitHub has
— which a refresh may find someone else has changed. A failed action is only ever the latest
standing one on its card: a later change to the card gives it up, so a retry can never write over
something newer.

## Endpoints

| | |
| --- | --- |
| `GET /api/v1/board` | `{repo, permissions, buckets, milestones, columns, cards, actions, rev}`. `permissions` is `"read"` or `"write"`; `columns` is the open milestones' numbers in display order; `rev` is the number of the last event the snapshot includes. |
| `POST /api/v1/actions` | Body `{actions: [{issue, kind: "milestone", value, carryParked?, carry?}]}`. `value` is required: a milestone number, or `null` to take the card off its milestone. Applies them to the cards at once, queues the writes, and returns `{actions: [Action], held: [{issue, carriedBy}]}`. A pull request moved onto a milestone takes the open issues it closes that have no milestone; those parked on Needs triage go too only with `carryParked: true`, and are otherwise listed in `held` and left where they are. Send it only when a person asked for the move — a parked issue is one a person put there, and an agent should leave it. An issue already on the target is never carried, and `carry: false` carries nothing — what an undo sends, to put back exactly the cards it names. A move to where the card already is makes no action. One bad request refuses the whole batch: `400` unknown kind or no `value`, `404` unknown issue or milestone, `409` closed milestone. |
| `POST /api/v1/actions/{id}/retry` | Queue a `failed` action again. `409` if it is not failed. |
| `POST /api/v1/actions/{id}/revert` | Give up on a `failed` action. Returns `{action, card}`: the card shows what GitHub has, unless a pending or read-only action on it stands. |
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
| `card` | a Card | a revert put it back on what GitHub has |
| `milestone` | a Milestone | one is created, reopened or re-dated |
| `reload` | `{}` | a refresh finished; fetch the board again |

Each event's SSE `id` is its number, counting up from 1 for the session, and is always sent after the change it reports. A client that keeps itself in step fetches the board only once the stream is open, holds events back while that fetch is under way, and then replays the ones numbered after the snapshot's `rev`. Nothing is replayed after a reconnection, so a client fetches the board again each time the stream reopens.

A comment line arrives every 15 seconds to keep the connection open.

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
