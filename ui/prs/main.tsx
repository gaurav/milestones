// Every open pull request of yours, anywhere on GitHub: the ones in tracked repos that are on
// no milestone yet, then the rest by whether the repo is tracked, untracked or ignored. What
// `prs` printed; each row opens its repo's board at that pull request, tracked or not.
import { render } from 'preact';
import { computed, signal } from '@preact/signals';
import { boardPath } from '../shared/api.ts';
import { PR_FLAGS, prsQuery } from '../shared/app.ts';
import type { PrFlag, PullRequest } from '../shared/app.ts';
import { SearchBox } from '../shared/search.tsx';
import { Toast, Top, api, fatal, follow, link, say } from '../shared/pages.tsx';

const LABELS: Record<PrFlag, string> = { assigned: 'assigned', review_requested: 'to review', mentions: 'mentions' };
const HINTS: Record<PrFlag, string> = { assigned: 'Also the ones assigned to you', review_requested: 'Also the ones waiting on your review', mentions: 'Also the ones that mention you' };

/** Whose pull requests besides the ones you wrote. In the address, so a reload asks the same. */
const flags = signal<PrFlag[]>(PR_FLAGS.filter((f) => new URLSearchParams(location.search).get(f) === '1'));
const section = follow<{ prs: PullRequest[] }>('prs', () => api.prs(flags.value));
const search = signal('');

function toggle(flag: PrFlag) {
  flags.value = flags.value.includes(flag) ? flags.value.filter((f) => f !== flag) : [...flags.value, flag];
  try { history.replaceState(null, '', `${location.pathname}${prsQuery(flags.value)}${location.hash}`); } catch { /* the next change will catch up */ }
  // The answer to a different question: the server drops the one it kept and reads again.
  api.prs(flags.value).then((got) => { section.value = got; }).catch((e: Error) => say(e.message));
}

const shown = computed(() => {
  const q = search.value.trim().toLowerCase();
  const all = section.value.data?.prs ?? [];
  // By repo, freshest first within it: the question is per repo, not per pull request.
  return all.filter((p) => !q || p.repo.toLowerCase().includes(q) || p.title.toLowerCase().includes(q))
    .sort((a, b) => a.repo.toLowerCase().localeCompare(b.repo.toLowerCase()) || b.updated.localeCompare(a.updated));
});

function Table({ rows, milestone }: { rows: PullRequest[]; milestone: boolean }) {
  return (
    <table class="prs">
      <colgroup><col class="repo" /><col class="number" /><col />{milestone && <col class="milestone" />}<col class="when" /><col class="go" /></colgroup>
      <thead><tr><th>Repo</th><th>PR</th><th>Title</th>{milestone && <th>Milestone</th>}<th>Updated</th><th /></tr></thead>
      <tbody>
        {rows.map((p) => {
          const [owner, name] = p.repo.split('/');
          return (
            <tr key={`${p.repo}#${p.number}`}>
              <td><a class="repo" href={link(boardPath(p.repo))} title={`Open ${p.repo}'s board`}><span class="owner">{owner}/</span>{name}</a></td>
              <td class="number"><a href={p.url} target="_blank" rel="noreferrer" title="This pull request on GitHub">#{p.number} ↗</a></td>
              <td class="title"><a href={link(boardPath(p.repo, { card: p.number }))} title="Open the board at this pull request">{p.title}</a>{p.draft && <span class="chip draft">draft</span>}</td>
              {milestone && <td>{p.milestone ?? <span class="chip none">no milestone</span>}</td>}
              <td class="when">{p.updated.slice(0, 10)}</td>
              <td><a href={link(boardPath(p.repo, { card: p.number }))}>board</a></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Prs() {
  if (fatal.value) return <p class="fatal">{fatal.value}</p>;
  const data = section.value.data;
  const refresh = () => { api.refresh(['prs']).catch((e: Error) => say(e.message)); };
  const of = (group: PullRequest['group']) => shown.value.filter((p) => p.group === group);
  const tracked = of('tracked');
  const todo = tracked.filter((p) => !p.milestone);
  const placed = tracked.filter((p) => p.milestone);
  return (
    <>
      <Top page="prs" section={section.value} onRefresh={refresh}>
        <SearchBox placeholder="Filter by repo or title" value={search.value} onInput={(v) => { search.value = v; }} />
        {PR_FLAGS.map((flag) => (
          <label key={flag} class="check" title={HINTS[flag]}><input type="checkbox" checked={flags.value.includes(flag)} onChange={() => toggle(flag)} /> {LABELS[flag]}</label>
        ))}
      </Top>
      {!data ? (
        <p class="fatal">{section.value.error ?? 'Reading your open pull requests from GitHub…'}</p>
      ) : data.prs.length === 0 ? (
        <p class="fatal">No open pull requests.</p>
      ) : (
        <main>
          <section>
            <h2>In tracked repos, on no milestone <small>{todo.length}</small></h2>
            {todo.length ? <Table rows={todo} milestone={false} /> : <p>{tracked.length ? `Every pull request in a tracked repo is on a milestone (${placed.length}).` : 'None in a tracked repo.'}</p>}
          </section>
          {placed.length > 0 && <section><h2>In tracked repos, on a milestone <small>{placed.length}</small></h2><Table rows={placed} milestone /></section>}
          {of('untracked').length > 0 && (
            <section>
              <h2>In repos that are not tracked <small>{of('untracked').length}</small></h2>
              <p><code>milestones add OWNER/NAME</code> tracks a repo; its board opens either way.</p>
              <Table rows={of('untracked')} milestone />
            </section>
          )}
          {of('ignored').length > 0 && <section><h2>In ignored repos <small>{of('ignored').length}</small></h2><Table rows={of('ignored')} milestone /></section>}
        </main>
      )}
      <Toast />
    </>
  );
}

render(<Prs />, document.getElementById('app')!);
