// The column view: one column per milestone, cards you move by key, by drag or by picker.
// Everything it knows about GitHub comes through ui/shared/.
import { render } from 'preact';
import type { ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import type { Action, Card as CardData, Kind, PrRef, Suggestion } from '../shared/api.ts';
import {
  DIMENSIONS, FIELD_OF, NONE, age, askPrompt, cardStatus, cardValue, describe, effectiveValue, inkOn, isValueLabel,
  estimatedEffort, keyedOptions, noFilters, optionForKey, pickerMatches, priorityOf, sections, shownByLayout,
} from '../shared/logic.ts';
import type { ColumnId, DimKind } from '../shared/logic.ts';
import {
  actions, board, cards, change, columnTitle, cycleDimension, detailOpen, dimension, discardPlan, fatal, filters, focused, groupBy, grouped,
  layout, loading, milestoneColumns, milestones, modal, move, moveToTitle, notify, openSuggestions, pending, place,
  planInfo, readOnly, reviewPlan, refresh, resolve, retry, revert, scope, selectAll, selected, setDimension, setDue, shown, start, step,
  targets, toast, toggleColumn, toggleSelected, totals, trayOpen, undo, visibleColumns,
} from '../shared/state.ts';

const DRAG = 'application/x-milestones-cards';

/** Puts the keyboard in a field as it appears. The `autofocus` attribute is not enough: a browser
 *  ignores it while something else has focus, which is the case whenever a dialog is opened by
 *  a click on a button rather than by a key — the field then took no typing and no Escape. */
const focusOnMount = (el: HTMLElement | null) => { el?.focus(); };

function Status({ action }: { action: Action | undefined }) {
  if (!action) return null;
  if (action.status === 'pending') return <span class="status saving" title="Writing to GitHub…">saving…</span>;
  if (action.status === 'would') return <span class="status would" title="Read-only: not written to GitHub; kept in the plan file">planned</span>;
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
  const labels = card.labels.filter((l) => !isValueLabel(l.name, layout.value.mapping));
  const waiting = openSuggestions.value.filter((s) => s.issue === card.number).length;
  const closes = card.closes ?? [];
  const closedBy = card.closingPrs ?? [];
  const mentionedIn = card.mentionedPrs ?? [];
  return (
    <div
      ref={ref}
      class={`card${isFocused ? ' focused' : ''}${isSelected ? ' selected' : ''}${card.closed ? ' is-closed' : ''}`}
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
      {/* Read top to bottom: the title alone, so nothing interrupts it; the facts beside the
          number, as a GitHub issue list has them; what kind of thing it is, then where it stands
          (buttons, as in the panel); then, small, what it is tied to (who is assigned, the issues
          a pull request closes, the pull requests that close or only mention an issue), and its
          labels on a row of their own. The author is among the facts, as
          `@login`. */}
      <div class="card-title">{card.title}</div>
      <div class="card-facts">
        <a href={card.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>#{card.number}</a>
        <span title={`updated ${card.updated.slice(0, 10)}, opened ${card.created.slice(0, 10)}`}>{age(card.updated)}</span>
        {card.additions !== undefined && card.deletions !== undefined && (
          <span class="diff" title="lines added and removed"><ins>+{card.additions}</ins> <del>−{card.deletions}</del></span>
        )}
        {card.comments > 0 && <span title="comments">💬 {card.comments}</span>}
        {(card.subIssues?.total ?? 0) > 0 && <span title="sub-issues done">☑ {card.subIssues!.completed}/{card.subIssues!.total}</span>}
        {card.author && <span class="author" title={`opened by ${card.author}`}>@{card.author}</span>}
        {waiting > 0 && <span class="suggested" title="Has suggestions waiting for you">✎ {waiting}</span>}
        <Status action={cardStatus(actions.value.values(), card.number)} />
      </div>
      <div class="card-tags">
        <span class="tags">
          <span class={`tag kind ${card.kind}`}>{card.kind === 'pr' ? 'PR' : 'Issue'}</span>
          {card.draft && <span class="tag">draft</span>}
          {card.closed && <span class="tag">closed</span>}
          {card.type && <span class="tag">{card.type}</span>}
        </span>
        <span class="card-places"><Places card={card} onCard /></span>
      </div>
      {(card.assignees.length > 0 || closes.length > 0 || closedBy.length > 0 || mentionedIn.length > 0) && (
        <div class="card-ties">
          {card.assignees.length > 0 && (
            <span class="assigned">
              assigned to{' '}
              {/* The name is where a click will open a picker to reassign (#55); the arrow goes to
                  their profile on GitHub. */}
              {card.assignees.map((login, i) => (
                <>
                  {i > 0 && ', '}
                  <span class="who">{login}</span>
                  <a class="profile" href={`https://github.com/${login}`} target="_blank" rel="noreferrer"
                    title={`${login} on GitHub`} onClick={(e) => e.stopPropagation()}>↗</a>
                </>
              ))}
            </span>
          )}
          {closes.length > 0 && (
            <span class="closes" title={`Closes, when it merges:\n${closes.map((c) => `#${c.number} ${c.title}`).join('\n')}`}>
              closes {closes.map((c) => `#${c.number}`).join(', ')}
            </span>
          )}
          {/* Which pull requests would close it when they merge, and which only mention it, is
              the difference worth seeing, so they are two lists rather than one count. */}
          {closedBy.length > 0 && <span class="closed-by">closed by <PrNumbers prs={closedBy} /></span>}
          {mentionedIn.length > 0 && <span class="mentioned-in">mentioned in <PrNumbers prs={mentionedIn} /></span>}
        </div>
      )}
      {labels.length > 0 && (
        <div class="card-labels">
          {labels.map((l) => <span title={`label: ${l.name}`}><i style={{ background: `#${l.color}` }} />{l.name}</span>)}
        </div>
      )}
    </div>
  );
}

function DueDate({ id }: { id: number }) {
  const [editing, setEditing] = useState(false);
  const milestone = milestones.value.get(id);
  if (!milestone) return null;
  if (editing) {
    return (
      <input
        type="date"
        class="due-input"
        value={milestone.due ?? ''}
        ref={focusOnMount}
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
  const byMilestone = dimension.value === 'milestone';
  const milestone = byMilestone ? milestones.value.get(id as number) : undefined;
  const closed = milestone?.state === 'closed';
  const total = totals.value.get(id) ?? 0;
  const groups = sections(list, groupBy.value.filter((k) => k !== dimension.value), layout.value);
  return (
    <section
      class={`column${over ? ' over' : ''}${closed ? ' closed' : ''}${id === NONE && !byMilestone ? ' inbox' : ''}`}
      onDragOver={(e) => { if (!closed && e.dataTransfer?.types.includes(DRAG)) { e.preventDefault(); setOver(true); } }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        const dragged = e.dataTransfer?.getData(DRAG);
        if (dragged) { e.preventDefault(); place(JSON.parse(dragged), id); }
      }}
    >
      <header>
        <div class="column-title">
          {index < 9 && <kbd>{byMilestone ? index + 1 : index}</kbd>}
          {milestone ? <a href={milestone.url} target="_blank" rel="noreferrer">{milestone.title}</a> : columnTitle(id)}
          {closed && <span class="chip closed">closed</span>}
        </div>
        <div class="column-sub">
          <span>{list.length === total ? total : `${list.length} of ${total}`}</span>
          {byMilestone && id !== NONE && <DueDate id={id as number} />}
          {byMilestone && <button class="hide" title="Hide this column" onClick={() => toggleColumn(id)}>×</button>}
        </div>
      </header>
      <div class="cards">
        {groups.map((g) => (
          <>
            {g.label && (
              <button class="group-head" title="Select this group" onClick={() => selectAll(g.cards.map((c) => c.number))}>
                {g.label} <small>{g.cards.length}</small>
              </button>
            )}
            {g.cards.map((card) => <Card key={card.number} card={card} />)}
          </>
        ))}
        {list.length === 0 && <div class="empty">{total ? 'Nothing matches the filters.' : 'Empty.'}</div>}
      </div>
    </section>
  );
}

/** Folds the card panel to a strip at the edge, giving its room to the columns. */
function CollapseButton() {
  return <button class="collapse" title="Collapse this panel (i)" aria-label="Collapse the card panel" onClick={() => { detailOpen.value = false; }}>›</button>;
}

/** The focused card's milestone, priority and effort, each a button that opens the dialog its
 *  key does (m, p, e) — so a click here changes the whole selection when the focused card is
 *  part of one, exactly as the key would. Priority and effort are left off a pull request where
 *  they are an issue field, which a pull request cannot hold.
 *
 *  A card shows the same buttons (`onCard`), so there is still one way in: a click there focuses
 *  that card first, and doesn't reach the card's own click. On a card they come priority first,
 *  the milestone last since its title can be long, and a value the layout already shows is left
 *  off — unless it was read from a label, which is still worth knowing. A value read from a label
 *  is in italics on a card, where the panel spells it out. */
function Places({ card, onCard = false }: { card: CardData; onCard?: boolean }) {
  const milestone = card.milestone === null ? undefined : milestones.value.get(card.milestone);
  const open = (kind: DimKind) => (e: MouseEvent) => {
    if (onCard) { e.stopPropagation(); focused.value = card.number; }
    modal.value = kind;
  };
  const shown = (kind: DimKind) => !onCard || !shownByLayout(kind, dimension.value, groupBy.value);
  // A pull request's size, as an effort estimate, where nothing has been recorded: on a card
  // only, since the panel has room to say what is actually set.
  const estimate = onCard ? estimatedEffort(card, layout.value.mapping) : null;
  const estimateTip = estimate && `Estimated from ${estimate.lines} lines changed (+${card.additions} −${card.deletions}): `
    + `size ${estimate.size}.`;
  const value = (kind: 'priority' | 'effort') => {
    // Shown even when the layout shows effort: its heading then says "no effort", which the
    // estimate adds to rather than repeats.
    const guess = kind === 'effort' ? estimate : null;
    const found = effectiveValue(card, kind, layout.value.mapping);
    // Read from somewhere other than the standard label, which is where it is kept.
    const source = found.from === 'field' ? 'field' : found.from === 'other' ? 'old label' : '';
    const fromLabel = source !== '';
    const guessed = !found.value && guess;
    if (!shown(kind) && !fromLabel && !guessed) return null;
    const text = !shown(kind) && fromLabel ? `from ${source}`
      : found.value ? `${kind === 'effort' ? 'effort ' : ''}${found.value}${fromLabel && !onCard ? ` ·${source}` : ''}`
      : guessed ? `~${guess.size}` : `no ${kind}`;
    return (
      <button class={`place${found.value ? '' : ' unset'}${guessed ? ' estimate' : ''}${fromLabel && onCard ? ' from-label' : ''}${kind === 'priority' && found.value && shown(kind) ? ` chip priority p-${found.value.toLowerCase()}` : ''}`}
        title={guessed ? `${estimateTip} Set it (e).` : `${FIELD_OF[kind]}${fromLabel ? `, read from ${source === 'field' ? 'the organisation field' : 'another label'}; no ${FIELD_OF[kind]}: label is set` : ''}: change it (${kind[0]})`} onClick={open(kind)}>
        {text}
      </button>
    );
  };
  const where = shown('milestone') && (
    <button class={`place${milestone ? '' : ' unset'}`} title={`Milestone${milestone ? `: ${milestone.title}` : ''} — change it (m)`} onClick={open('milestone')}>
      {milestone ? `${milestone.title}${milestone.state === 'closed' ? ' (closed)' : ''}` : 'no milestone'}
    </button>
  );
  return onCard ? <>{value('priority')}{value('effort')}{where}</> : <>{where}{value('priority')}{value('effort')}</>;
}

/** The card panel, collapsed: a button to open it again, and what the strip can still usefully
 *  say and do about the focused card — its number, where it stands (each a way to change it),
 *  and how many suggestions are waiting, which opens the panel on them. */
function DetailStrip() {
  const card = focused.value === null ? undefined : cards.value.get(focused.value);
  const waiting = card ? openSuggestions.value.filter((s) => s.issue === card.number).length : 0;
  return (
    <aside class="detail-strip">
      <button class="open" title="Show the card panel (i)" aria-label="Show the card panel" onClick={() => { detailOpen.value = true; }}>‹</button>
      {card && <span>#{card.number}</span>}
      {card && <Places card={card} />}
      {waiting > 0 && (
        <button class="place suggested" title="Suggestions waiting for you on this card: show them" onClick={() => { detailOpen.value = true; }}>✎ {waiting}</button>
      )}
    </aside>
  );
}

function Detail() {
  const card = focused.value === null ? undefined : cards.value.get(focused.value);
  if (!card) return <aside class="detail"><div class="detail-head"><p class="empty">No card in focus.</p><CollapseButton /></div></aside>;
  // Priority and Effort are among the controls at the top; the rest are only shown.
  const fields = Object.entries(card.fields ?? {}).filter(([k]) => k !== FIELD_OF.priority && k !== FIELD_OF.effort);
  // GitHub has already sanitised bodyHTML; the sandbox (no scripts, no same-origin) is the
  // second lock. Links open in a new tab, since the frame itself may not navigate.
  const doc = `<!doctype html><base target="_blank"><style>
    :root { color-scheme: light dark; }
    body { font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 0 2px; overflow-wrap: anywhere; }
    img, video { max-width: 100%; height: auto; } pre { overflow: auto; padding: 8px; background: #8881; }
    code { font-size: 14px; } table { border-collapse: collapse; } td, th { border: 1px solid #8885; padding: 2px 6px; }
    blockquote { margin-left: 0; padding-left: 12px; border-left: 3px solid #8885; } a { color: #2f81f7; }
  </style>${card.bodyHTML || '<p><em>No description.</em></p>'}`;
  return (
    <aside class="detail">
      <div class="detail-head">
        <h2><a href={card.url} target="_blank" rel="noreferrer">#{card.number}</a> {card.title}</h2>
        <CollapseButton />
      </div>
      <div class="places"><Places card={card} /></div>
      {/* Above the facts about the card: what is waiting on a decision comes first. */}
      <Suggestions list={openSuggestions.value.filter((s) => s.issue === card.number)} />
      <dl>
        <dt>Opened</dt><dd>{card.created.slice(0, 10)} by {card.author ?? 'a deleted account'}; updated {card.updated.slice(0, 10)}; {card.comments} comment{card.comments === 1 ? '' : 's'}</dd>
        {card.type && <><dt>Type</dt><dd>{card.type}</dd></>}
        {fields.length > 0 && <><dt>Fields</dt><dd>{fields.map(([k, v]) => `${k}: ${v}`).join(' · ')}</dd></>}
        <dt>Labels</dt><dd>
          {card.labels.map((l) => <span class="label" style={{ background: `#${l.color}`, color: inkOn(l.color) }}>{l.name}</span>)}
          <button class="place unset" title="Add or remove labels (L)" onClick={() => { modal.value = 'labels'; }}>{card.labels.length ? 'edit' : 'add a label'}</button>
        </dd>
        {card.assignees.length > 0 && <><dt>Assignees</dt><dd>{card.assignees.join(', ')}</dd></>}
        {card.parent && <><dt>Parent</dt><dd>#{card.parent.number} {card.parent.title}</dd></>}
        {(card.subIssues?.total ?? 0) > 0 && <><dt>Sub-issues</dt><dd>{card.subIssues!.completed} of {card.subIssues!.total} done</dd></>}
        {(card.projects ?? 0) > 0 && <><dt>Projects</dt><dd>in {card.projects} project{card.projects === 1 ? '' : 's'}</dd></>}
        {(card.closingPrs?.length ?? 0) > 0 && <><dt>Closed by</dt><dd>{card.closingPrs!.map((p) => <PrLink pr={p} />)}</dd></>}
        {(card.mentionedPrs?.length ?? 0) > 0 && <><dt>Mentioned in</dt><dd>{card.mentionedPrs!.map((p) => <PrLink pr={p} />)}</dd></>}
        {card.branch && <><dt>Branch</dt><dd><code>{card.branch}</code></dd></>}
        {card.base && <><dt>Targets</dt><dd><code>{card.base}</code>{card.basePr && <PrLink pr={card.basePr} />}</dd></>}
        {(card.closes?.length ?? 0) > 0 && <><dt>Closes</dt><dd>{card.closes!.map((c) => <div>#{c.number} {c.title} <small>({c.state.toLowerCase()})</small></div>)}</dd></>}
      </dl>
      <div class="detail-actions">
        <button onClick={() => { modal.value = 'ask'; }} title="Copy a prompt for a coding agent; its answer comes back as suggestions (a)">Ask an agent…</button>
      </div>
      <iframe class="body" sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc={doc} title="Description" />
    </aside>
  );
}

function SuggestionRow({ s, withIssue }: { s: Suggestion; withIssue?: boolean }) {
  const card = cards.value.get(s.issue);
  return (
    <li class="suggestion">
      <div class="suggestion-what">
        {withIssue && <a href="#" onClick={(e) => { e.preventDefault(); focused.value = s.issue; }}>#{s.issue}</a>}{' '}
        <strong>{describe(s.kind, s.value, milestones.value)}</strong>
        {withIssue && card && <small> — {card.title}</small>}
      </div>
      {s.reason && <div class="suggestion-why">{s.reason}</div>}
      <div class="suggestion-foot">
        <small>{s.source.replace(/^rule:/, 'board rule: ')}</small>
        <button onClick={() => resolve([s.id], 'accept')}>Accept</button>
        <button onClick={() => resolve([s.id], 'reject')}>Reject</button>
      </div>
    </li>
  );
}

function Suggestions({ list }: { list: Suggestion[] }) {
  if (list.length === 0) return null;
  return (
    <section class="suggestions">
      <h3>Suggested</h3>
      <ul>{list.map((s) => <SuggestionRow key={s.id} s={s} />)}</ul>
    </section>
  );
}

/** Every open suggestion, by who made it, with a way to take a whole source at once. */
function Tray() {
  const bySource = new Map<string, Suggestion[]>();
  for (const s of openSuggestions.value) bySource.set(s.source, [...(bySource.get(s.source) ?? []), s]);
  return (
    <aside class="detail tray">
      <h2>Suggestions <button class="close" onClick={() => { trayOpen.value = false; }}>×</button></h2>
      {bySource.size === 0 && <p class="empty">Nothing is waiting for you.</p>}
      {[...bySource].map(([source, list]) => (
        <section class="suggestions">
          <h3>
            {source.replace(/^rule:/, 'board rule: ')} <small>({list.length})</small>
            <button onClick={() => { if (confirm(`Accept all ${list.length} from ${source}? Each is written to GitHub.`)) resolve(list.map((s) => s.id), 'accept'); }}>Accept all</button>
            <button onClick={() => { if (confirm(`Reject all ${list.length} from ${source}?`)) resolve(list.map((s) => s.id), 'reject'); }}>Reject all</button>
          </h3>
          <ul>{list.slice(0, 200).map((s) => <SuggestionRow key={s.id} s={s} withIssue />)}</ul>
          {list.length > 200 && <p class="empty">…and {list.length - 200} more.</p>}
        </section>
      ))}
    </aside>
  );
}

type ChoiceOption = { label: string; value: string | null; keys: number[] };
/** Priority and effort take their numbers from `keyedOptions`, so a key is the same level on
 *  both, and Clear is 0, as it is in their column views; closing is numbered down the list. */
const levels = (kind: 'priority' | 'effort'): ChoiceOption[] => [
  ...keyedOptions(kind, layout.value.mapping).map((o) => ({ label: o.value, value: o.value, keys: o.keys })),
  { label: 'Clear', value: null, keys: [0] },
];
const CHOICES: Partial<Record<Kind, { title: string; options(): ChoiceOption[] }>> = {
  priority: { title: 'Priority', options: () => levels('priority') },
  effort: { title: 'Effort', options: () => levels('effort') },
  state: {
    title: 'Close',
    options: () => [{ label: 'Close as completed', value: 'completed' }, { label: 'Close as not planned', value: 'not_planned' }, { label: 'Reopen', value: null }]
      .map((o, i) => ({ ...o, keys: [i + 1] })),
  },
};

/** A short list to pick one of, by number key or click: priority, effort, close. */
function Choice({ kind }: { kind: Kind }) {
  const spec = CHOICES[kind]!;
  const options = spec.options();
  const issues = targets();
  const close = () => { modal.value = null; };
  const choose = (value: string | null) => { close(); change(issues, kind, value); };
  // A layout effect, as in Menu: the number key may come before the next frame does.
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      e.stopPropagation();
      if (e.key === 'Escape') close();
      const option = /^[0-9]$/.test(e.key) ? options.find((o) => o.keys.includes(Number(e.key))) : undefined;
      if (option) { e.preventDefault(); choose(option.value); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);
  return (
    <div class="overlay" onClick={close}>
      <div class="picker" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">{spec.title} for {issues.length === 1 ? `#${issues[0]}` : `${issues.length} cards`}</div>
        <ul>{options.map((o) => <li onClick={() => choose(o.value)}><span>{o.keys.map((k) => <><kbd>{k}</kbd> </>)}{o.label}</span></li>)}</ul>
      </div>
    </div>
  );
}

/** A title to edit, or a comment to write. */
function TextDialog({ kind }: { kind: 'title' | 'comment' }) {
  const issues = targets();
  const card = cards.value.get(issues[0]!);
  const [text, setText] = useState(kind === 'title' ? card?.title ?? '' : '');
  const close = () => { modal.value = null; };
  const save = () => { if (text.trim()) { close(); change(kind === 'title' ? issues.slice(0, 1) : issues, kind, text); } };
  const keys = (e: KeyboardEvent) => {
    e.stopPropagation();
    if (e.key === 'Escape') close();
    else if (e.key === 'Enter' && (kind === 'title' || e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
  };
  return (
    <div class="overlay" onClick={close}>
      <div class="picker wide" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">{kind === 'title' ? `Retitle #${issues[0]}` : `Comment on ${issues.length === 1 ? `#${issues[0]}` : `${issues.length} cards`}`}</div>
        {kind === 'title'
          ? <input ref={focusOnMount} value={text} onInput={(e) => setText((e.target as HTMLInputElement).value)} onKeyDown={keys} />
          : <textarea ref={focusOnMount} rows={8} placeholder="Markdown. ⌘↵ or Ctrl+↵ to post." value={text} onInput={(e) => setText((e.target as HTMLTextAreaElement).value)} onKeyDown={keys} />}
        <div class="picker-foot"><button onClick={close}>Cancel</button><button class="primary" onClick={save}>{kind === 'title' ? 'Save' : 'Comment'}</button></div>
      </div>
    </div>
  );
}

/** Toggle labels on the focused card or the selection; each toggle is one change. */
function LabelsDialog() {
  const [text, setText] = useState('');
  const [at, setAt] = useState(0);
  const issues = targets();
  const has = (name: string) => issues.every((n) => (cardValue(cards.value.get(n)!, 'labels') as string[]).includes(name));
  const words = text.trim().toLowerCase();
  const found = (board.value?.labels ?? []).filter((l) => l.name.toLowerCase().includes(words));
  const rows = [...found.map((l) => l.name), ...(words && !found.some((l) => l.name.toLowerCase() === words) ? [`+${text.trim()}`] : [])];
  const toggle = (row: string | undefined) => {
    if (!row) return;
    const name = row.startsWith('+') ? row.slice(1) : row;
    change(issues, 'labels', has(name) ? { remove: [name] } : { add: [name] });
  };
  return (
    <div class="overlay" onClick={() => { modal.value = null; }}>
      <div class="picker" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">Labels for {issues.length === 1 ? `#${issues[0]}` : `${issues.length} cards`} — ↵ toggles, esc closes</div>
        <input ref={focusOnMount} placeholder="Type a label's name" value={text}
          onInput={(e) => { setText((e.target as HTMLInputElement).value); setAt(0); }}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Escape') modal.value = null;
            else if (e.key === 'ArrowDown') { e.preventDefault(); setAt(Math.min(rows.length - 1, at + 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setAt(Math.max(0, at - 1)); }
            else if (e.key === 'Enter') toggle(rows[at]);
          }} />
        <ul>
          {rows.map((row, i) => (
            <li class={i === at ? 'at' : ''} onMouseEnter={() => setAt(i)} onClick={() => toggle(row)}>
              <span>{row.startsWith('+') ? `Create “${row.slice(1)}”` : `${has(row) ? '✓' : '　'} ${row}`}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** Hand the focused card, or the selection, to a coding agent: say what you think, and copy
 *  a prompt to paste into its session. Its answer comes back as suggestions. */
function AskDialog() {
  const [view, setView] = useState('');
  const asked = targets().map((n) => cards.value.get(n)!).filter(Boolean);
  const prompt = askPrompt(board.value?.repo ?? '', asked, milestones.value, view, layout.value.mapping);
  const close = () => { modal.value = null; };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(prompt);
      notify('Copied. Paste it into your coding agent; its suggestions will appear here.');
      close();
    } catch {
      notify('Could not reach the clipboard; select the text and copy it by hand.');
    }
  };
  return (
    <div class="overlay" onClick={close}>
      <div class="picker wide" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">Ask a coding agent about {asked.length === 1 ? `#${asked[0]!.number}` : `${asked.length} cards`} — what do you think?</div>
        <textarea ref={focusOnMount} rows={4} placeholder="e.g. medium priority, medium effort: we can add this as its own identifier source that won't conflict with existing ones"
          value={view} onInput={(e) => setView((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Escape') close();
            else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); copy(); }
          }} />
        <pre class="prompt">{prompt}</pre>
        <div class="picker-foot"><button onClick={close}>Cancel</button><button class="primary" onClick={copy}>Copy prompt (⌘↵)</button></div>
      </div>
    </div>
  );
}

function PrNumbers({ prs }: { prs: PrRef[] }) {
  return (
    <>
      {prs.map((p, i) => (
        <>
          {i > 0 && ', '}
          <a href={p.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
            title={`#${p.number} ${p.title} (${p.draft ? 'draft' : p.state.toLowerCase()})`}>#{p.number}</a>
        </>
      ))}
    </>
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
  const choose = (row: Row | undefined) => { if (row) { modal.value = null; row.go(); } };
  return (
    <div class="overlay" onClick={() => { modal.value = null; }}>
      <div class="picker" onClick={(e) => e.stopPropagation()}>
        <div class="picker-head">Move {issues.length === 1 ? `#${issues[0]}` : `${issues.length} cards`} to…</div>
        <input
          ref={focusOnMount}
          placeholder="Type a milestone's name"
          value={text}
          onInput={(e) => { setText((e.target as HTMLInputElement).value); setAt(0); }}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Escape') modal.value = null;
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

/** The toolbar menu that is open, by name: one at a time, so two never overlap. */
const openMenu = signal<string | null>(null);

/** A button that drops a panel under itself. It closes on a press anywhere outside and on
 *  Escape. A press in the card panel's description would not count — that is a sandboxed frame,
 *  whose events never reach this document — so `main` is given `menu-open` while a menu is, and
 *  the stylesheet lets presses through the frame for as long. `align` is the edge of the
 *  button the panel hangs from to begin with; one that would run off the page that way is hung
 *  from the other edge instead, since where a button sits depends on how the bar has wrapped. */
function Menu({ name, label, title, align = 'left', wide, children }: {
  name: string; label: ComponentChildren; title?: string; align?: 'left' | 'right'; wide?: boolean; children: ComponentChildren;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const [side, setSide] = useState(align);
  const open = openMenu.value === name;
  // Before the panel is painted: measure it where `align` puts it, and turn it round if it is
  // over the edge of the page.
  useLayoutEffect(() => {
    if (!open) { setSide(align); return; }
    const box = body.current?.getBoundingClientRect();
    if (box && side === 'left' && box.right > document.documentElement.clientWidth) setSide('right');
    else if (box && side === 'right' && box.left < 0) setSide('left');
  }, [open]);
  // A layout effect, so the listeners are there as soon as the panel is: a plain effect waits
  // for the next frame, and an Escape sent straight after the click (by a script) was missed.
  useLayoutEffect(() => {
    if (!open) return undefined;
    const close = () => { if (openMenu.value === name) openMenu.value = null; };
    const outside = (e: Event) => { if (!ref.current?.contains(e.target as Node)) close(); };
    // Captured, and stopped: Escape closes the menu and does not go on to clear the selection.
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('pointerdown', outside, true);
    window.addEventListener('keydown', key, true);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      window.removeEventListener('keydown', key, true);
    };
  }, [open]);
  return (
    <div class="menu" ref={ref}>
      <button class={open ? 'on' : ''} aria-haspopup="true" aria-expanded={open} title={title} onClick={() => { openMenu.value = open ? null : name; }}>{label}</button>
      {open && <div ref={body} class={`menu-body ${side}${wide ? ' wide' : ''}`}>{children}</div>}
    </div>
  );
}

/** Which milestones are on show. In the milestone view they are the columns; in the
 *  priority and effort views they are the scope — whose cards are being sorted. */
function ColumnsMenu() {
  const all = milestoneColumns.value;
  const count = (id: ColumnId) => [...cards.value.values()].filter((c) => (c.milestone ?? NONE) === id).length;
  return (
    <Menu name="columns" align="right" wide label={`${dimension.value === 'milestone' ? 'Columns' : 'In milestones'} (${shown.value.length}/${all.length})`}>
      <div class="menu-actions">
        <button onClick={() => { shown.value = [...all]; }}>All</button>
        <button onClick={() => { shown.value = []; }}>None</button>
      </div>
      {all.map((id) => (
        <label>
          <input type="checkbox" checked={shown.value.includes(id)} onChange={() => toggleColumn(id)} />
          {columnTitle(id, 'milestone')}{milestones.value.get(id as number)?.state === 'closed' ? ' (closed)' : ''}
          <small>{count(id)}</small>
        </label>
      ))}
    </Menu>
  );
}

const GROUPINGS: { label: string; by: DimKind[] }[] = [
  { label: 'No grouping', by: [] },
  { label: 'Group: priority', by: ['priority'] },
  { label: 'Group: effort', by: ['effort'] },
  { label: 'Group: priority, effort', by: ['priority', 'effort'] },
  { label: 'Group: milestone', by: ['milestone'] },
];

/** What the columns are, and what the cards in a column are grouped by. */
function ViewControls() {
  // What is in effect, not what is saved: a grouping by what the columns already are does
  // nothing, and its option is not offered, so the select would otherwise show another's label.
  const current = groupBy.value.filter((k) => k !== dimension.value).join(',');
  return (
    <>
      <span class="segmented" title="What the columns are (v)">
        {DIMENSIONS.map((d) => (
          <button class={dimension.value === d.kind ? 'on' : ''} onClick={() => setDimension(d.kind)}>{d.title}</button>
        ))}
      </span>
      <select value={current} title="Group the cards within each column; click a group's heading to select it"
        onChange={(e) => { groupBy.value = (e.target as HTMLSelectElement).value.split(',').filter(Boolean) as DimKind[]; }}>
        {GROUPINGS.filter((g) => !g.by.includes(dimension.value) || g.by.length > 1).map((g) => <option value={g.by.join(',')}>{g.label}</option>)}
      </select>
    </>
  );
}

/** The search and the filters, on a row of their own under the top bar. They were once in a
 *  Filters menu, to keep the top bar to one line on a laptop, but they are used too often to be
 *  a click away. Each one that is on is drawn in the accent colour, so a filtered board never
 *  looks like a whole one. */
function FilterBar() {
  const f = filters.value;
  const set = (patch: Partial<typeof f>) => { filters.value = { ...f, ...patch }; };
  const all = [...cards.value.values()];
  const labels = [...new Set(all.flatMap((c) => c.labels.map((l) => l.name)))].sort((a, b) => a.localeCompare(b));
  const priorities = [...new Set(all.map((c) => priorityOf(c, layout.value.mapping)).filter(Boolean))].sort();
  const authors = [...new Set(all.map((c) => c.author).filter((a): a is string => !!a))].sort((a, b) => a.localeCompare(b));
  const mark = (v: unknown) => (v ? 'filtering' : '');
  return (
    <div class="filterbar">
      <input id="search" type="search" placeholder="Search (/)" value={f.text} class={mark(f.text)}
        onInput={(e) => set({ text: (e.target as HTMLInputElement).value })}
        onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Escape' || e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
      <label class={mark(f.kind)}>Show
        <select value={f.kind} onChange={(e) => set({ kind: (e.target as HTMLSelectElement).value as typeof f.kind })}>
          <option value="">Issues and PRs</option><option value="issue">Issues</option><option value="pr">Pull requests</option>
        </select>
      </label>
      <label class={mark(f.label)}>Label
        <select value={f.label} onChange={(e) => set({ label: (e.target as HTMLSelectElement).value })}>
          <option value="">Any label</option>{labels.map((l) => <option value={l}>{l}</option>)}
        </select>
      </label>
      <label class={mark(f.priority)}>Priority
        <select value={f.priority} onChange={(e) => set({ priority: (e.target as HTMLSelectElement).value })}>
          <option value="">Any priority</option>{priorities.map((p) => <option value={p}>{p}</option>)}
        </select>
      </label>
      <label class={mark(f.hasPr)}><input type="checkbox" checked={f.hasPr} onChange={() => set({ hasPr: !f.hasPr })} /> Has a PR</label>
      <label class={mark(f.unlabelled)}><input type="checkbox" checked={f.unlabelled} onChange={() => set({ unlabelled: !f.unlabelled })} /> No labels</label>
      {/* Last and quiet: who opened a card is rarely what the board is narrowed by. */}
      <label class={`secondary ${mark(f.author)}`}>by
        <select value={f.author} onChange={(e) => set({ author: (e.target as HTMLSelectElement).value })}>
          <option value="">anyone</option>{authors.map((a) => <option value={a}>{a}</option>)}
        </select>
      </label>
      {[f.text, f.kind, f.label, f.priority, f.hasPr, f.unlabelled, f.author].some(Boolean) && <button class="clear" onClick={() => { filters.value = noFilters; }}>Clear</button>}
    </div>
  );
}

function Toolbar() {
  const scoped = shown.value.map((id) => columnTitle(id, 'milestone'));
  const unsaved = pending.value;
  return (
    <header class="toolbar">
      <strong>{board.value?.repo}</strong>
      <ViewControls />
      {dimension.value !== 'milestone' && scope.value && (
        <span class="chip scope" title={`Only cards on these milestones are being sorted: ${scoped.join(', ') || 'none'}. Change it under 'In milestones'.`}>
          in {scoped.length <= 3 ? scoped.join(', ') : `${scoped.length} milestones`}
        </span>
      )}
      <div class="toolbar-end">
        {selected.value.size > 0 && <span class="chip">{selected.value.size} selected</span>}
        {unsaved.length > 0 && <span class={`chip ${unsaved.some((a) => a.status === 'failed') ? 'failed' : 'saving'}`}>
          {unsaved.filter((a) => a.status === 'failed').length ? `${unsaved.filter((a) => a.status === 'failed').length} not saved` : `saving ${unsaved.length}…`}
        </span>}
        <button class={openSuggestions.value.length ? 'has-suggestions' : ''} onClick={() => { trayOpen.value = !trayOpen.value; }} title="What the board's rules and your agents propose (s)">
          Suggestions ({openSuggestions.value.length})
        </button>
        <ColumnsMenu />
        <button class="refresh" onClick={refresh} title="Read everything from GitHub again">{loading.value ? 'Reading…' : 'Refresh'}</button>
        <Menu name="keys" align="right" label="?" title="Keys">
          <div class="keys">
            <div><kbd>j</kbd> <kbd>k</kbd> down, up</div><div><kbd>h</kbd> <kbd>l</kbd> column left, right</div>
            <div><kbd>v</kbd> columns by milestone, priority, effort</div>
            <div><kbd>1</kbd>–<kbd>9</kbd> put in that column</div><div><kbd>0</kbd> clear: no milestone, no priority, no effort</div>
            <div><kbd>m</kbd> pick any milestone, or make one</div><div><kbd>space</kbd> select; then keys move them all</div>
            <div><kbd>p</kbd> priority <kbd>e</kbd> effort <kbd>L</kbd> labels</div>
            <div>priority: <kbd>1</kbd>–<kbd>4</kbd> urgent, high, medium, low; effort: <kbd>1</kbd>–<kbd>5</kbd> XL to XS</div>
            <div><kbd>t</kbd> retitle <kbd>c</kbd> comment <kbd>x</kbd> close or reopen</div>
            <div><kbd>a</kbd> ask a coding agent <kbd>s</kbd> suggestions</div><div><kbd>i</kbd> show or collapse the card panel</div>
            <div><kbd>z</kbd> undo</div><div><kbd>o</kbd> open on GitHub</div><div><kbd>/</kbd> search</div><div><kbd>esc</kbd> clear selection</div>
            <div>Drag a card, or a selection, onto a column.</div>
          </div>
        </Menu>
      </div>
    </header>
  );
}

const MODAL_KEYS: Record<string, Kind | 'ask'> = { a: 'ask', m: 'milestone', p: 'priority', e: 'effort', L: 'labels', t: 'title', c: 'comment', x: 'state' };

/** What becomes of changes made here, said where it can't be missed: on a read-only board
 *  they go to the plan file, not GitHub; on a writable one, a plan may be waiting. */
function PlanBanner() {
  const { path, count } = planInfo.value;
  if (readOnly.value) {
    return (
      <div class="banner rehearsal">
        <strong>Read-only.</strong> Nothing you do here is written to GitHub.{' '}
        {count === 0
          ? 'Changes you make are kept in a plan file as you go, for a writable board to apply later.'
          : <>Your <strong>{count} change{count === 1 ? '' : 's'}</strong> {count === 1 ? 'is' : 'are'} kept in the plan file, for a writable board to apply later.</>}
        {path && <code title="A JSON file a person or a coding agent can read">{path}</code>}
      </div>
    );
  }
  if (count === 0) return null;
  return (
    <div class="banner waiting">
      A read-only session left a plan of <strong>{count} change{count === 1 ? '' : 's'}</strong>. None {count === 1 ? 'has' : 'have'} been written.
      <button class="primary" onClick={reviewPlan}>Review as suggestions</button>
      <button onClick={() => { if (confirm(`Discard the plan of ${count} change${count === 1 ? '' : 's'}? This deletes the plan file.`)) discardPlan(); }}>Discard</button>
    </div>
  );
}

function onKey(e: KeyboardEvent) {
  // An open menu has the keyboard: j or 1 behind it would move a card nobody is looking at.
  // Not the list of keys, though, which is there to be read while trying them.
  if (e.metaKey || e.ctrlKey || e.altKey || modal.value || (openMenu.value && openMenu.value !== 'keys')) return;
  const tag = (e.target as HTMLElement).tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  const key = e.key;
  if (key === 'j' || key === 'ArrowDown') step(0, 1);
  else if (key === 'k' || key === 'ArrowUp') step(0, -1);
  else if (key === 'h' || key === 'ArrowLeft') step(-1, 0);
  else if (key === 'l' || key === 'ArrowRight') step(1, 0);
  else if (key === ' ') { if (focused.value !== null) toggleSelected(focused.value); }
  else if (key === 'Escape') selected.value = new Set();
  else if (key in MODAL_KEYS) { if (targets().length) modal.value = MODAL_KEYS[key]!; }
  else if (key === 's') trayOpen.value = !trayOpen.value;
  else if (key === 'i') detailOpen.value = !detailOpen.value;
  else if (key === 'z') undo();
  else if (key === 'o') { const c = focused.value === null ? undefined : cards.value.get(focused.value); if (c) window.open(c.url, '_blank', 'noreferrer'); }
  else if (key === '/') document.getElementById('search')?.focus();
  else if (key === 'v') cycleDimension();
  else if (key === '0') place(targets(), NONE);
  else if (/^[1-9]$/.test(key)) {
    // In the priority and effort views a key is a level, the same on both (`keyedOptions`), and
    // does nothing while that level's column is hidden; in the milestone view it is a position.
    const kind = dimension.value;
    const to = kind === 'milestone' ? visibleColumns.value[Number(key) - 1] : optionForKey(kind, Number(key), layout.value.mapping);
    if (to !== undefined && visibleColumns.value.includes(to)) place(targets(), to);
  }
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
      <FilterBar />
      <PlanBanner />
      <main class={openMenu.value ? 'menu-open' : ''}>
        <div class="columns">
          {visibleColumns.value.map((id, i) => <Column key={id} id={id} index={i} />)}
          {visibleColumns.value.length === 0 && <p class="empty">No columns shown. Pick some under Columns.</p>}
        </div>
        {trayOpen.value ? <Tray /> : detailOpen.value ? <Detail /> : <DetailStrip />}
      </main>
      {modal.value === 'milestone' && <Picker />}
      {(modal.value === 'priority' || modal.value === 'effort' || modal.value === 'state') && <Choice kind={modal.value} />}
      {(modal.value === 'title' || modal.value === 'comment') && <TextDialog kind={modal.value} />}
      {modal.value === 'labels' && <LabelsDialog />}
      {modal.value === 'ask' && <AskDialog />}
      {toast.value && <div class="toast" onClick={() => { toast.value = ''; }}>{toast.value}</div>}
    </>
  );
}

render(<App />, document.getElementById('app')!);
