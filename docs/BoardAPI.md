# The app's HTTP API

`milestones` starts one server on `127.0.0.1` and prints a URL. It serves every page of the app:
the home page at `/`, and a board for any repo at `/r/OWNER/NAME/`. Everything a page does goes
through the API below, and nothing else is shared between the server and the pages — so a front
end that looks and behaves quite differently can be built against it without touching the Python,
and a script or a coding agent can drive the same endpoints.

The front end in this repo lives in `ui/`, a folder per page. `ui/shared/api.ts` (a board) and
`ui/shared/app.ts` (everything else) are the only files in it that know a URL, and are the place
to start a new one.

## Reaching it

- There is one app at a time. It listens on port 8377 unless `--port` names another, or on any
  free port if 8377 is taken; a second `milestones` opens its page in the one already running.
- A page's URL carries a token in its fragment that is new on every start:
  `/r/OWNER/NAME/?from=<title>…#t=<token>`. It is in the fragment so that it is never sent in a
  request line or a `Referer`; a link from one page to another carries it on.
- A board's query string says how to open it. `from` repeats once per `--from` given on
  the command line, and is only a hint about which columns to show first; `by` and `group` (from
  `--by` and `--group`) are hints of the same kind. The page rewrites the query string as the
  view changes (`viewQuery` in `ui/shared/logic.ts`), so that a reload opens the same columns.
  One empty `from=` means no milestone is ticked. The page also keeps its filters there while
  they are on — `q` (the search), `kind`, `label`, `priority`, `pr=1`, `nolabels=1`, `author` —
  and unlike the view they are saved nowhere else, so a new run starts with none. `card=<number>`
  asks for that issue's or pull request's card to be focused with its panel open; it is read
  once, and gone from the query at the view's first change.
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
| `draft`, `closes`, `additions`, `deletions` | pull requests only: `closes` is `[{number, title, state}]`, issues in the same repo; `additions` and `deletions` are lines added and removed, from which the page estimates effort |
| `branch`, `base`, `basePr` | pull requests only: the head branch (`owner:branch` when it is from a fork), the branch it targets, and `{number, title, state, draft, url}` for the open pull request whose head is that branch, or `null` |
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
| `priority`, `effort` | an option's name (any case: `High`, `m`), or `null` to clear | the standard label, `Priority: X` or `Effort: X`, in place of every other label about that kind (`Priority: Critical`, `Size: M`). The action that comes back is of kind `labels`; an issue and a pull request take it alike. Clearing one (`null`) on an issue whose organisation field is set makes a second action, of kind `priority-field` or `effort-field`, that clears the field too, since the field is still read. A value that is not one of `labelValues`' options: `400`. |
| `priority-field`, `effort-field` | `null`, to clear it (or an option's name, which is how an undo puts it back) | the organisation's Priority or Effort issue field, which is read but no longer where either is kept. A pull request, or an owner with no such field: `400`. |
| `labels` | the whole list of names, or a change `{add: [...], remove: [...]}` | the issue's label set. The action's `value` is always the resulting list. A name the repo hasn't got is created, a standard priority or effort label in its colour and with its description. |
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

The board makes suggestions of its own from each card (`milestones/board/reconcile.py`): set the
standard `Priority:` or `Effort:` label from an older label, one of the repo's own or the
organisation's field, where those agree; then remove an older label, and clear a field
(`priority-field`, `effort-field`), that repeats or contradicts it. Undecided suggestions from outside, and every rejection, are kept in
`$XDG_STATE_HOME/milestones/board-<owner>-<repo>.json` (default `~/.local/state`), so a restart
loses neither and a rejected suggestion is not made again.

## Endpoints

A repo's board is everything under `/api/v1/repos/OWNER/NAME`, written `…` below. The repo need
not be in the config, and is read from GitHub the first time anything asks for its board: in
any spelling, since the name is settled with GitHub first (`404` for one it can't read). The
read has two parts. Its milestones, labels and fields come first, in a second or two; until
then `GET …/board` and `GET …/events` answer, and everything else is `503`. Then the board is
ready, and its cards arrive a page at a time, newest first, issues before pull requests, each
page as a `cards` event. Every endpoint works meanwhile, on the cards there are.
`POST …/suggestions` waits for the whole first read, so that its issue is on the board.

| | |
| --- | --- |
| `GET /api/v1/app` | `{permissions, repos, focus, config, boards}`: the mode, the tracked repos and the focused ones as the config spells them, where the config file is, and `[{repo, status}]` for each board opened so far (`loading`, `ready` or `failed`). |
| `GET /api/v1/overview` | A **section**, below, whose `data` is what the home page shows: `{milestones, quiet_repos, stranded, untriaged, untriaged_fresh, findings, kinds, buckets, repos, focus, today}`. `milestones` is every open milestone of the tracked repos in due order, each `{repo, title, number, due, open, closed, open_prs, closed_prs, percent, flags, focus, bucket, url}`; `stranded` is the closed ones still holding open work; `untriaged` is per repo `{repo, issues, prs, needs_triage, focus}`, and comes from a slower search, so a read publishes once with the last counts known (`untriaged_fresh: false`) and again with new ones; `findings` is what is wrong with a milestone, each `{kind, repo, title, number, detail, due, bucket, url, issues, prs, free_buckets}`, and `kinds` says what each kind's fix is. |
| `GET /api/v1/prs` | A section whose `data` is `{prs: [{repo, number, title, draft, updated, milestone, url, group}]}`: every open pull request you wrote, anywhere, `group` being `tracked`, `untracked` or `ignored`. `?assigned=1`, `?review_requested=1` and `?mentions=1` add the ones assigned to you, waiting on your review, or mentioning you; asking with different flags drops the kept answer and reads again. |
| `POST /api/v1/refresh` | Read sections again: body `{sections: ["overview", "prs"]}`, or no body for both. Returns each one's section as it stands, `refreshing`. |
| `PATCH /api/v1/milestones/OWNER/NAME/{number}` | Body any of `{title, due: "YYYY-MM-DD" \| null, state: "open" \| "closed"}`. The milestone is read from GitHub first, never taken from a kept answer. Closing a standing bucket is `409`. Returns `{milestone}`. Needs no board for the repo; one that is open is told to read its milestones again. |
| `DELETE /api/v1/milestones/OWNER/NAME/{number}` | Delete a milestone nothing was ever filed on. `409` for a standing bucket, and for one with any issue or pull request on it, open or closed. |
| `POST /api/v1/milestones/OWNER/NAME/{number}/rollover` | Body `{to, close?, dry?}`: move its open issues and pull requests onto the open milestone titled `to`, and close it after if `close`. Returns `{from, to, items: [{number, title, pr}], moved, closed}`; with `dry`, only says what would move. `409` for a title the repo hasn't got, a closed destination, or `close` on a standing bucket. |
| `GET /api/v1/events` | Server-sent events for the pages that are not a board: `overview` and `prs`, each carrying its section whenever it changes. |
| `GET …/board` | Until the milestones are read: `{repo, permissions, loading: {}, error}`, where `error` is why the read failed, if it did; ask again until `loading` is absent. `POST …/refresh` tries a failed read again. Then: `{repo, permissions, filling, gaps, buckets, milestones, columns, cards, actions, labels, fields, suggestions}`. `filling` is `{what, seen, total}` while cards are still arriving (`what` is `issues` or `pullRequests`, `total` is `null` until the first page), and `null` after. `gaps` is what the last read couldn't get, each `{what, seen, total, error}`: a page GitHub kept failing on, after three more tries with smaller pages, stops that kind there. A read with gaps removes no card, and leaves the plan to a later one. `permissions` is `"read"` or `"write"`; `columns` is the open milestones' numbers in display order; `labels` is every label in the repo; `fields` is the owner's single-select issue fields as `{name: [option names]}`, empty for a user account; `suggestions` leaves out rejected ones; `labelValues` is the priority and effort label scheme, as `{priority: {field, prefix, options, colors, meanings, values, aliases}, effort: {…}}` — `options` are the values in order, each written as the label `<field>: <option>`; `prefix` is a regular expression for the name, before its colon, of any label about the kind; `values` maps what follows the colon, or the organisation field's value, lower-cased, to an option; `aliases` maps a label of the repo's own, lower-cased, to the option it stands for; `view` is what was last saved with `POST …/view`, or `{}`; `plan` is `{path, count}` for the plan file, below. |
| `POST …/actions` | Body `{actions: [{issue, kind, value}]}`. Applies them to the cards at once and in order, queues the writes, and returns `{actions: [Action]}` — including any made for the issues a moved pull request closes. A change that changes nothing makes no action. One bad request refuses the whole batch: `400` unknown kind or bad value, `404` unknown issue or milestone, `409` closed milestone. |
| `POST …/actions/{id}/retry` | Queue a `failed` action again. `409` if it is not failed. |
| `POST …/actions/{id}/revert` | Give up on a `failed` action; the card goes back to `previous` unless a later action on it stands. |
| `POST …/suggestions` | Body `{suggestions: [{issue, kind, value, reason, source}]}`. Here alone a `milestone` value may be a **title** (matched ignoring case), for a caller with no way to know the numbers. Each is checked as an action would be, so one that could never be accepted is refused with why. Returns `{suggestions}`: the ones added — one already open, or already rejected, is skipped. Writes nothing to GitHub, in any mode. |
| `POST …/suggestions/resolve` | Body `{ids: [...], decision: "accept" \| "reject"}`. Returns `{suggestions, actions}`: the suggestions that changed (including any of the board's own that accepting made due or moot) and the actions accepting made. |
| `POST …/plan/review` | On a writable board, turn the plan a read-only session left into suggestions with source `plan`, and remove the plan file. Returns `{suggestions, dropped}`: `dropped` counts changes already made, or no longer possible. `409` on a read-only board, and while the board is still reading its cards or has `gaps`, since a card not read would count as gone. |
| `POST …/plan/discard` | Delete the plan file without reviewing it. `409` on a read-only board, where the plan is the session's own changes: undo them instead. |
| `POST …/view` | Body `{by, group, shown, detail?}`: what the columns are (`milestone`, `priority` or `effort`), the dimensions cards are grouped by, the milestone numbers ticked (`0` for none), and whether the card panel is open (`detail`, default `true`). Kept in the repo's state file and handed back as `view` in the board, so the next run can open the same way. A front end's own business: the server only stores it. |
| `POST …/milestones` | Body `{title, reopen?}`. Returns `{milestone}`: the open milestone of that title (matched ignoring case), created if there is none. A closed one is `409` unless `reopen` is true. |
| `PATCH …/milestones/{number}` | Body `{due: "YYYY-MM-DD"}` or `{due: null}`. Returns `{milestone}`. |
| `POST …/refresh` | Read everything from GitHub again, in the background: returns the board at once, with `filling` set, and the cards arrive as `cards` events. A card read to the end of its kind and not found has closed, and leaves the board. Changes not yet written stay on their cards, as does a change written after the read began. With body `{issues: [number, …]}`, read only those cards, and the milestones; one that has closed, merged or gone leaves the board, and the returned board's `gone` lists them. |
| `GET …/events` | Server-sent events, below. |

Milestone writes are made before the response and fail with `502` and GitHub's message; in a
read-only run they are `403` with what would have been done. Nothing in the API closes or deletes
a milestone.

**Section** — an answer that takes seconds to read, kept on disk
(`$XDG_STATE_HOME/milestones/<name>.json`) so that a page has something to show at once:
`{data, fetched_at, params, fresh, refreshing, error}`. `data` is the last answer, or `null` if
there has never been one; `fetched_at` is when it was read. Asking for a section starts a read
if this run has not made one, or made it more than five minutes ago, and the new answer arrives
as an event. `fresh` is true only for an answer this run read to the end. The kept copy is for
looking at: a page should offer no write that was decided from it until `fresh`, and the
milestone endpoints read the milestone again themselves either way. `error` is why the last
read failed; `data` is then still the answer before it.

A read-only app refuses the three milestone writes with `403`, saying what it would have done.

## Events

| Event | Data | When |
| --- | --- | --- |
| `action` | an Action | it is made, and each time its status changes |
| `milestone` | a Milestone | one is created, reopened or re-dated |
| `suggestion` | a Suggestion | one is added, accepted, rejected or withdrawn |
| `plan` | `{path, count}` | the plan file changed: after any action on a read-only board, and after a review or a discard |
| `cards` | `{cards, filling, suggestions}` | a page of cards has been read: put them on the board, in place of any of the same number; `suggestions` are the ones they brought (kept from an earlier run, or the board's own rules) |
| `reload` | `{}` | a read started or finished; fetch the board again |
| `stopped` | `{}` | the app is shutting down (not on a `--reload`, which keeps every connection's place); also sent on `GET /api/v1/events` |

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

`issue`, `kind` and `value` are what `POST …/actions` takes; `title` and `summary` are for
whoever reads the file. A read-only board applies the file to its cards when it starts. A writable
board never applies it: `plan.count` in the board says one is waiting, and `/plan/review` makes
suggestions of it. A labels change becomes `{add, remove}` on review, so labels added since the
plan survive it.

## Finding the running app

While it runs, the app keeps `$XDG_STATE_HOME/milestones/app.session.json` (mode 0600) holding
`{url, token, pid, permissions}`, and removes it when it stops. That is how `milestones suggest`
reaches a repo's board, and how a second `milestones` finds the first; anything else on the same
machine can do the same. A file left behind by an app that was killed outright names a process
that is gone.

## Trying it

```sh
milestones --no-open --port 8765     # prints the URL with its token
T=<token>
R=http://127.0.0.1:8765/api/v1/repos/gaurav/milestones
curl -s -H "X-Milestones-Token: $T" $R/board | jq '.columns'
curl -s -H "X-Milestones-Token: $T" -H 'Content-Type: application/json' \
     -d '{"actions":[{"issue":1,"kind":"milestone","value":3}]}' $R/actions
curl -sN "$R/events?token=$T"
```
