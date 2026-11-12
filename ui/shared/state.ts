// The board's state and everything a view may do to it. Views read the signals and call the
// functions; nothing else talks to the API.
import { computed, signal } from '@preact/signals';
import { Api, parseFragment } from './api.ts';
import type { Action, BoardData, Card, Milestone } from './api.ts';
import {
  NONE, allColumns, columnOf, group, initialColumns, latestAction, nextFocus, noFilters, unsettled,
} from './logic.ts';
import type { ColumnId, Filters } from './logic.ts';

const fragment = parseFragment(location.hash);
export const api = new Api(fragment.token);

export const loading = signal(true);
export const fatal = signal('');
export const toast = signal('');
export const board = signal<BoardData | null>(null);
export const cards = signal<Map<number, Card>>(new Map());
export const milestones = signal<Map<number, Milestone>>(new Map());
export const actions = signal<Map<number, Action>>(new Map());
export const filters = signal<Filters>(noFilters);
export const shown = signal<ColumnId[]>([]);
export const focused = signal<number | null>(null);
export const selected = signal<Set<number>>(new Set());
export const picking = signal(false);
/** Batches of moves made here, newest last, for `z`. Each entry is one card's way back. */
const undoStack: { issue: number; previous: number | null }[][] = [];

export const columns = computed<ColumnId[]>(() =>
  board.value ? allColumns(board.value.columns, [...milestones.value.values()], [...cards.value.values()]) : []);
export const grouped = computed(() => group([...cards.value.values()], filters.value));
export const visibleColumns = computed(() => columns.value.filter((id) => shown.value.includes(id)));
export const pending = computed(() => unsettled(actions.value.values()));
export const readOnly = computed(() => board.value?.permissions === 'read');

export function columnTitle(id: ColumnId): string {
  return id === NONE ? 'No milestone' : milestones.value.get(id)?.title ?? `Milestone ${id}`;
}

function say(message: string) {
  toast.value = message;
  setTimeout(() => { if (toast.value === message) toast.value = ''; }, 6000);
}

function take(data: BoardData, first: boolean) {
  board.value = data;
  cards.value = new Map(data.cards.map((c) => [c.number, c]));
  milestones.value = new Map(data.milestones.map((m) => [m.number, m]));
  actions.value = new Map(data.actions.map((a) => [a.id, a]));
  if (first) {
    shown.value = initialColumns(columns.value, data.milestones, fragment.from);
    document.title = `${data.repo} · milestones board`;
  }
  if (focused.value === null || !cards.value.has(focused.value)) {
    const top = visibleColumns.value.map((id) => grouped.value.get(id)?.[0]).find(Boolean);
    focused.value = top?.number ?? null;
  }
}

export async function start() {
  try {
    take(await api.board(), true);
  } catch (error) {
    fatal.value = `Could not load the board: ${(error as Error).message}. The link carries a `
      + 'token that is new each time `milestones board` starts; open the one it printed.';
    return;
  } finally {
    loading.value = false;
  }
  api.events({
    action: applyAction,
    milestone: (m) => { milestones.value = new Map(milestones.value).set(m.number, m); },
    reload: () => { api.board().then((data) => take(data, false)).catch(() => {}); },
  });
}

export async function refresh() {
  loading.value = true;
  try { take(await api.refresh(), false); } catch (error) { say((error as Error).message); }
  loading.value = false;
}

/** Record an action, and make its card show what the latest action on it says. */
export function applyAction(action: Action) {
  const all = new Map(actions.value).set(action.id, action);
  actions.value = all;
  const card = cards.value.get(action.issue);
  if (!card) return;
  const latest = latestAction(all.values(), action.issue);
  // No action left standing means the reverted one was the only one: back to before it.
  const value = latest ? latest.value : action.previous;
  if (card.milestone !== value) cards.value = new Map(cards.value).set(card.number, { ...card, milestone: value });
}

/** The focused card, or the whole selection when the focused card is part of it. */
export function targets(of: number | null = focused.value): number[] {
  if (of === null) return [];
  return selected.value.has(of) ? [...selected.value] : [of];
}

/** Move cards to a column. Shows the move at once; the server queues the write. */
export async function move(issues: number[], to: ColumnId, remember = true) {
  const value = to === NONE ? null : to;
  const moving = issues.map((n) => cards.value.get(n)).filter((c): c is Card => !!c && c.milestone !== value);
  if (moving.length === 0) return;
  const target = milestones.value.get(to);
  if (to !== NONE && target?.state !== 'open') {
    say(`'${columnTitle(to)}' is closed. Press m to reopen it and move there.`);
    return;
  }
  // Focus first, while the cards are still where they were.
  const leaving = new Set(moving.map((c) => c.number));
  if (focused.value !== null && leaving.has(focused.value)) {
    const from = grouped.value.get(columnOf(cards.value.get(focused.value)!)) ?? [];
    focused.value = nextFocus(from, leaving, focused.value) ?? focused.value;
  }
  const back = moving.map((c) => ({ issue: c.number, previous: c.milestone }));
  const next = new Map(cards.value);
  for (const c of moving) next.set(c.number, { ...c, milestone: value });
  cards.value = next;
  selected.value = new Set();
  try {
    const made = await api.act(moving.map((c) => ({ issue: c.number, kind: 'milestone' as const, value })));
    made.forEach(applyAction);
    if (remember) undoStack.push(back);
  } catch (error) {
    // Refused before anything was queued: put the cards back.
    const restore = new Map(cards.value);
    for (const b of back) {
      const c = restore.get(b.issue);
      if (c) restore.set(b.issue, { ...c, milestone: b.previous });
    }
    cards.value = restore;
    say((error as Error).message);
  }
}

/** Undo the last batch of moves made here. Undo is a move like any other, so it is written
 *  to GitHub too. */
export async function undo() {
  const batch = undoStack.pop();
  if (!batch) { say('Nothing to undo.'); return; }
  const by = new Map<ColumnId, number[]>();
  for (const b of batch) {
    const to = b.previous ?? NONE;
    by.set(to, [...(by.get(to) ?? []), b.issue]);
  }
  for (const [to, issues] of by) await move(issues, to, false);
  focused.value = batch[0]!.issue;
}

export async function retry(id: number) {
  try { applyAction(await api.retry(id)); } catch (error) { say((error as Error).message); }
}

export async function revert(id: number) {
  try { applyAction(await api.revert(id)); } catch (error) { say((error as Error).message); }
}

/** Move to a milestone by title, making it or (after asking) reopening it first. */
export async function moveToTitle(issues: number[], title: string) {
  const existing = [...milestones.value.values()].find((m) => m.title.toLowerCase() === title.toLowerCase());
  let reopen = false;
  if (existing?.state === 'closed') {
    if (!confirm(`'${existing.title}' is closed. Reopen it and move ${issues.length === 1 ? 'this' : `these ${issues.length}`} there?`)) return;
    reopen = true;
  } else if (!existing && !confirm(`Create a new milestone called '${title}'?`)) {
    return;
  }
  try {
    const milestone = await api.ensureMilestone(title, reopen);
    milestones.value = new Map(milestones.value).set(milestone.number, milestone);
    if (board.value && !board.value.columns.includes(milestone.number)) {
      board.value = { ...board.value, columns: [...board.value.columns, milestone.number] };
    }
    if (!shown.value.includes(milestone.number)) shown.value = [...shown.value, milestone.number];
    await move(issues, milestone.number);
  } catch (error) {
    say((error as Error).message);
  }
}

export async function setDue(number: number, due: string | null) {
  try {
    const milestone = await api.setDue(number, due);
    milestones.value = new Map(milestones.value).set(milestone.number, milestone);
  } catch (error) {
    say((error as Error).message);
  }
}

export function toggleSelected(issue: number) {
  const next = new Set(selected.value);
  if (!next.delete(issue)) next.add(issue);
  selected.value = next;
}

export function toggleColumn(id: ColumnId) {
  shown.value = shown.value.includes(id) ? shown.value.filter((c) => c !== id) : [...shown.value, id];
}

/** Step the focus within its column (dy) or to the neighbouring shown column (dx). */
export function step(dx: number, dy: number) {
  const cols = visibleColumns.value;
  const current = focused.value === null ? undefined : cards.value.get(focused.value);
  if (!current) {
    focused.value = cols.map((id) => grouped.value.get(id)?.[0]).find(Boolean)?.number ?? null;
    return;
  }
  const col = columnOf(current);
  const list = grouped.value.get(col) ?? [];
  const row = Math.max(0, list.findIndex((c) => c.number === current.number));
  if (dy) {
    focused.value = list[Math.min(list.length - 1, Math.max(0, row + dy))]?.number ?? focused.value;
    return;
  }
  // Skip columns the filters have emptied: there is nothing in them to land on.
  for (let at = cols.indexOf(col) + dx; at >= 0 && at < cols.length; at += dx) {
    const next = grouped.value.get(cols[at]!) ?? [];
    if (next.length) { focused.value = next[Math.min(row, next.length - 1)]!.number; return; }
  }
}
