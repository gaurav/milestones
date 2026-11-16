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

export interface Action {
  id: number;
  issue: number;
  kind: 'milestone';
  value: number | null;
  previous: number | null;
  carriedBy: number | null;
  error: string | null;
  status: ActionStatus;
  /** The number of the last event about it: of two copies, the higher is the newer. */
  rev: number;
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
  /** The last event number this snapshot includes. */
  rev: number;
}

/** `carryParked` moves the parked (Needs triage) issues a pull request closes along with it;
 *  without it they are `held`, as `assign` holds them when no person is there to confirm.
 *  `carry: false` carries nothing: an undo puts back exactly what it names. */
export interface Move { issue: number; kind: 'milestone'; value: number | null; carryParked?: boolean; carry?: boolean }

/** What the event stream calls back with. `rev` is the event's number (its SSE id). `open`
 *  comes on the first connection and every reconnection, `error` whenever it drops. */
export interface Handlers {
  open(): void;
  error(): void;
  action(a: Action, rev: number): void;
  card(c: Card, rev: number): void;
  milestone(m: Milestone, rev: number): void;
  reload(): void;
}

/** A parked issue a pull request closes, left where it was. */
export interface Held { issue: number; carriedBy: number }

/** The per-run token and the columns asked for with --from, from the URL fragment. */
export function parseFragment(hash: string): { token: string; from: string[] } {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  return { token: params.get('t') ?? '', from: params.getAll('from') };
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
  act(moves: Move[]): Promise<{ actions: Action[]; held: Held[] }> {
    return this.call('POST', '/actions', { actions: moves });
  }
  async retry(id: number): Promise<Action> {
    return (await this.call<{ action: Action }>('POST', `/actions/${id}/retry`)).action;
  }
  /** The card comes back too: with no change left standing on it, nothing in the action
   *  says what GitHub has. */
  revert(id: number): Promise<{ action: Action; card: Card }> {
    return this.call('POST', `/actions/${id}/revert`);
  }
  async ensureMilestone(title: string, reopen = false): Promise<Milestone> {
    return (await this.call<{ milestone: Milestone }>('POST', '/milestones', { title, reopen })).milestone;
  }
  async setDue(number: number, due: string | null): Promise<Milestone> {
    return (await this.call<{ milestone: Milestone }>('PATCH', `/milestones/${number}`, { due })).milestone;
  }
  /** Server-sent events: every action as it changes state, every milestone as it changes, a
   *  card a revert put back. Nothing is replayed after a reconnection: `open` is the cue to
   *  fetch the board again. */
  events(on: Handlers): EventSource {
    const source = new EventSource(`/api/v1/events?token=${encodeURIComponent(this.token)}`);
    const read = (e: Event) => [JSON.parse((e as MessageEvent).data), Number((e as MessageEvent).lastEventId)] as const;
    source.addEventListener('open', () => on.open());
    source.addEventListener('error', () => on.error());
    source.addEventListener('action', (e) => on.action(...read(e)));
    source.addEventListener('card', (e) => on.card(...read(e)));
    source.addEventListener('milestone', (e) => on.milestone(...read(e)));
    source.addEventListener('reload', () => on.reload());
    return source;
  }
}
