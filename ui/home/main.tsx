// The app's home page: every tracked repo's milestones by due date, what is wrong with any
// of them and the fix for it, and how much in each repo is on no milestone. What `status` and
// `check` printed, with `check -i`'s fixes as buttons.
import { render } from 'preact';
import { useState } from 'preact/hooks';
import { computed, signal } from '@preact/signals';
import { boardPath } from '../shared/api.ts';
import { dateChoices, dueness } from '../shared/app.ts';
import type { Finding, FindingKind, MilestoneRow, Overview, Rollover } from '../shared/app.ts';
import { SearchBox } from '../shared/search.tsx';
import { Toast, Top, api, fatal, follow, info, link, say } from '../shared/pages.tsx';

const section = follow<Overview>('overview', () => api.overview());
const overview = computed(() => section.value.data);
/** Whether a fix may be offered: only on an answer this run read itself, never a kept one. */
const fixable = computed(() => section.value.fresh && !section.value.refreshing);

function kept(key: string, otherwise: boolean): boolean {
  try { return (localStorage.getItem(key) ?? String(otherwise)) === 'true'; } catch { return otherwise; }
}
function keep(key: string, value: boolean) { try { localStorage.setItem(key, String(value)); } catch { /* a convenience only */ } }
const search = signal('');
const focusOnly = signal(kept('milestones.focusOnly', false));
const showBuckets = signal(kept('milestones.showBuckets', true));

type Dialog = { kind: 'rename' | 'date' | 'rollover' | 'delete'; finding: Finding };
const dialog = signal<Dialog | null>(null);
const focusOnMount = (el: HTMLElement | null) => { el?.focus(); };

const matches = (repo: string, title = '') => {
  const q = search.value.trim().toLowerCase();
  return !q || repo.toLowerCase().includes(q) || title.toLowerCase().includes(q);
};
const wanted = (row: { repo: string; focus?: boolean }, title = '') =>
  matches(row.repo, title) && (!focusOnly.value || Boolean(row.focus));

/** Make a change and say how it went. The server reads the overview again after a write, so
 *  the row redraws itself; a read-only app answers with what it would have done. */
async function fix(what: string, call: () => Promise<unknown>) {
  dialog.value = null;
  try {
    await call();
    say(what);
  } catch (error) {
    say((error as Error).message);
  }
}

function Repo({ repo, focus }: { repo: string; focus?: boolean | undefined }) {
  const [owner, name] = repo.split('/');
  return (
    <a class="repo" href={link(boardPath(repo))} title={`Open ${repo}'s board`}>
      {focus && <span class="star" title="A repo you are focusing on">✦</span>}<span class="owner">{owner}/</span>{name}
    </a>
  );
}

function Due({ due, today }: { due: string | null; today: string }) {
  return due ? <span class={`due ${dueness(due, today)}`}>{due}</span> : <span class="muted">—</span>;
}

function Milestones({ data }: { data: Overview }) {
  const rows = data.milestones.filter((m) => wanted(m, m.title) && (showBuckets.value || !m.bucket));
  const work = (m: MilestoneRow) => m.open + m.open_prs;
  return (
    <section>
      <h2>Milestones <small>{rows.length} of {data.milestones.length} open</small></h2>
      {rows.length === 0 ? <p class="muted">{data.milestones.length ? 'None match.' : 'No open milestones in any tracked repo.'}</p> : (
        <table class="milestones">
          <thead><tr><th>Repo</th><th>Milestone</th><th>Due</th><th class="n">Open</th><th class="n">Done</th><th class="n">PRs</th><th class="n">%</th><th /></tr></thead>
          <tbody>
            {rows.map((m) => (
              <tr key={`${m.repo}#${m.number}`} class={m.bucket ? 'bucket' : ''}>
                <td><Repo repo={m.repo} focus={m.focus} /></td>
                <td>
                  <a class={`title${m.bucket && work(m) ? ` bucket-${m.title.toLowerCase().replace(/\W+/g, '-')}` : ''}`} href={link(boardPath(m.repo, { from: [m.title] }))} title="Open the board on this milestone">{m.title}</a>
                  {m.flags.map((flag) => <span key={flag} class={`chip flag ${flag}`}>{flag}</span>)}
                </td>
                <td><Due due={m.due} today={data.today} /></td>
                <td class="n">{m.open}</td>
                <td class="n">{m.closed}</td>
                <td class="n">{m.open_prs || ''}</td>
                <td class="n">{m.percent === null ? <span class="muted">—</span> : <span class={`pct${m.percent >= 80 ? ' near' : ''}`}>{m.percent}%</span>}</td>
                <td><a class="out" href={m.url} target="_blank" rel="noreferrer" title="This milestone on GitHub">↗</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** The fixes a finding's kind offers: the keys `check -i` had, as buttons. */
function Fixes({ f }: { f: Finding }) {
  const off = !fixable.value;
  const title = off ? 'Waiting for a fresh read from GitHub: nothing is fixed from a kept copy' : undefined;
  const open = (kind: Dialog['kind']) => () => { dialog.value = { kind, finding: f }; };
  const patch = (what: string, change: Parameters<typeof api.fixMilestone>[2]) => () => fix(what, () => api.fixMilestone(f.repo, f.number, change));
  const buttons = [];
  if (f.kind === 'rename') {
    // Only the buckets this repo hasn't got: a rename onto one it has would collide.
    for (const bucket of f.free_buckets) buttons.push(<button key={bucket} disabled={off} title={title} onClick={patch(`Renamed '${f.title}' to '${bucket}' in ${f.repo}.`, { title: bucket })}>→ {bucket}</button>);
    buttons.push(<button key="rename" disabled={off} title={title} onClick={open('rename')}>Rename…</button>);
  }
  if (f.kind === 'undated' || f.kind === 'overdue') buttons.push(<button key="date" disabled={off} title={title} onClick={open('date')}>Set a date…</button>);
  if (f.kind === 'overdue' || f.kind === 'stranded') buttons.push(<button key="roll" disabled={off} title={title} onClick={open('rollover')}>Roll over…</button>);
  // Reopening is the one state change that can't hide anything, so a bucket gets it too.
  if (f.kind === 'stranded') buttons.push(<button key="reopen" disabled={off} title={title} onClick={patch(`Reopened '${f.title}' in ${f.repo}.`, { state: 'open' })}>Reopen</button>);
  if (f.kind === 'done') buttons.push(<button key="close" disabled={off} title={title} onClick={patch(`Closed '${f.title}' in ${f.repo}.`, { state: 'closed' })}>Close</button>);
  if (f.kind === 'empty') buttons.push(<button key="delete" disabled={off} title={title} onClick={open('delete')}>Delete…</button>);
  return <span class="fixes">{buttons}</span>;
}

function Findings({ data }: { data: Overview }) {
  const shown = data.findings.filter((f) => matches(f.repo, f.title) && (!focusOnly.value || data.focus.some((r) => r.toLowerCase() === f.repo.toLowerCase())));
  if (data.findings.length === 0) return <section><h2>To fix</h2><p class="muted">Nothing: every milestone is named, dated and closed sensibly.</p></section>;
  const kinds = Object.keys(data.kinds) as FindingKind[];
  return (
    <section>
      <h2>To fix <small>{shown.length} of {data.findings.length}{fixable.value ? '' : ' · fixes wait for a fresh read'}</small></h2>
      {kinds.map((kind) => {
        const group = shown.filter((f) => f.kind === kind);
        if (group.length === 0) return null;
        return (
          <div key={kind} class="finding-group">
            <h3><span class={`chip flag ${kind}`}>{kind}</span> {data.kinds[kind]} <small>{group.length}</small></h3>
            <table class="findings">
              <tbody>
                {group.map((f) => (
                  <tr key={`${f.repo}#${f.number}`}>
                    <td><Repo repo={f.repo} /></td>
                    <td><a class="title" href={link(boardPath(f.repo, { from: [f.title] }))}>{f.title}</a> <a class="out" href={f.url} target="_blank" rel="noreferrer" title="This milestone on GitHub">↗</a></td>
                    <td class="muted">{f.issues} issue{f.issues === 1 ? '' : 's'}{f.prs ? `, ${f.prs} PR${f.prs === 1 ? '' : 's'}` : ''}{f.detail ? ` · ${f.detail}` : ''}</td>
                    <td><Fixes f={f} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </section>
  );
}

function Untriaged({ data }: { data: Overview }) {
  const rows = data.untriaged.filter((r) => wanted(r));
  const stale = !data.untriaged_fresh;
  const sum = (key: 'issues' | 'prs' | 'needs_triage') => data.untriaged.reduce((n, r) => n + r[key], 0);
  return (
    <section>
      <h2>To triage <small>{sum('issues')} issues and {sum('prs')} pull requests on no milestone{sum('needs_triage') ? `, ${sum('needs_triage')} on Needs triage` : ''}{stale ? ' · last known, reading…' : ''}</small></h2>
      {data.untriaged.length === 0 ? <p class="muted">{stale ? 'Reading…' : 'Everything open in every tracked repo is on a milestone.'}</p> : (
        <table class={`untriaged${stale ? ' stale' : ''}`}>
          <thead><tr><th>Repo</th><th class="n">Issues</th><th class="n">PRs</th><th class="n">Needs triage</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.repo}>
                <td><Repo repo={r.repo} focus={r.focus} /></td>
                <td class="n">{r.issues || ''}</td><td class="n">{r.prs || ''}</td><td class="n">{r.needs_triage || ''}</td>
                <td><a href={link(boardPath(r.repo, { from: ['none', ...(r.needs_triage ? ['Needs triage'] : [])] }))}>triage on the board</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Quiet({ data }: { data: Overview }) {
  const rows = data.quiet_repos.filter((r) => wanted(r));
  if (rows.length === 0) return null;
  return (
    <section>
      <h2>No open milestones <small>{rows.length} tracked repo{rows.length === 1 ? '' : 's'}</small></h2>
      <ul class="repos">{rows.map((r) => <li key={r.repo}><Repo repo={r.repo} focus={r.focus} /></li>)}</ul>
    </section>
  );
}

function Frame({ head, children, onClose }: { head: string; children: preact.ComponentChildren; onClose: () => void }) {
  return (
    <div class="overlay" onPointerDown={(e) => { if (e.target === e.currentTarget) onClose(); }} onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}>
      <div class="picker">
        <div class="picker-head">{head}</div>
        {children}
      </div>
    </div>
  );
}

function RenameDialog({ f }: { f: Finding }) {
  const [text, setText] = useState(f.title);
  const close = () => { dialog.value = null; };
  const go = () => { const title = text.trim(); if (title && title !== f.title) fix(`Renamed '${f.title}' to '${title}' in ${f.repo}.`, () => api.fixMilestone(f.repo, f.number, { title })); else close(); };
  return (
    <Frame head={`Rename '${f.title}' in ${f.repo}: a version or a date in the title says when it is for`} onClose={close}>
      <input ref={focusOnMount} value={text} onInput={(e) => setText((e.target as HTMLInputElement).value)} onKeyDown={(e) => { if (e.key === 'Enter') go(); }} />
      <div class="picker-foot"><button onClick={close}>Cancel</button><button class="primary" onClick={go}>Rename</button></div>
    </Frame>
  );
}

function DateDialog({ f, today }: { f: Finding; today: string }) {
  const [day, setDay] = useState(f.due ?? '');
  const close = () => { dialog.value = null; };
  const go = (date: string) => { if (date) fix(`'${f.title}' in ${f.repo} is now due ${date}.`, () => api.fixMilestone(f.repo, f.number, { due: date })); };
  return (
    <Frame head={`Due date for '${f.title}' in ${f.repo}`} onClose={close}>
      <div class="quick">{dateChoices(today).map((c) => <button key={c.label} onClick={() => go(c.date)}>{c.label} <small>{c.date}</small></button>)}</div>
      <input ref={focusOnMount} type="date" value={day} onInput={(e) => setDay((e.target as HTMLInputElement).value)} onKeyDown={(e) => { if (e.key === 'Enter') go(day); }} />
      <div class="picker-foot"><button onClick={close}>Cancel</button><button class="primary" disabled={!day} onClick={() => go(day)}>Set</button></div>
    </Frame>
  );
}

function DeleteDialog({ f }: { f: Finding }) {
  const close = () => { dialog.value = null; };
  return (
    <Frame head={`Delete '${f.title}' from ${f.repo}?`} onClose={close}>
      <p>Nothing was ever filed on it. A deleted milestone can't be brought back.</p>
      <div class="picker-foot"><button ref={focusOnMount} onClick={close}>Cancel</button><button class="primary danger" onClick={() => fix(`Deleted '${f.title}' from ${f.repo}.`, () => api.deleteMilestone(f.repo, f.number))}>Delete</button></div>
    </Frame>
  );
}

/** Roll a milestone's open work onto another: name it, see what would move, then move it. */
function RolloverDialog({ f, data }: { f: Finding; data: Overview }) {
  const [to, setTo] = useState('');
  const [closeIt, setCloseIt] = useState(false);
  const [plan, setPlan] = useState<Rollover | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const close = () => { dialog.value = null; };
  const targets = data.milestones.filter((m) => m.repo === f.repo && m.title !== f.title).map((m) => m.title);
  const look = async () => {
    setError(''); setBusy(true);
    try { setPlan(await api.rollover(f.repo, f.number, to, closeIt, true)); } catch (e) { setError((e as Error).message); }
    setBusy(false);
  };
  const move = () => fix(`Moved ${plan!.items.length} from '${f.title}' to '${plan!.to}' in ${f.repo}${closeIt ? ', and closed it' : ''}.`, () => api.rollover(f.repo, f.number, to, closeIt, false));
  return (
    <Frame head={`Roll the open work on '${f.title}' in ${f.repo} onto another milestone`} onClose={close}>
      <input ref={focusOnMount} list="rollover-targets" placeholder="An open milestone's title" value={to} disabled={plan !== null}
        onInput={(e) => setTo((e.target as HTMLInputElement).value)} onKeyDown={(e) => { if (e.key === 'Enter' && to.trim()) look(); }} />
      <datalist id="rollover-targets">{targets.map((t) => <option key={t} value={t} />)}</datalist>
      {/* A standing bucket outlives what is on it, and a closed milestone has nothing left to close. */}
      {!f.bucket && f.kind !== 'stranded' && <label class="check"><input type="checkbox" checked={closeIt} disabled={plan !== null} onChange={(e) => setCloseIt((e.target as HTMLInputElement).checked)} /> then close '{f.title}'</label>}
      {error && <p class="bad">{error}</p>}
      {plan && (plan.items.length
        ? <ul class="moving">{plan.items.map((i) => <li key={i.number}>#{i.number} {i.title}{i.pr ? ' (PR)' : ''}</li>)}</ul>
        : <p class="muted">Nothing is open on '{f.title}'.</p>)}
      <div class="picker-foot">
        <button onClick={close}>Cancel</button>
        {plan === null
          ? <button class="primary" disabled={!to.trim() || busy} onClick={look}>{busy ? 'Reading…' : 'See what would move'}</button>
          : <button class="primary" disabled={!plan.items.length && !closeIt} onClick={move}>Move {plan.items.length} to '{plan.to}'</button>}
      </div>
    </Frame>
  );
}

function Home() {
  if (fatal.value) return <p class="fatal">{fatal.value}</p>;
  const data = overview.value;
  const refresh = () => { api.refresh(['overview']).catch((e: Error) => say(e.message)); };
  const open = dialog.value;
  return (
    <>
      <Top page="home" section={section.value} onRefresh={refresh}>
        <SearchBox placeholder="Filter by repo or milestone" value={search.value} onInput={(v) => { search.value = v; }} />
        <label class="check"><input type="checkbox" checked={focusOnly.value} onChange={(e) => { focusOnly.value = (e.target as HTMLInputElement).checked; keep('milestones.focusOnly', focusOnly.value); }} /> focused repos</label>
        <label class="check"><input type="checkbox" checked={showBuckets.value} onChange={(e) => { showBuckets.value = (e.target as HTMLInputElement).checked; keep('milestones.showBuckets', showBuckets.value); }} /> buckets</label>
      </Top>
      {!data ? (
        <p class="fatal">{section.value.error ?? 'Reading every tracked repo’s milestones from GitHub…'}</p>
      ) : data.repos.length === 0 ? (
        <p class="fatal">No repos are tracked yet. <code>milestones add OWNER/NAME</code> tracks one{info.value ? <> (the config is <code>{info.value.config}</code>)</> : null}; <code>milestones OWNER/NAME</code> opens any repo's board without tracking it.</p>
      ) : (
        <main>
          <Milestones data={data} />
          <Untriaged data={data} />
          <Findings data={data} />
          <Quiet data={data} />
        </main>
      )}
      {data && open?.kind === 'rename' && <RenameDialog f={open.finding} />}
      {data && open?.kind === 'date' && <DateDialog f={open.finding} today={data.today} />}
      {data && open?.kind === 'delete' && <DeleteDialog f={open.finding} />}
      {data && open?.kind === 'rollover' && <RolloverDialog f={open.finding} data={data} />}
      <Toast />
    </>
  );
}

render(<Home />, document.getElementById('app')!);
