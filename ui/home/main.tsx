// The app's home page: where `milestones` opens. For now, the tracked repos and a way into
// each one's board.
import { render } from 'preact';
import { useEffect } from 'preact/hooks';
import { signal } from '@preact/signals';
import { AppApi } from '../shared/app.ts';
import type { AppInfo } from '../shared/app.ts';
import { boardPath, pageLink, parseLocation } from '../shared/api.ts';

const { token } = parseLocation(location.search, location.hash);
const api = new AppApi(token);
const info = signal<AppInfo | null>(null);
const fatal = signal('');

function Home() {
  useEffect(() => {
    api.info().then((got) => { info.value = got; })
      .catch((error: Error) => { fatal.value = `Could not reach milestones: ${error.message}. The link carries a token that is new each time \`milestones\` starts; open the one it printed.`; });
  }, []);
  if (fatal.value) return <p class="fatal">{fatal.value}</p>;
  const app = info.value;
  if (!app) return <p class="fatal">Reading…</p>;
  return (
    <>
      <header class="top">
        <h1>milestones</h1>
        {app.permissions === 'read' && <span class="chip read-only" title="MILESTONES_PERMISSIONS=read: nothing is written to GitHub">read-only</span>}
      </header>
      {app.repos.length === 0
        ? <p>No repos are tracked yet. <code>milestones add OWNER/NAME</code> tracks one (the config is <code>{app.config}</code>); <code>milestones OWNER/NAME</code> opens any repo's board without tracking it.</p>
        : <ul class="repos">{app.repos.map((repo) => <li key={repo}><a href={pageLink(boardPath(repo), token)}>{repo}</a></li>)}</ul>}
    </>
  );
}

render(<Home />, document.getElementById('app')!);
