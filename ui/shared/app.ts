// The app's own API: what is not about one repo's board. See docs/BoardAPI.md.

export interface AppInfo {
  permissions: 'read' | 'write';
  /** The tracked repos, as the config spells them. */
  repos: string[];
  focus: string[];
  /** Where the config file is. */
  config: string;
  /** The boards opened so far this run. */
  boards: { repo: string; status: string }[];
}

/** An answer that takes seconds to read, handed over from its last reading first. `fresh` is
 *  true only once this run has read it; nothing should be written on the strength of less. */
export interface Section<T> {
  data: T | null;
  fetched_at: string | null;
  params: Record<string, boolean>;
  fresh: boolean;
  refreshing: boolean;
  error: string | null;
}

export interface MilestoneRow {
  repo: string; title: string; number: number; due: string | null;
  open: number; closed: number; open_prs: number; closed_prs: number;
  percent: number | null; flags: string[]; focus: boolean; bucket: boolean; url: string;
}

export type FindingKind = 'stranded' | 'rename' | 'undated' | 'overdue' | 'done' | 'empty';

export interface Finding {
  kind: FindingKind; repo: string; title: string; number: number; detail: string;
  due: string | null; bucket: boolean; url: string; issues: number; prs: number; free_buckets: string[];
}

export interface Overview {
  milestones: MilestoneRow[];
  quiet_repos: { repo: string; focus: boolean; url: string }[];
  stranded: { repo: string; title: string; number: number; open: number; open_prs: number; url: string }[];
  untriaged: { repo: string; issues: number; prs: number; needs_triage: number; focus?: boolean }[];
  /** False while the untriaged counts are still the last ones known. */
  untriaged_fresh: boolean;
  findings: Finding[];
  /** What each kind of finding is, and so what its fix is. */
  kinds: Record<FindingKind, string>;
  buckets: string[];
  repos: string[];
  focus: string[];
  today: string;
}

export interface PullRequest {
  repo: string; number: number; title: string; draft: boolean; updated: string;
  milestone: string | null; url: string; group: 'tracked' | 'untracked' | 'ignored';
}

export interface Rollover { from: string; to: string; items: { number: number; title: string; pr: boolean }[]; moved: boolean; closed: boolean }

export const PR_FLAGS = ['assigned', 'review_requested', 'mentions'] as const;
export type PrFlag = typeof PR_FLAGS[number];

export class AppApi {
  token: string;
  constructor(token: string) { this.token = token; }

  async call<T>(method: string, path: string, body?: unknown): Promise<T> {
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

  info(): Promise<AppInfo> { return this.call('GET', '/app'); }
  overview(): Promise<Section<Overview>> { return this.call('GET', '/overview'); }
  prs(flags: PrFlag[]): Promise<Section<{ prs: PullRequest[] }>> {
    return this.call('GET', `/prs${prsQuery(flags)}`);
  }
  refresh(sections: string[]): Promise<unknown> { return this.call('POST', '/refresh', { sections }); }
  fixMilestone(repo: string, number: number, change: { title?: string; due?: string | null; state?: 'open' | 'closed' }): Promise<unknown> {
    return this.call('PATCH', `/milestones/${repo}/${number}`, change);
  }
  deleteMilestone(repo: string, number: number): Promise<unknown> { return this.call('DELETE', `/milestones/${repo}/${number}`); }
  rollover(repo: string, number: number, to: string, close: boolean, dry: boolean): Promise<Rollover> {
    return this.call('POST', `/milestones/${repo}/${number}/rollover`, { to, close, dry });
  }
  /** Server-sent events: a section, each time it changes. */
  events(on: Record<string, (section: Section<never>) => void>): EventSource {
    const source = new EventSource(`/api/v1/events?token=${encodeURIComponent(this.token)}`);
    for (const [name, take] of Object.entries(on)) {
      source.addEventListener(name, (e) => take(JSON.parse((e as MessageEvent).data)));
    }
    return source;
  }
}

export function prsQuery(flags: PrFlag[]): string {
  const asked = PR_FLAGS.filter((f) => flags.includes(f)).map((f) => `${f}=1`).join('&');
  return asked ? `?${asked}` : '';
}

/** "just now", "5 min ago", "3 h ago", "2 days ago": how old a kept answer is. */
export function ago(then: string, now: Date): string {
  const minutes = Math.floor((now.getTime() - new Date(then).getTime()) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

/** How near a due date is, for its colour: late, within a week, within a month, or further. */
export function dueness(due: string | null, today: string): 'none' | 'late' | 'soon' | 'ahead' | 'distant' {
  if (!due) return 'none';
  const days = Math.round((Date.parse(due) - Date.parse(today)) / 86400000);
  if (days < 0) return 'late';
  return days <= 7 ? 'soon' : days <= 30 ? 'ahead' : 'distant';
}

/** The dates a due-date dialog offers by name, as `check -i` did: today, tomorrow, next
 *  Monday, and the first of next month. */
export function dateChoices(today: string): { label: string; date: string }[] {
  const at = (d: Date) => d.toISOString().slice(0, 10);
  const day = (n: number) => { const d = new Date(`${today}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d; };
  const start = new Date(`${today}T12:00:00Z`);
  const weekday = (start.getUTCDay() + 6) % 7;   // Monday is 0
  const month = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1, 12));
  return [
    { label: 'today', date: today },
    { label: 'tomorrow', date: at(day(1)) },
    { label: 'next Monday', date: at(day(7 - weekday)) },
    { label: 'start of next month', date: at(month) },
  ];
}
