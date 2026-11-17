// The board's HTTP API, as docs/BoardAPI.md describes it. This is the only file that knows a
// URL: a different front end starts by copying it.

export interface Label { name: string; color: string }
export interface PrRef { number: number; title: string; state: string; draft: boolean; url: string }

export interface Card {
  id: string;
  number: number;
  kind: 'issue' | 'pr';
  title: string;
  url: string;
  bodyHTML: string;
  created: string;
  updated: string;
  author: string | null;
  comments: number;
  assignees: string[];
  labels: Label[];
  /** The milestone's number; null for none. */
  milestone: number | null;
  // Issues only.
  type?: string | null;
  parent?: { number: number; title: string } | null;
  subIssues?: { total: number; completed: number };
  /** The organisation's issue fields by name: Priority, Effort, … */
  fields?: Record<string, string | number | null>;
  closingPrs?: PrRef[];
  mentionedPrs?: PrRef[];
  projects?: number;
  // Pull requests only.
  draft?: boolean;
  closes?: { number: number; title: string; state: string }[];
  /** Pull requests only: lines added and removed. */
  additions?: number;
  deletions?: number;
  /** Pull requests only: its head branch (`owner:branch` from a fork), the branch it targets,
   * and the open pull request whose head is that branch — the one below it in a stack. */
  branch?: string;
  base?: string;
  basePr?: PrRef | null;
  /** Set once this session closes the card: the reason. A refresh drops closed cards. */
  closed?: string | null;
}

export interface Milestone {
  number: number;
  title: string;
  state: 'open' | 'closed';
  due: string | null;
  description: string;
  url: string;
}

export type ActionStatus = 'pending' | 'done' | 'failed' | 'would' | 'reverted';

export type Kind = 'milestone' | 'priority' | 'effort' | 'labels' | 'title' | 'state' | 'comment' | 'priority-field' | 'effort-field';
/** What a kind's value is: a milestone number, a field option's name, label names, text,
 *  a close reason; null clears it (and reopens, for state). */
export type Value = number | string | string[] | null;

export interface Action {
  id: number;
  issue: number;
  kind: Kind;
  value: Value;
  previous: Value;
  carriedBy: number | null;
  error: string | null;
  status: ActionStatus;
}

/** How far a read of the cards has got: `what` is `issues` or `pullRequests`; `total` is null
 *  until the first page is in. */
export interface Filling { what: string; seen: number; total: number | null }

/** What the last read of the cards couldn't get. */
export interface Gap { what: string; seen: number; total: number | null; error: string }

/** A page of cards, as the `cards` event brings it while the board is read. */
export interface CardsPage { cards: Card[]; filling: Filling; suggestions: Suggestion[] }

export interface BoardData {
  /** Only until the repo's milestones are read, or if they could not be: `error` if it
   *  stopped. Nothing else but `repo` and `permissions` comes with it. */
  loading?: Record<string, never>;
  /** While the cards are still arriving; null once the read has ended. */
  filling?: Filling | null;
  gaps?: Gap[];
  error?: string | null;
  /** From a refresh only: the cards it found closed, merged or gone, and took off the board. */
  gone?: number[];
  repo: string;
  permissions: 'read' | 'write';
  buckets: string[];
  milestones: Milestone[];
  /** Open milestones' numbers, in column order. */
  columns: number[];
  cards: Card[];
  actions: Action[];
  /** Every label the repo has. */
  labels: Label[];
  /** The owner's single-select issue fields and their options; empty for a user account. */
  fields: Record<string, string[]>;
  suggestions: Suggestion[];
  /** What a label means in a field's terms, per kind: `prefix` is a regular expression for
   *  the label's name before its colon, `values` maps what follows (lower-cased) to an option. */
  labelValues?: Record<string, { field: string; prefix: string; values: Record<string, string>; options?: string[]; aliases?: Record<string, string> }>;
  /** How the board was last looked at, if it has been; see `View`. */
  view?: Partial<View>;
  plan?: PlanInfo;
}

/** The plan file: on a read-only board, this session's changes, saved as they are made; on
 *  a writable one, what an earlier read-only session left to be reviewed. */
export interface PlanInfo { path: string | null; count: number }

/** What the columns are, the grouping within them, the milestones ticked (0 is none), and
 *  whether the card panel on the right is open (absent, from an older run, means open). */
export interface View { by: string; group: string[]; shown: number[]; detail?: boolean }

/** A change someone proposes — a coding agent, or the board's own rules — for you to
 *  accept or reject. A labels suggestion's value is a change: {add, remove}. */
export interface Suggestion {
  id: number;
  issue: number;
  kind: Kind;
  value: Value | { add?: string[]; remove?: string[] };
  reason: string;
  source: string;
  status: 'open' | 'accepted' | 'rejected' | 'withdrawn';
  action: number | null;
}

export interface Move { issue: number; kind: Kind; value: Value | { add?: string[]; remove?: string[] } }

/** What the page's URL says: the per-run token, from the fragment, and the view — what
 *  --from, --by and --group asked for, or what the page last wrote back — from the query
 *  string. The token is in the fragment because that is never sent to a server or left in a
 *  Referer; the view is not a secret, and reads better as ordinary parameters. */
export function parseLocation(search: string, hash: string): { token: string; from: string[]; by: string; group: string[] } {
  const params = new URLSearchParams(search);
  return {
    token: new URLSearchParams(hash.replace(/^#/, '')).get('t') ?? '',
    from: params.getAll('from'),
    by: params.get('by') ?? '',
    group: (params.get('group') ?? '').split(',').filter(Boolean),
  };
}

/** OWNER/NAME from a board's address, `/r/OWNER/NAME/`; empty for any other page. */
export function repoFromPath(pathname: string): string {
  const match = /^\/r\/([^/]+)\/([^/]+)\/?$/.exec(pathname);
  return match ? `${decodeURIComponent(match[1]!)}/${decodeURIComponent(match[2]!)}` : '';
}

/** A link to another of the app's pages, carrying the token on in its fragment: each page
 *  is a load of its own, and the fragment is the only place the token is kept. */
export function pageLink(path: string, token: string): string {
  return `${path}#t=${token}`;
}

/** The address of a repo's board, opened at a card or on some milestones if asked. */
export function boardPath(repo: string, query: { card?: number; from?: string[] } = {}): string {
  const params = new URLSearchParams();
  for (const title of query.from ?? []) params.append('from', title);
  if (query.card) params.set('card', String(query.card));
  const asked = params.toString();
  return `/r/${repo}/${asked ? `?${asked}` : ''}`;
}

/** One repo's board, as the app serves it under /api/v1/repos/OWNER/NAME/. */
export class Api {
  token: string;
  base: string;
  constructor(token: string, repo: string) { this.token = token; this.base = `/api/v1/repos/${repo}`; }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method, headers: { 'X-Milestones-Token': this.token } };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    }
    const response = await fetch(`${this.base}${path}`, init);
    const answer = await response.json().catch(() => ({ error: response.statusText }));
    if (!response.ok) throw new Error(answer.error ?? `HTTP ${response.status}`);
    return answer as T;
  }

  board(): Promise<BoardData> { return this.call('GET', '/board'); }
  /** Read everything again, or with `issues`, only those cards (and the milestones). */
  refresh(issues?: number[]): Promise<BoardData> { return this.call('POST', '/refresh', issues ? { issues } : undefined); }
  async act(moves: Move[]): Promise<Action[]> {
    return (await this.call<{ actions: Action[] }>('POST', '/actions', { actions: moves })).actions;
  }
  async retry(id: number): Promise<Action> {
    return (await this.call<{ action: Action }>('POST', `/actions/${id}/retry`)).action;
  }
  async revert(id: number): Promise<Action> {
    return (await this.call<{ action: Action }>('POST', `/actions/${id}/revert`)).action;
  }
  async ensureMilestone(title: string, reopen = false): Promise<Milestone> {
    return (await this.call<{ milestone: Milestone }>('POST', '/milestones', { title, reopen })).milestone;
  }
  async setDue(number: number, due: string | null): Promise<Milestone> {
    return (await this.call<{ milestone: Milestone }>('PATCH', `/milestones/${number}`, { due })).milestone;
  }
  reviewPlan(): Promise<{ suggestions: Suggestion[]; dropped: number }> { return this.call('POST', '/plan/review'); }
  discardPlan(): Promise<unknown> { return this.call('POST', '/plan/discard'); }
  saveView(view: View): Promise<unknown> { return this.call('POST', '/view', view); }
  resolve(ids: number[], decision: 'accept' | 'reject'): Promise<{ suggestions: Suggestion[]; actions: Action[] }> {
    return this.call('POST', '/suggestions/resolve', { ids, decision });
  }
  /** Server-sent events: every action as it changes state, every milestone as it changes. */
  events(on: { action(a: Action): void; milestone(m: Milestone): void; suggestion(s: Suggestion): void; plan(p: PlanInfo): void; cards(p: CardsPage): void; reload(): void }): EventSource {
    const source = new EventSource(`${this.base}/events?token=${encodeURIComponent(this.token)}`);
    source.addEventListener('action', (e) => on.action(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('milestone', (e) => on.milestone(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('suggestion', (e) => on.suggestion(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('plan', (e) => on.plan(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('cards', (e) => on.cards(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('reload', () => on.reload());
    return source;
  }
}
