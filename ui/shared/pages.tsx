// What the pages that are not a board share: the token, the API, a section's signal kept up
// to date from the event stream, and the bar across the top.
import { signal } from '@preact/signals';
import type { Signal } from '@preact/signals';
import type { ComponentChildren } from 'preact';
import { AppApi, ago } from './app.ts';
import type { AppInfo, Section } from './app.ts';
import { pageLink, parseLocation } from './api.ts';

export const { token } = parseLocation(location.search, location.hash);
export const api = new AppApi(token);
export const info = signal<AppInfo | null>(null);
export const fatal = signal('');
export const toast = signal('');
/** Ticks once a minute, so "3 min ago" stays true while the page sits open. */
const clock = signal(new Date());
setInterval(() => { clock.value = new Date(); }, 60000);

export function say(message: string) {
  toast.value = message;
  setTimeout(() => { if (toast.value === message) toast.value = ''; }, 8000);
}

export const link = (path: string) => pageLink(path, token);

const EMPTY = { data: null, fetched_at: null, params: {}, fresh: false, refreshing: true, error: null };

/** Load a section and follow it: the kept answer at once, each new one as it is read. */
export function follow<T>(name: string, load: () => Promise<Section<T>>): Signal<Section<T>> {
  const section = signal<Section<T>>(EMPTY);
  const failed = (error: Error) => {
    fatal.value = `Could not reach milestones: ${error.message}. If the token is wrong: the link carries one that is new each time \`milestones\` starts; open the one it printed.`;
  };
  load().then((got) => { section.value = got; }).catch(failed);
  api.info().then((got) => { info.value = got; }).catch(() => {});
  api.events({ [name]: (got) => { section.value = got as Section<T>; } });
  return section;
}

/** The bar across the top of a page: where you are, how old what you are looking at is, and
 *  a way to read it again. */
export function Top({ page, section, onRefresh, children }: {
  page: 'home' | 'prs'; section: Section<unknown>; onRefresh: () => void; children?: ComponentChildren;
}) {
  const { fetched_at: at, refreshing, fresh, error } = section;
  const age = at ? `as of ${ago(at, clock.value)}` : 'never read';
  return (
    <header class="top">
      <h1>milestones</h1>
      <nav>
        <a class={page === 'home' ? 'here' : ''} href={link('/')}>Milestones</a>
        <a class={page === 'prs' ? 'here' : ''} href={link('/prs/')}>My pull requests</a>
      </nav>
      {info.value?.permissions === 'read' && <span class="chip read-only" title="MILESTONES_PERMISSIONS=read: nothing is written to GitHub. A fix says what it would have done.">read-only</span>}
      {children}
      <span class="age" title={at ? new Date(at).toLocaleString() : ''}>
        {error ? <span class="bad" title={error}>could not read GitHub · {at ? `showing ${age}` : 'nothing kept'}</span>
          : refreshing ? `${at && !fresh ? `${age} · ` : ''}reading GitHub…` : age}
        <button class="refresh" disabled={refreshing} onClick={onRefresh} title="Read it from GitHub again">Refresh</button>
      </span>
    </header>
  );
}

export function Toast() {
  return toast.value ? <div class="toast" onClick={() => { toast.value = ''; }}>{toast.value}</div> : null;
}
