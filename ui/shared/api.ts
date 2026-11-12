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

export type Kind = 'milestone' | 'priority' | 'effort' | 'labels' | 'title' | 'state' | 'comment';
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

export interface BoardData {
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
  labelValues?: Record<string, { field: string; prefix: string; values: Record<string, string> }>;
}

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

/** The per-run token, and what --from, --by and --group asked for, from the URL fragment. */
export function parseFragment(hash: string): { token: string; from: string[]; by: string; group: string[] } {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  return {
    token: params.get('t') ?? '',
    from: params.getAll('from'),
    by: params.get('by') ?? '',
    group: (params.get('group') ?? '').split(',').filter(Boolean),
  };
}

export class Api {
  token: string;
  constructor(token: string) { this.token = token; }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method, headers: { 'X-Milestones-Token': this.token } };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    }
    const response = await fetch(`/api/v1${path}`, init);
    const answer = await response.json().catch(() => ({ error: response.statusText }));
    if (!response.ok) throw new Error(answer.error ?? `HTTP ${response.status}`);
    return answer as T;
  }

  board(): Promise<BoardData> { return this.call('GET', '/board'); }
  refresh(): Promise<BoardData> { return this.call('POST', '/refresh'); }
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
  resolve(ids: number[], decision: 'accept' | 'reject'): Promise<{ suggestions: Suggestion[]; actions: Action[] }> {
    return this.call('POST', '/suggestions/resolve', { ids, decision });
  }
  /** Server-sent events: every action as it changes state, every milestone as it changes. */
  events(on: { action(a: Action): void; milestone(m: Milestone): void; suggestion(s: Suggestion): void; reload(): void }): EventSource {
    const source = new EventSource(`/api/v1/events?token=${encodeURIComponent(this.token)}`);
    source.addEventListener('action', (e) => on.action(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('milestone', (e) => on.milestone(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('suggestion', (e) => on.suggestion(JSON.parse((e as MessageEvent).data)));
    source.addEventListener('reload', () => on.reload());
    return source;
  }
}
