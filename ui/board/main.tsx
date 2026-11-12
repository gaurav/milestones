// The column view: one column per milestone, cards you move by key, by drag or by picker.
// Everything it knows about GitHub comes through ui/shared/.
import { render } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import type { Action, Card as CardData } from '../shared/api.ts';
import {
  NONE, age, columnOf, effortOf, inkOn, latestAction, linkedPrs, noFilters, pickerMatches, priorityOf,
} from '../shared/logic.ts';
import type { ColumnId } from '../shared/logic.ts';
import {
  actions, board, cards, columnTitle, columns, fatal, filters, focused, grouped, loading, milestones,
  move, moveToTitle, pending, picking, readOnly, refresh, retry, revert, selected, setDue, shown, start,
  step, targets, toast, toggleColumn, toggleSelected, undo, visibleColumns,
} from '../shared/state.ts';

const DRAG = 'application/x-milestones-cards';

function Status({ action }: { action: Action | undefined }) {
  if (!action) return null;
  if (action.status === 'pending') return <span class="status saving" title="Writing to GitHub…">saving…</span>;
  if (action.status === 'would') return <span class="status would" title="Read-only: not written to GitHub">not written</span>;
  if (action.status === 'failed') {
    return (
      <span class="status failed" title={action.error ?? ''}>
        not saved
        <button onClick={(e) => { e.stopPropagation(); retry(action.id); }}>Retry</button>
        <button onClick={(e) => { e.stopPropagation(); revert(action.id); }}>Revert</button>
      </span>
    );
  }
  return null;
}

function Card({ card }: { card: CardData }) {
  const ref = useRef<HTMLDivElement>(null);
  const isFocused = focused.value === card.number;
  const isSelected = selected.value.has(card.number);
  useEffect(() => { if (isFocused) ref.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }, [isFocused]);
  const priority = priorityOf(card);
  const effort = effortOf(card);
  const prs = linkedPrs(card);
  return (
    <div
      ref={ref}
      class={`card${isFocused ? ' focused' : ''}${isSelected ? ' selected' : ''}`}
      draggable
      onClick={(e) => {
        if (e.shiftKey || e.metaKey || e.ctrlKey) toggleSelected(card.number);
        focused.value = card.number;
      }}
      onDragStart={(e) => {
        e.dataTransfer!.setData(DRAG, JSON.stringify(targets(card.number)));
        e.dataTransfer!.effectAllowed = 'move';
      }}
    >
      <div class="card-title">
        <a href={card.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>#{card.number}</a>{' '}
        {card.kind === 'pr' && <span class="chip pr">{card.draft ? 'draft PR' : 'PR'}</span>}
        {card.title}
      </div>
      <div class="card-meta">
        {priority && <span class={`chip priority p-${priority.toLowerCase()}`}>{priority}</span>}
        {effort && <span class="chip effort">effort {effort}</span>}
        {card.type && <span class="chip type">{card.type}</span>}
        {card.labels.filter((l) => !/^(priority|size):/i.test(l.name)).map((l) => (
          <span class="label" style={{ background: `#${l.color}`, color: inkOn(l.color) }}>{l.name}</span>
        ))}
      </div>
      <div class="card-foot">
        <span title={`updated ${card.updated.slice(0, 10)}, opened ${card.created.slice(0, 10)}`}>{age(card.updated)}</span>
        {card.comments > 0 && <span title="comments">💬 {card.comments}</span>}
        {prs.length > 0 && <span title={prs.map((p) => `#${p.number} ${p.title}`).join('\n')}>⇄ {prs.length} PR{prs.length > 1 ? 's' : ''}</span>}
        {(card.closes?.length ?? 0) > 0 && <span title="issues this pull request closes">closes {card.closes!.map((c) => `#${c.number}`).join(' ')}</span>}
        {(card.subIssues?.total ?? 0) > 0 && <span title="sub-issues done">☑ {card.subIssues!.completed}/{card.subIssues!.total}</span>}
        {card.assignees.length > 0 && <span title="assignees">@{card.assignees.join(' @')}</span>}
        <Status action={latestAction(actions.value.values(), card.number)} />
      </div>
    </div>
  );
}

function DueDate({ id }: { id: ColumnId }) {
  const [editing, setEditing] = useState(false);
  const milestone = milestones.value.get(id);
  if (!milestone) return null;
  if (editing) {
    return (
      <input
        type="date"
        class="due-input"
        value={milestone.due ?? ''}
        autoFocus
        onBlur={() => setEditing(false)}
        onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false); e.stopPropagation(); }}
        onChange={(e) => { setDue(id, (e.target as HTMLInputElement).value || null); setEditing(false); }}
      />
    );
  }
  const overdue = milestone.due !== null && milestone.due < new Date().toISOString().slice(0, 10);
  return (
    <button class={`due${overdue ? ' overdue' : ''}`} title="Change the due date" onClick={() => setEditing(true)}>
      {milestone.due ? `due ${milestone.due}` : 'no date'}
    </button>
  );
}

function Column({ id, index }: { id: ColumnId; index: number }) {
  const [over, setOver] = useState(false);
  const list = grouped.value.get(id) ?? [];
  const milestone = milestones.value.get(id);
  const closed = milestone?.state === 'closed';
  const total = [...cards.value.values()].filter((c) => columnOf(c) === id).length;
  return (
    <section
      class={`column${over ? ' over' : ''}${closed ? ' closed' : ''}`}
      onDragOver={(e) => { if (!closed && e.dataTransfer?.types.includes(DRAG)) { e.preventDefault(); setOver(true); } }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        const dragged = e.dataTransfer?.getData(DRAG);
        if (dragged) { e.preventDefault(); move(JSON.parse(dragged), id); }
      }}
    >
      <header>
        <div class="column-title">
          {index < 9 && <kbd>{index + 1}</kbd>}
          {milestone ? <a href={milestone.url} target="_blank" rel="noreferrer">{milestone.title}</a> : 'No milestone'}
          {closed && <span class="chip closed">closed</span>}
        </div>
        <div class="column-sub">
          <span>{list.length === total ? total : `${list.length} of ${total}`}</span>
          {id !== NONE && <DueDate id={id} />}
          <button class="hide" title="Hide this column" onClick={() => toggleColumn(id)}>×</button>
        </div>
      </header>
      <div class="cards">
        {list.map((card) => <Card key={card.number} card={card} />)}
        {list.length === 0 && <div class="empty">{total ? 'Nothing matches the filters.' : 'Empty.'}</div>}
      </div>
    </section>
  );
}

function Detail() {
  const card = focused.value === null ? undefined : cards.value.get(focused.value);
  if (!card) return <aside class="detail"><p class="empty">No card in focus.</p></aside>;
  const milestone = card.milestone === null ? undefined : milestones.value.get(card.milestone);
  const fields = Object.entries(card.fields ?? {});
  // GitHub has already sanitised bodyHTML; the sandbox (no scripts, no same-origin) is the
  // second lock. Links open in a new tab, since the frame itself may not navigate.
  const doc = `<!doctype html><base target="_blank"><style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 0 2px; overflow-wrap: anywhere; }
    img, video { max-width: 100%; height: auto; } pre { overflow: auto; padding: 8px; background: #8881; }
    code { font-size: 12px; } table { border-collapse: collapse; } td, th { border: 1px solid #8885; padding: 2px 6px; }
    blockquote { margin-left: 0; padding-left: 12px; border-left: 3px solid #8885; } a { color: #2f81f7; }
  </style>${card.bodyHTML || '<p><em>No description.</em></p>'}`;
  return (
    <aside class="detail">
      <h2><a href={card.url} target="_blank" rel="noreferrer">#{card.number}</a> {card.title}</h2>
      <dl>
        <dt>Milestone</dt><dd>{milestone ? `${milestone.title}${milestone.state === 'closed' ? ' (closed)' : ''}` : 'none'}</dd>
        <dt>Opened</dt><dd>{card.created.slice(0, 10)} by {card.author ?? 'a deleted account'}; updated {card.updated.slice(0, 10)}; {card.comments} comment{card.comments === 1 ? '' : 's'}</dd>
        {card.type && <><dt>Type</dt><dd>{card.type}</dd></>}
        {fields.length > 0 && <><dt>Fields</dt><dd>{fields.map(([k, v]) => `${k}: ${v}`).join(' · ')}</dd></>}
        {card.labels.length > 0 && <><dt>Labels</dt><dd>{card.labels.map((l) => <span class="label" style={{ background: `#${l.color}`, color: inkOn(l.color) }}>{l.name}</span>)}</dd></>}
        {card.assignees.length > 0 && <><dt>Assignees</dt><dd>{card.assignees.join(', ')}</dd></>}
        {card.parent && <><dt>Parent</dt><dd>#{card.parent.number} {card.parent.title}</dd></>}
        {(card.subIssues?.total ?? 0) > 0 && <><dt>Sub-issues</dt><dd>{card.subIssues!.completed} of {card.subIssues!.total} done</dd></>}
        {(card.projects ?? 0) > 0 && <><dt>Projects</dt><dd>in {card.projects} project{card.projects === 1 ? '' : 's'}</dd></>}
        {(card.closingPrs?.length ?? 0) > 0 && <><dt>Closed by</dt><dd>{card.closingPrs!.map((p) => <PrLink pr={p} />)}</dd></>}
        {(card.mentionedPrs?.length ?? 0) > 0 && <><dt>Mentioned in</dt><dd>{card.mentionedPrs!.map((p) => <PrLink pr={p} />)}</dd></>}
        {(card.closes?.length ?? 0) > 0 && <><dt>Closes</dt><dd>{card.closes!.map((c) => <div>#{c.number} {c.title} <small>({c.state.toLowerCase()})</small></div>)}</dd></>}
      </dl>
      <iframe class="body" sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc={doc} title="Description" />
    </aside>
  );
}

function PrLink({ pr }: { pr: { number: number; title: string; state: string; draft: boolean; url: string } }) {
  return <div><a href={pr.url} target="_blank" rel="noreferrer">#{pr.number}</a> {pr.title} <small>({pr.draft ? 'draft' : pr.state.toLowerCase()})</small></div>;
}

function Picker() {
  const [text, setText] = useState('');
  const [at, setAt] = useState(0);
  const issues = targets();
  const found = pickerMatches([...milestones.value.values()], board.value?.columns ?? [], text);
  const exact = found.some((m) => m.title.toLowerCase() === text.trim().toLowerCase());
  type Row = { label: string; hint: string; go(): void };
  const rows: Row[] = found.map((m) => ({
    label: m.title,
    hint: m.state === 'closed' ? 'closed — will ask to reopen' : m.due ? `due ${m.due}` : '',
    go: () => (m.state === 'open' ? move(issues, m.number) : moveToTitle(issues, m.title)),
  }));
  if ('no milestone'.includes(text.trim().toLowerCase())) rows.push({ label: 'No milestone', hint: 'take it off its milestone', go: () => move(issues, NONE) });
  if (text.trim() && !exact) rows.push({ label: `Create “${text.trim()}”`, hint: 'a new milestone', go: () => moveToTitle(issues, text.trim()) });
  const choose = (row: Row | undefined) => { if (row) { picking.value = false; row.go(); } };
  return (
    <div class="overlay" onClick={() => { picking.value = false; }}>
      <div class="picker" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">Move {issues.length === 1 ? `#${issues[0]}` : `${issues.length} cards`} to…</div>
        <input
          autoFocus
          placeholder="Type a milestone's name"
          value={text}
          onInput={(e) => { setText((e.target as HTMLInputElement).value); setAt(0); }}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Escape') picking.value = false;
            else if (e.key === 'ArrowDown') { e.preventDefault(); setAt(Math.min(rows.length - 1, at + 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setAt(Math.max(0, at - 1)); }
            else if (e.key === 'Enter') choose(rows[at]);
          }}
        />
        <ul>
          {rows.map((row, i) => (
            <li class={i === at ? 'at' : ''} onMouseEnter={() => setAt(i)} onClick={() => choose(row)}>
              <span>{row.label}</span><small>{row.hint}</small>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function ColumnsMenu() {
  return (
    <details class="menu">
      <summary>Columns ({visibleColumns.value.length}/{columns.value.length})</summary>
      <div class="menu-body">
        <div class="menu-actions">
          <button onClick={() => { shown.value = [...columns.value]; }}>All</button>
          <button onClick={() => { shown.value = []; }}>None</button>
        </div>
        {columns.value.map((id) => (
          <label>
            <input type="checkbox" checked={shown.value.includes(id)} onChange={() => toggleColumn(id)} />
            {columnTitle(id)}{milestones.value.get(id)?.state === 'closed' ? ' (closed)' : ''}
            <small>{[...cards.value.values()].filter((c) => columnOf(c) === id).length}</small>
          </label>
        ))}
      </div>
    </details>
  );
}

function Toolbar() {
  const f = filters.value;
  const set = (patch: Partial<typeof f>) => { filters.value = { ...f, ...patch }; };
  const all = [...cards.value.values()];
  const labels = [...new Set(all.flatMap((c) => c.labels.map((l) => l.name)))].sort((a, b) => a.localeCompare(b));
  const priorities = [...new Set(all.map(priorityOf).filter(Boolean))].sort();
  const active = JSON.stringify(f) !== JSON.stringify(noFilters);
  const unsaved = pending.value;
  return (
    <header class="toolbar">
      <strong>{board.value?.repo}</strong>
      {readOnly.value && <span class="chip would" title="MILESTONES_PERMISSIONS=read: moves are shown here and not written to GitHub">read-only</span>}
      <input id="search" type="search" placeholder="Search (/)" value={f.text}
        onInput={(e) => set({ text: (e.target as HTMLInputElement).value })}
        onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Escape' || e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
      <select value={f.kind} onChange={(e) => set({ kind: (e.target as HTMLSelectElement).value as typeof f.kind })}>
        <option value="">Issues and PRs</option><option value="issue">Issues</option><option value="pr">Pull requests</option>
      </select>
      <select value={f.label} onChange={(e) => set({ label: (e.target as HTMLSelectElement).value })}>
        <option value="">Any label</option>{labels.map((l) => <option value={l}>{l}</option>)}
      </select>
      <select value={f.priority} onChange={(e) => set({ priority: (e.target as HTMLSelectElement).value })}>
        <option value="">Any priority</option>{priorities.map((p) => <option value={p}>{p}</option>)}
      </select>
      <label><input type="checkbox" checked={f.hasPr} onChange={() => set({ hasPr: !f.hasPr })} /> has PR</label>
      <label><input type="checkbox" checked={f.unlabelled} onChange={() => set({ unlabelled: !f.unlabelled })} /> no labels</label>
      {active && <button onClick={() => { filters.value = noFilters; }}>Clear</button>}
      <span class="spacer" />
      {selected.value.size > 0 && <span class="chip">{selected.value.size} selected</span>}
      {unsaved.length > 0 && <span class={`chip ${unsaved.some((a) => a.status === 'failed') ? 'failed' : 'saving'}`}>
        {unsaved.filter((a) => a.status === 'failed').length ? `${unsaved.filter((a) => a.status === 'failed').length} not saved` : `saving ${unsaved.length}…`}
      </span>}
      <ColumnsMenu />
      <button onClick={refresh} title="Read everything from GitHub again">{loading.value ? 'Reading…' : 'Refresh'}</button>
      <details class="menu">
        <summary>Keys</summary>
        <div class="menu-body keys">
          <div><kbd>j</kbd> <kbd>k</kbd> down, up</div><div><kbd>h</kbd> <kbd>l</kbd> column left, right</div>
          <div><kbd>1</kbd>–<kbd>9</kbd> move to that column</div><div><kbd>0</kbd> take off its milestone</div>
          <div><kbd>m</kbd> pick any milestone, or make one</div><div><kbd>space</kbd> select; then keys move them all</div>
          <div><kbd>z</kbd> undo</div><div><kbd>o</kbd> open on GitHub</div><div><kbd>/</kbd> search</div><div><kbd>esc</kbd> clear selection</div>
          <div>Drag a card, or a selection, onto a column.</div>
        </div>
      </details>
    </header>
  );
}

function onKey(e: KeyboardEvent) {
  if (e.metaKey || e.ctrlKey || e.altKey || picking.value) return;
  const tag = (e.target as HTMLElement).tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  const key = e.key;
  if (key === 'j' || key === 'ArrowDown') step(0, 1);
  else if (key === 'k' || key === 'ArrowUp') step(0, -1);
  else if (key === 'h' || key === 'ArrowLeft') step(-1, 0);
  else if (key === 'l' || key === 'ArrowRight') step(1, 0);
  else if (key === ' ') { if (focused.value !== null) toggleSelected(focused.value); }
  else if (key === 'Escape') selected.value = new Set();
  else if (key === 'm') { if (targets().length) picking.value = true; }
  else if (key === 'z') undo();
  else if (key === 'o') { const c = focused.value === null ? undefined : cards.value.get(focused.value); if (c) window.open(c.url, '_blank', 'noreferrer'); }
  else if (key === '/') document.getElementById('search')?.focus();
  else if (key === '0') move(targets(), NONE);
  else if (/^[1-9]$/.test(key)) { const to = visibleColumns.value[Number(key) - 1]; if (to !== undefined) move(targets(), to); }
  else return;
  e.preventDefault();
}

function App() {
  useEffect(() => {
    start();
    window.addEventListener('keydown', onKey);
    // The quit guard: a write that hasn't reached GitHub is lost with the tab only in the
    // sense that nobody is left to press Retry, which is reason enough to ask.
    const guard = (e: BeforeUnloadEvent) => { if (pending.value.length) e.preventDefault(); };
    window.addEventListener('beforeunload', guard);
    return () => { window.removeEventListener('keydown', onKey); window.removeEventListener('beforeunload', guard); };
  }, []);
  if (fatal.value) return <p class="fatal">{fatal.value}</p>;
  if (!board.value) return <p class="fatal">Reading the board…</p>;
  return (
    <>
      <Toolbar />
      <main>
        <div class="columns">
          {visibleColumns.value.map((id, i) => <Column key={id} id={id} index={i} />)}
          {visibleColumns.value.length === 0 && <p class="empty">No columns shown. Pick some under Columns.</p>}
        </div>
        <Detail />
      </main>
      {picking.value && <Picker />}
      {toast.value && <div class="toast" onClick={() => { toast.value = ''; }}>{toast.value}</div>}
    </>
  );
}

render(<App />, document.getElementById('app')!);
