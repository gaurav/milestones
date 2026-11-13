// The board's state and everything a view may do to it. Views read the signals and call the
// functions; nothing else talks to the API.
import { computed, effect, signal } from '@preact/signals';
import { Api, parseLocation } from './api.ts';
import type { Action, BoardData, Card, Kind, Milestone, Move, PlanInfo, Suggestion, Value } from './api.ts';
import {
  DIMENSIONS, NONE, PLAIN_MAPPING, allColumns, columnFor, columnLabel, dimColumns, filtersFrom, group, initialView,
  focusAfter, latestAction, nextFocus, noFilters, unsettled, viewQuery, withValue,
} from './logic.ts';
import type { ColumnId, DimKind, Filters, Layout } from './logic.ts';

const asked = parseLocation(location.search, location.hash);
export const api = new Api(asked.token);

export const loading = signal(true);
export const fatal = signal('');
export const toast = signal('');
export const board = signal<BoardData | null>(null);
export const cards = signal<Map<number, Card>>(new Map());
export const milestones = signal<Map<number, Milestone>>(new Map());
export const actions = signal<Map<number, Action>>(new Map());
export const filters = signal<Filters>(filtersFrom(location.search));
/** The milestone columns ticked under Columns. In the priority and effort views these are
 *  the scope instead: which milestones' cards are being sorted. */
export const shown = signal<ColumnId[]>([]);
/** What the columns are, and what cards are grouped by within a column. */
export const dimension = signal<DimKind>('milestone');
export const groupBy = signal<DimKind[]>([]);
export const focused = signal<number | null>(null);
export const selected = signal<Set<number>>(new Set());
/** Which dialog is open: the milestone picker, or the editor for one kind. */
export const modal = signal<Kind | 'ask' | null>(null);
export const suggestions = signal<Map<number, Suggestion>>(new Map());
export const trayOpen = signal(false);
/** Whether the card panel on the right is open. Collapsed, the columns get its room; the
 *  suggestions tray still opens over it. Saved with the view. */
export const detailOpen = signal(true);
/** Where the plan file is and how many changes it holds. */
export const planInfo = signal<PlanInfo>({ path: null, count: 0 });
/** Whether the read-only notice has been given: once is enough to say it. */
let warned = false;
/** Batches of changes made here, newest last, for `z`. Each entry is one card's way back. */
const undoStack: { issue: number; kind: Kind; previous: Value }[][] = [];

export const layout = computed<Layout>(() => ({
  kind: dimension.value,
  order: board.value?.columns ?? [],
  mapping: board.value?.labelValues ?? PLAIN_MAPPING,
  milestones: [...milestones.value.values()],
}));
/** Every milestone column there is: what the Columns menu lists, in any view. */
export const milestoneColumns = computed<ColumnId[]>(() =>
  board.value ? allColumns(board.value.columns, [...milestones.value.values()], [...cards.value.values()]) : []);
export const columns = computed<ColumnId[]>(() =>
  board.value ? dimColumns(dimension.value, layout.value, [...cards.value.values()]) : []);
/** The milestones being sorted, in the priority and effort views; null when it is all of them. */
export const scope = computed<Set<ColumnId> | null>(() =>
  (shown.value.length === milestoneColumns.value.length ? null : new Set(shown.value)));
export const grouped = computed(() =>
  group([...cards.value.values()], filters.value, layout.value, scope.value, groupBy.value.filter((k) => k !== dimension.value)));
/** How many cards each column holds before the filters, for "12 of 40". */
export const totals = computed(() => {
  const all = group([...cards.value.values()], noFilters, layout.value, scope.value);
  return new Map([...all].map(([id, list]) => [id, list.length]));
});
export const visibleColumns = computed(() =>
  (dimension.value === 'milestone' ? columns.value.filter((id) => shown.value.includes(id)) : columns.value));
export const pending = computed(() => unsettled(actions.value.values()));
export const readOnly = computed(() => board.value?.permissions === 'read');
export const openSuggestions = computed(() => [...suggestions.value.values()].filter((s) => s.status === 'open'));

export function notify(message: string) { say(message); }

export function columnTitle(id: ColumnId, kind: DimKind = dimension.value): string {
  return columnLabel(id, kind, layout.value.milestones);
}

/** The column a card stands in, in the current view. */
export const currentColumn = (card: Card): ColumnId => columnFor(card, dimension.value, layout.value);

export function setDimension(kind: DimKind) {
  dimension.value = kind;
  selected.value = new Set();
  const here = focused.value === null ? undefined : cards.value.get(focused.value);
  // Keep the focus if its card is still on show; otherwise start at the top of the view.
  if (!here || !(grouped.value.get(currentColumn(here)) ?? []).includes(here)) {
    focused.value = visibleColumns.value.map((id) => grouped.value.get(id)?.[0]).find(Boolean)?.number ?? null;
  }
}

export function cycleDimension() {
  const at = DIMENSIONS.findIndex((d) => d.kind === dimension.value);
  setDimension(DIMENSIONS[(at + 1) % DIMENSIONS.length]!.kind);
}

export function selectAll(numbers: number[]) {
  const next = new Set(selected.value);
  // A second click on a group that is wholly selected lets it go.
  if (numbers.every((n) => next.has(n))) numbers.forEach((n) => next.delete(n));
  else numbers.forEach((n) => next.add(n));
  selected.value = next;
  if (numbers.length && (focused.value === null || !numbers.includes(focused.value))) focused.value = numbers[0]!;
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
  suggestions.value = new Map(data.suggestions.map((s) => [s.id, s]));
  planInfo.value = data.plan ?? { path: null, count: 0 };
  if (first) {
    const view = initialView(asked, data.view, milestoneColumns.value, data.milestones);
    shown.value = view.shown;
    dimension.value = view.by;
    groupBy.value = view.group;
    detailOpen.value = view.detail;
    rememberView();
    document.title = `milestones: ${data.repo}`;   // the tool first, so its tabs sort and search together
  }
  if (focused.value === null || !cards.value.has(focused.value)) {
    const top = visibleColumns.value.map((id) => grouped.value.get(id)?.[0]).find(Boolean);
    focused.value = top?.number ?? null;
  }
}

/** Tell the server how the board is being looked at whenever that changes, so the next run
 *  opens the same way. Storage in the browser would not do: each run is on a new port. */
function rememberView() {
  let last = '';
  effect(() => {
    const view = { by: dimension.value, group: groupBy.value, shown: shown.value.filter((id): id is number => typeof id === 'number'), detail: detailOpen.value };
    const now = JSON.stringify(view);
    const first = last === '';
    if (now === last) return;
    last = now;
    if (!first) api.saveView(view).catch(() => {});
  });
  // And say it in the URL, which is what a reload reads first. replaceState, so that ticking a
  // column is not a stop on the way Back; the fragment, where the token is, is kept as it is.
  effect(() => {
    const now = `?${viewQuery({ by: dimension.value, group: groupBy.value, shown: shown.value }, milestoneColumns.value, [...milestones.value.values()], filters.value)}`;
    if (location.search === now) return;
    // Safari refuses more than a hundred of these in thirty seconds, which fast typing in the
    // search box could reach; a URL one keystroke behind is better than an exception.
    try { history.replaceState(null, '', `${location.pathname}${now}${location.hash}`); } catch { /* next change will catch up */ }
  });
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
    suggestion: applySuggestion,
    plan: (p) => { planInfo.value = p; },
    reload: () => { api.board().then((data) => take(data, false)).catch(() => {}); },
  });
}

/** The cards being read again on their own, which the panel shows as busy. */
export const refreshing = signal<Set<number>>(new Set());

/** Read some cards again from GitHub, not the whole repo: for a change made elsewhere, or a
 *  pull request that has merged and should leave the board. If the focused card leaves, the
 *  focus goes to the one below it, as when a move takes it out of its column. */
export async function refreshCards(numbers: number[]) {
  if (!numbers.length) return;
  refreshing.value = new Set([...refreshing.value, ...numbers]);
  const here = focused.value === null ? undefined : cards.value.get(focused.value);
  const before = here ? currentColumn(here) : NONE;
  const list = here ? grouped.value.get(before) ?? [] : [];
  try {
    const data = await api.refresh(numbers);
    take(data, false);
    const gone = data.gone ?? [];
    if (here && gone.includes(here.number)) focused.value = nextFocus(list, new Set(gone), here.number) ?? focused.value;
    const named = (ns: number[]) => ns.map((n) => `#${n}`).join(', ');
    say(gone.length
      ? `${named(gone)} ${gone.length === 1 ? 'is' : 'are'} closed or merged, and off the board now.`
      : `Read ${named(numbers)} from GitHub again.`);
  } catch (error) {
    say((error as Error).message);
  } finally {
    const still = new Set(refreshing.value);
    numbers.forEach((n) => still.delete(n));
    refreshing.value = still;
  }
}

export async function refresh() {
  loading.value = true;
  try { take(await api.refresh(), false); } catch (error) { say((error as Error).message); }
  loading.value = false;
}

/** Record an action, and make its card show what the latest action of that kind says. */
export function applyAction(action: Action) {
  const all = new Map(actions.value).set(action.id, action);
  actions.value = all;
  const card = cards.value.get(action.issue);
  if (!card || action.kind === 'comment') return;
  const latest = latestAction(all.values(), action.issue, action.kind);
  // No action left standing means the reverted one was the only one: back to before it.
  const value = latest ? latest.value : action.previous;
  cards.value = new Map(cards.value).set(card.number, withValue(card, action.kind, value, board.value?.labels ?? []));
}

export function applySuggestion(suggestion: Suggestion) {
  const next = new Map(suggestions.value);
  if (suggestion.status === 'open' || suggestion.status === 'accepted') next.set(suggestion.id, suggestion);
  else next.delete(suggestion.id);
  suggestions.value = next;
}

/** The focused card, or the whole selection when the focused card is part of it. */
export function targets(of: number | null = focused.value): number[] {
  if (of === null) return [];
  return selected.value.has(of) ? [...selected.value] : [of];
}

function remember(made: Action[]) {
  const back = made.filter((a) => a.kind !== 'comment').map((a) => ({ issue: a.issue, kind: a.kind, previous: a.previous }));
  if (back.length) undoStack.push(back);
}

/** Send changes to the server, which applies them and queues the writes; show the result. */
async function submit(moves: Move[], undoable = true, advance = true): Promise<boolean> {
  if (moves.length === 0) return true;
  // Where the focused card stands now, to tell afterwards whether the change took it away.
  const here = focused.value === null ? undefined : cards.value.get(focused.value);
  const before = here ? currentColumn(here) : NONE;
  const list = here ? grouped.value.get(before) ?? [] : [];
  try {
    const made = await api.act(moves);
    made.forEach(applyAction);
    if (undoable) remember(made);
    if (readOnly.value && made.length && !warned) {
      warned = true;
      say('Read-only: this was not written to GitHub. It is kept in the plan file, and a writable board can apply it.');
    }
    const now = here ? cards.value.get(here.number) : undefined;
    const after = grouped.value.get(before) ?? [];
    if (advance && now && (currentColumn(now) !== before || !after.some((c) => c.number === now.number))) {
      // It left its column (or the view): carry on with the card that was below it and is
      // still there, so a run of keys walks down a column.
      focused.value = focusAfter(list, after, now.number) ?? now.number;
    }
    return true;
  } catch (error) {
    say((error as Error).message);
    return false;
  }
}

/** Move cards to a column. */
export async function move(issues: number[], to: ColumnId) {
  const value = to === NONE ? null : (to as number);
  const moving = issues.map((n) => cards.value.get(n)).filter((c): c is Card => !!c && c.milestone !== value);
  if (moving.length === 0) return;
  const target = milestones.value.get(to as number);
  if (to !== NONE && target?.state !== 'open') {
    say(`'${columnTitle(to, 'milestone')}' is closed. Press m to reopen it and move there.`);
    return;
  }
  selected.value = new Set();
  await submit(moving.map((c) => ({ issue: c.number, kind: 'milestone' as const, value })));
}

/** Put cards in a column of the current view: a milestone, a priority or an effort. */
export async function place(issues: number[], to: ColumnId) {
  if (dimension.value === 'milestone') return move(issues, to);
  const kind = dimension.value;
  const placing = issues.map((n) => cards.value.get(n)).filter((c): c is Card => !!c && currentColumn(c) !== to);
  selected.value = new Set();
  await submit(placing.map((c) => ({ issue: c.number, kind, value: to === NONE ? null : to })));
}

/** Change one kind on some cards: a priority, a title, the labels… */
export async function change(issues: number[], kind: Kind, value: Move['value']) {
  await submit(issues.map((issue) => ({ issue, kind, value })));
}

/** Undo the last batch of changes made here. Undo is a change like any other, so it is
 *  written to GitHub too. */
export async function undo() {
  const batch = undoStack.pop();
  if (!batch) { say('Nothing to undo.'); return; }
  await submit(batch.map((b) => ({ issue: b.issue, kind: b.kind, value: b.previous })).reverse(), false, false);
  focused.value = batch[0]!.issue;
}

export async function resolve(ids: number[], decision: 'accept' | 'reject') {
  if (ids.length === 0) return;
  try {
    const result = await api.resolve(ids, decision);
    result.actions.forEach(applyAction);
    result.suggestions.forEach(applySuggestion);
    remember(result.actions);
  } catch (error) {
    say((error as Error).message);
  }
}

/** On a writable board: turn the plan a read-only session left into suggestions. */
export async function reviewPlan() {
  try {
    const result = await api.reviewPlan();
    result.suggestions.forEach(applySuggestion);
    trayOpen.value = result.suggestions.length > 0;
    say(`${result.suggestions.length} planned change${result.suggestions.length === 1 ? '' : 's'} to review under Suggestions`
      + (result.dropped ? `; ${result.dropped} left out as already made or no longer possible.` : '.'));
  } catch (error) {
    say((error as Error).message);
  }
}

export async function discardPlan() {
  try { await api.discardPlan(); } catch (error) { say((error as Error).message); }
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
  const col = currentColumn(current);
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
