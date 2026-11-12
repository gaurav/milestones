// Pure board logic: what a view needs to decide, with no DOM and no signals, so it can be
// tested under `node --test` and reused by a view that looks nothing like the first.
import type { Action, Card, Kind, Label, Milestone, Suggestion, Value } from './api.ts';

/** A column is a milestone's number, or NONE for the issues that have no milestone. */
export const NONE = 0;
export type ColumnId = number;
export const columnOf = (card: Card): ColumnId => card.milestone ?? NONE;

export interface Filters {
  text: string;
  label: string;
  priority: string;
  kind: '' | 'issue' | 'pr';
  hasPr: boolean;
  unlabelled: boolean;
}
export const noFilters: Filters = { text: '', label: '', priority: '', kind: '', hasPr: false, unlabelled: false };

/** The priority someone recorded, wherever they recorded it: the organisation's field
 *  first, then a `Priority: X` label. Slice 2 turns a disagreement into a suggestion. */
export function priorityOf(card: Card): string {
  const field = card.fields?.['Priority'];
  if (typeof field === 'string' && field) return field;
  const label = card.labels.find((l) => /^priority:/i.test(l.name));
  return label ? label.name.replace(/^priority:\s*/i, '') : '';
}

export function effortOf(card: Card): string {
  const field = card.fields?.['Effort'];
  if (typeof field === 'string' && field) return field;
  const label = card.labels.find((l) => /^size:/i.test(l.name));
  return label ? label.name.replace(/^size:\s*/i, '') : '';
}

export const linkedPrs = (card: Card) => [...(card.closingPrs ?? []), ...(card.mentionedPrs ?? [])];

export function matches(card: Card, f: Filters): boolean {
  if (f.kind && card.kind !== f.kind) return false;
  if (f.label && !card.labels.some((l) => l.name === f.label)) return false;
  if (f.priority && priorityOf(card).toLowerCase() !== f.priority.toLowerCase()) return false;
  if (f.hasPr && linkedPrs(card).length === 0) return false;
  if (f.unlabelled && card.labels.length > 0) return false;
  const text = f.text.trim().toLowerCase();
  if (!text) return true;
  if (/^#?\d+$/.test(text)) return String(card.number).startsWith(text.replace('#', ''));
  const hay = `${card.title}\n${card.labels.map((l) => l.name).join('\n')}\n${card.author ?? ''}\n${card.bodyHTML}`;
  return text.split(/\s+/).every((word) => hay.toLowerCase().includes(word));
}

/** Every column the board can show, in order: no milestone, the open milestones as the
 *  server ordered them, then any closed milestone that still has open work on it. */
export function allColumns(columns: number[], milestones: Milestone[], cards: Card[]): ColumnId[] {
  const stranded = milestones
    .filter((m) => m.state === 'closed' && cards.some((c) => c.milestone === m.number))
    .map((m) => m.number);
  return [NONE, ...columns, ...stranded];
}

/** The columns `--from` asked for, by title (`none` for no milestone); all of them if it
 *  asked for nothing, or for nothing that exists. */
export function initialColumns(all: ColumnId[], milestones: Milestone[], from: string[]): ColumnId[] {
  if (from.length === 0) return all;
  const wanted = new Set(from.map((t) => t.toLowerCase()));
  const title = (id: ColumnId) => (id === NONE ? 'none' : milestones.find((m) => m.number === id)?.title ?? '');
  const shown = all.filter((id) => wanted.has(title(id).toLowerCase()));
  return shown.length ? shown : all;
}

/** Cards by column, freshest first, after the filters. */
export function group(cards: Card[], f: Filters): Map<ColumnId, Card[]> {
  const groups = new Map<ColumnId, Card[]>();
  for (const card of cards) {
    if (!matches(card, f)) continue;
    const id = columnOf(card);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id)!.push(card);
  }
  for (const list of groups.values()) list.sort((a, b) => b.updated.localeCompare(a.updated));
  return groups;
}

/** Where focus goes when the focused card leaves its column: the card that was below it,
 *  else the one above, so that a run of number keys walks down a column. */
export function nextFocus(column: Card[], leaving: Set<number>, focused: number): number | null {
  const at = column.findIndex((c) => c.number === focused);
  if (at < 0) return null;
  for (let i = at + 1; i < column.length; i++) if (!leaving.has(column[i]!.number)) return column[i]!.number;
  for (let i = at - 1; i >= 0; i--) if (!leaving.has(column[i]!.number)) return column[i]!.number;
  return null;
}

/** The action that decides what a card shows for a kind: the latest one that wasn't reverted. */
export function latestAction(actions: Iterable<Action>, issue: number, kind: Kind): Action | undefined {
  let latest: Action | undefined;
  for (const a of actions) {
    if (a.issue === issue && a.kind === kind && a.status !== 'reverted' && (!latest || a.id > latest.id)) latest = a;
  }
  return latest;
}

/** The one action a card's status line is about: a failed write before one in flight
 *  before one a read-only run didn't make, the newest of each. */
export function cardStatus(actions: Iterable<Action>, issue: number): Action | undefined {
  const rank = { failed: 3, pending: 2, would: 1, done: 0, reverted: 0 } as const;
  let worst: Action | undefined;
  for (const a of actions) {
    if (a.issue !== issue || rank[a.status] === 0) continue;
    if (!worst || rank[a.status] > rank[worst.status] || (rank[a.status] === rank[worst.status] && a.id > worst.id)) worst = a;
  }
  return worst;
}

const FIELD: Partial<Record<Kind, string>> = { priority: 'Priority', effort: 'Effort' };

/** What a card holds for an action kind, in the shape an action's value takes. */
export function cardValue(card: Card, kind: Kind): Value {
  if (kind === 'milestone') return card.milestone;
  if (kind === 'title') return card.title;
  if (kind === 'labels') return card.labels.map((l) => l.name);
  if (kind === 'state') return card.closed ?? null;
  const field = FIELD[kind];
  return field ? (card.fields?.[field] as string | undefined) ?? null : null;
}

/** A copy of the card with one kind's value changed. `known` gives a label its colour. */
export function withValue(card: Card, kind: Kind, value: Value, known: Label[]): Card {
  if (kind === 'milestone') return { ...card, milestone: value as number | null };
  if (kind === 'title') return { ...card, title: value as string };
  if (kind === 'state') return { ...card, closed: value as string | null };
  if (kind === 'labels') {
    const colour = new Map(known.map((l) => [l.name, l.color]));
    return { ...card, labels: (value as string[]).map((name) => ({ name, color: colour.get(name) ?? 'ededed' })) };
  }
  const field = FIELD[kind];
  if (!field) return card;
  const fields = { ...card.fields };
  if (value === null) delete fields[field]; else fields[field] = value as string;
  return { ...card, fields };
}

/** A suggestion or an action in a few words: "Set Priority to High", "Remove label 'x'". */
export function describe(kind: Kind, value: Suggestion['value'], milestones: Map<number, Milestone>): string {
  if (kind === 'milestone') return value === null ? 'Take off its milestone' : `Move to ${milestones.get(value as number)?.title ?? `milestone ${value}`}`;
  if (kind === 'priority' || kind === 'effort') return value === null ? `Clear ${FIELD[kind]}` : `Set ${FIELD[kind]} to ${value}`;
  if (kind === 'title') return `Retitle: ${value}`;
  if (kind === 'comment') return `Comment: ${value}`;
  if (kind === 'state') return value === null ? 'Reopen' : `Close as ${String(value).replace('_', ' ')}`;
  if (Array.isArray(value)) return `Set labels to ${value.length ? value.join(', ') : 'none'}`;
  const change = value as { add?: string[]; remove?: string[] };
  const parts = [];
  if (change.add?.length) parts.push(`Add label${change.add.length > 1 ? 's' : ''} ${change.add.join(', ')}`);
  if (change.remove?.length) parts.push(`Remove label${change.remove.length > 1 ? 's' : ''} ${change.remove.join(', ')}`);
  return parts.join('; ') || 'No change';
}

export const unsettled = (actions: Iterable<Action>) =>
  [...actions].filter((a) => a.status === 'pending' || a.status === 'failed');

/** "3d", "5mo", "2y": how long ago, coarsely — a card has room for no more. */
export function age(iso: string, now = Date.now()): string {
  const days = Math.max(0, Math.floor((now - Date.parse(iso)) / 86_400_000));
  if (days < 1) return 'today';
  if (days < 60) return `${days}d`;
  if (days < 730) return `${Math.floor(days / 30)}mo`;
  return `${Math.floor(days / 365)}y`;
}

/** Black or white, whichever reads on a label's colour. */
export function inkOn(hex: string): string {
  const n = parseInt(hex, 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#000' : '#fff';
}

/** Milestones for the picker: those matching the typed text, open ones first. */
export function pickerMatches(milestones: Milestone[], order: number[], text: string): Milestone[] {
  const words = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rank = (m: Milestone) => (m.state === 'open' ? order.indexOf(m.number) : order.length);
  return milestones
    .filter((m) => words.every((w) => m.title.toLowerCase().includes(w)))
    .sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title));
}
