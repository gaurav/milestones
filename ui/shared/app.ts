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
}
