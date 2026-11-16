// The board's state and everything a view may do to it. Views read the signals and call the
// functions; nothing else talks to the API.
import { computed, effect, signal } from '@preact/signals';
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
/** One card's way back. */
interface Way { issue: number; previous: number | null }
/** Batches of moves made here, for `z`, in the order they were made — pushed before the
 *  request goes, so overlapping requests can't reorder them. Each resolves, once answered, to
 *  its cards' ways back (the issues a moved pull request carried included), or to null if
 *  nothing moved. */
const undoStack: Promise<Way[] | null>[] = [];

export const columns = computed<ColumnId[]>(() =>
  board.value ? allColumns(board.value.columns, [...milestones.value.values()], [...cards.value.values()]) : []);
export const grouped = computed(() => group([...cards.value.values()], filters.value));
export const visibleColumns = computed(() => columns.value.filter((id) => shown.value.includes(id)));
export const pending = computed(() => unsettled(actions.value.values()));
/** The cards on screen: matching the filters, in a shown column. Only these can be moved by
 *  key or picker, so a hidden card is never written by accident. */
export const visible = computed(() => new Set(
  visibleColumns.value.flatMap((id) => (grouped.value.get(id) ?? []).map((c) => c.number))));

function topCard(): number | null {
  return visibleColumns.value.map((id) => grouped.value.get(id)?.[0]).find(Boolean)?.number ?? null;
}

// Focus follows the filters and the shown columns: a focused card they hide hands focus on,
// and once they show cards again, the top one takes it.
effect(() => {
  if (focused.value === null || !visible.value.has(focused.value)) focused.value = topCard();
});
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
  if (focused.value === null || !visible.value.has(focused.value)) focused.value = topCard();
}

// Snapshots and events, kept in order. While a snapshot is being fetched, events are held
// back; once it is in, those numbered after it are replayed and the rest, which it already
// holds, dropped. A snapshot older than one already taken is ignored.
let fetching = 0;
let held: { rev: number; apply: () => void }[] = [];
let snapshotRev = -1;

function onEvent(rev: number, apply: () => void) {
  if (fetching) held.push({ rev, apply });
  else apply();
}

async function sync(fetch: () => Promise<BoardData>, first = false) {
  fetching++;
  try {
    const data = await fetch();
    if (data.rev < snapshotRev) return;
    snapshotRev = data.rev;
    take(data, first);
    for (const e of held) if (e.rev > data.rev) e.apply();
  } finally {
    if (--fetching === 0) held = [];
  }
}

export async function start() {
  // The stream first, and the board once it is open, so no event falls between the two. It
  // reopens by itself after a drop, with nothing replayed: each reopening fetches the board.
  let opened = false;
  await new Promise<void>((ready) => {
    api.events({
      open: () => {
        if (opened) sync(() => api.board()).catch(() => {});
        opened = true;
        ready();
      },
      error: ready, // a refused stream: the board fetch below says why
      action: (a, rev) => onEvent(rev, () => applyAction(a)),
      card: (c, rev) => onEvent(rev, () => applyCard(c)),
      milestone: (m, rev) => onEvent(rev, () => applyMilestone(m)),
      reload: () => { sync(() => api.board()).catch(() => {}); },
    });
  });
  try {
    await sync(() => api.board(), true);
  } catch (error) {
    fatal.value = `Could not load the board: ${(error as Error).message}. The link carries a `
      + 'token that is new each time `milestones board` starts; open the one it printed.';
  } finally {
    loading.value = false;
  }
}

export async function refresh() {
  loading.value = true;
  try { await sync(() => api.refresh()); } catch (error) { say((error as Error).message); }
  loading.value = false;
}

/** A milestone made, reopened or re-dated, here or in another tab. One newly open gets a
 *  column, at the end until a refresh puts it in its place. */
export function applyMilestone(m: Milestone) {
  milestones.value = new Map(milestones.value).set(m.number, m);
  if (m.state === 'open' && board.value && !board.value.columns.includes(m.number)) {
    board.value = { ...board.value, columns: [...board.value.columns, m.number] };
  }
}

/** Record an action, and make its card show what the latest standing action on it says. With
 *  none left standing, the card shows what GitHub has, which only the server knows: it sends
 *  the card (`applyCard`). Not the reverted action's `previous`, which may itself never have
 *  reached GitHub. */
export function applyAction(action: Action) {
  // A copy older than the one held — a POST answer the worker's event overtook — is dropped.
  const known = actions.value.get(action.id);
  if (known && known.rev > action.rev) return;
  const all = new Map(actions.value).set(action.id, action);
  actions.value = all;
  const card = cards.value.get(action.issue);
  const latest = latestAction(all.values(), action.issue);
  if (!card || !latest) return;
  if (card.milestone !== latest.value) cards.value = new Map(cards.value).set(card.number, { ...card, milestone: latest.value });
}

export function applyCard(card: Card) {
  cards.value = new Map(cards.value).set(card.number, card);
}

/** The focused card, or the whole selection when the focused card is part of it — the cards
 *  of it on screen, since a filter may hide some that were selected. */
export function targets(of: number | null = focused.value): number[] {
  if (of === null || !visible.value.has(of)) return [];
  return selected.value.has(of) ? [...selected.value].filter((n) => visible.value.has(n)) : [of];
}

/** Move cards to a column. Shows the move at once; the server queues the write. */
export async function move(issues: number[], to: ColumnId) {
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
  selected.value = new Set();
  const made = act(moving.map((c) => ({ issue: c.number, value })), true);
  // From what the server made, so an issue a pull request carried comes back with it.
  undoStack.push(made.then((m) => (m?.length ? m.map((a) => ({ issue: a.issue, previous: a.previous })) : null)));
  await made;
}

/** Send one batch of changes, showing them at once; the actions made, or null if it was
 *  refused. A person pressed the key, so the parked issues a pull request closes go with it
 *  (`carryParked`), as they do in the triage walk — unless `carry` is false, as for an undo. */
async function act(changes: { issue: number; value: number | null }[], carry: boolean): Promise<Action[] | null> {
  const before = new Map(changes.map((c) => [c.issue, cards.value.get(c.issue)?.milestone ?? null]));
  const next = new Map(cards.value);
  for (const c of changes) {
    const card = next.get(c.issue);
    if (card) next.set(c.issue, { ...card, milestone: c.value });
  }
  cards.value = next;
  try {
    const { actions: made } = await api.act(changes.map((c) => (
      { issue: c.issue, kind: 'milestone' as const, value: c.value, carryParked: true, carry })));
    made.forEach(applyAction);
    return made;
  } catch (error) {
    // Refused before anything was queued: put the cards back.
    const restore = new Map(cards.value);
    for (const [issue, previous] of before) {
      const c = restore.get(issue);
      if (c) restore.set(issue, { ...c, milestone: previous });
    }
    cards.value = restore;
    say((error as Error).message);
    return null;
  }
}

/** Undo the last batch of moves made here, once its request is answered. Undo is a move like
 *  any other, so it is written to GitHub too, but it carries nothing: it puts back exactly the
 *  cards the batch moved. One that can't be undone yet stays for another `z`. */
export async function undo() {
  while (undoStack.length) {
    const batch = await undoStack.pop()!;
    if (!batch) continue; // refused, or nothing moved: nothing to take back
    if (!(await putBack(batch))) undoStack.push(Promise.resolve(batch));
    return;
  }
  say('Nothing to undo.');
}

async function putBack(batch: Way[]): Promise<boolean> {
  // A card moved out of a closed milestone goes back only if that is reopened, asked first.
  const closed = new Map<number, Milestone>();
  for (const { previous } of batch) {
    const m = previous === null ? undefined : milestones.value.get(previous);
    if (m?.state === 'closed') closed.set(m.number, m);
  }
  for (const m of closed.values()) {
    if (!confirm(`Undo puts cards back on '${m.title}', which is closed. Reopen it?`)) return false;
    try {
      applyMilestone(await api.ensureMilestone(m.title, true));
    } catch (error) {
      say((error as Error).message);
      return false;
    }
  }
  if (!(await act(batch.map((b) => ({ issue: b.issue, value: b.previous })), false))) return false;
  focused.value = batch[0]!.issue;
  return true;
}

export async function retry(id: number) {
  try { applyAction(await api.retry(id)); } catch (error) { say((error as Error).message); }
}

export async function revert(id: number) {
  try {
    const { action, card } = await api.revert(id);
    applyAction(action);
    applyCard(card);
  } catch (error) { say((error as Error).message); }
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
    applyMilestone(milestone);
    if (!shown.value.includes(milestone.number)) shown.value = [...shown.value, milestone.number];
    await move(issues, milestone.number);
  } catch (error) {
    say((error as Error).message);
  }
}

export async function setDue(number: number, due: string | null) {
  try {
    applyMilestone(await api.setDue(number, due));
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
    focused.value = topCard();
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
