// Pure board logic: what a view needs to decide, with no DOM and no signals, so it can be
// tested under `node --test` and reused by a view that looks nothing like the first.
import type { Action, Card, Kind, Label, Milestone, Suggestion, Value } from './api.ts';

/** What the columns are: a milestone each, or one per priority, or one per effort. Each is
 *  an action kind, so putting a card in a column is the action `{kind, value}`. */
export type DimKind = 'milestone' | 'priority' | 'effort';
export const DIMENSIONS: { kind: DimKind; title: string }[] = [
  { kind: 'milestone', title: 'Milestone' }, { kind: 'priority', title: 'Priority' }, { kind: 'effort', title: 'Effort' },
];

/** A column: a milestone's number, a priority's or effort's name, or NONE for the cards
 *  that have no value in the dimension — no milestone, priority unset. */
export const NONE = 0;
export type ColumnId = number | string;

/** What a label says about a kind, as the server's reconcile table has it. */
export interface LabelValues { field: string; prefix: string; values: Record<string, string> }
export type Mapping = Partial<Record<'priority' | 'effort', LabelValues>>;
/** Enough to read a label's raw text when a server sends no table. */
export const PLAIN_MAPPING: Mapping = {
  priority: { field: 'Priority', prefix: 'priority', values: {} },
  effort: { field: 'Effort', prefix: '(?:size|effort)', values: {} },
};
const DEFAULT_OPTIONS = { priority: ['Urgent', 'High', 'Medium', 'Low'], effort: ['High', 'Medium', 'Low'] };

export interface Filters {
  text: string;
  label: string;
  priority: string;
  kind: '' | 'issue' | 'pr';
  hasPr: boolean;
  unlabelled: boolean;
}
export const noFilters: Filters = { text: '', label: '', priority: '', kind: '', hasPr: false, unlabelled: false };

/** A card's priority or effort wherever it was recorded, and where: the organisation's
 *  field first, then a label, read in the field's terms ("Priority: Critical" is Urgent,
 *  "Size: M" is Medium). A label the table doesn't know is given as written. */
export function effectiveValue(card: Card, kind: 'priority' | 'effort', mapping: Mapping = PLAIN_MAPPING): { value: string; from: 'field' | 'label' | '' } {
  const spec = mapping[kind] ?? PLAIN_MAPPING[kind]!;
  const field = card.fields?.[spec.field];
  if (typeof field === 'string' && field) return { value: field, from: 'field' };
  const pattern = new RegExp(`^${spec.prefix}\\s*:\\s*(.+)$`, 'i');
  for (const label of card.labels) {
    const raw = pattern.exec(label.name.trim())?.[1]?.trim();
    if (raw) return { value: spec.values[raw.toLowerCase()] ?? raw, from: 'label' };
  }
  return { value: '', from: '' };
}

export const priorityOf = (card: Card, mapping?: Mapping) => effectiveValue(card, 'priority', mapping).value;
export const effortOf = (card: Card, mapping?: Mapping) => effectiveValue(card, 'effort', mapping).value;

export const linkedPrs = (card: Card) => [...(card.closingPrs ?? []), ...(card.mentionedPrs ?? [])];

export function matches(card: Card, f: Filters, mapping?: Mapping): boolean {
  if (f.kind && card.kind !== f.kind) return false;
  if (f.label && !card.labels.some((l) => l.name === f.label)) return false;
  if (f.priority && priorityOf(card, mapping).toLowerCase() !== f.priority.toLowerCase()) return false;
  if (f.hasPr && linkedPrs(card).length === 0) return false;
  if (f.unlabelled && card.labels.length > 0) return false;
  const text = f.text.trim().toLowerCase();
  if (!text) return true;
  if (/^#?\d+$/.test(text)) return String(card.number).startsWith(text.replace('#', ''));
  const hay = `${card.title}\n${card.labels.map((l) => l.name).join('\n')}\n${card.author ?? ''}\n${card.bodyHTML}`;
  return text.split(/\s+/).every((word) => hay.toLowerCase().includes(word));
}

/** Every milestone column the board can show, in order: no milestone, the open milestones
 *  as the server ordered them, then any closed milestone that still has open work on it. */
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

/** What a view needs to know to lay cards out: which dimension the columns are, the
 *  board's milestone order and fields, and the label table. */
export interface Layout {
  kind: DimKind;
  /** Open milestones' numbers in order, from the server. */
  order: number[];
  /** The owner's single-select fields and their options. */
  fields: Record<string, string[]>;
  mapping: Mapping;
  milestones: Milestone[];
}

const FIELD_OF = { priority: 'Priority', effort: 'Effort' } as const;

/** Whether the owner has the issue field a dimension writes to. Without it the value lives
 *  in a label, which a pull request can carry; with it, a pull request has nowhere to. */
export const hasField = (kind: DimKind, fields: Record<string, string[]>) => kind !== 'milestone' && FIELD_OF[kind] in fields;

/** A dimension's columns in order, the unset one first. */
export function dimColumns(kind: DimKind, layout: Layout, cards: Card[]): ColumnId[] {
  if (kind === 'milestone') return allColumns(layout.order, layout.milestones, cards);
  return [NONE, ...(layout.fields[FIELD_OF[kind]] ?? DEFAULT_OPTIONS[kind])];
}

/** The column a card stands in for a dimension. A priority or effort that isn't one of the
 *  options — a label the table doesn't know — is unset as far as the columns go. */
export function columnFor(card: Card, kind: DimKind, layout: Layout): ColumnId {
  if (kind === 'milestone') return card.milestone ?? NONE;
  const value = effectiveValue(card, kind, layout.mapping).value.toLowerCase();
  const options = layout.fields[FIELD_OF[kind]] ?? DEFAULT_OPTIONS[kind];
  return options.find((o) => o.toLowerCase() === value) ?? NONE;
}

export function columnLabel(id: ColumnId, kind: DimKind, milestones: Milestone[]): string {
  if (kind === 'milestone') return id === NONE ? 'No milestone' : milestones.find((m) => m.number === id)?.title ?? `Milestone ${id}`;
  return id === NONE ? (kind === 'priority' ? 'No priority' : 'No effort') : String(id);
}

/** Whether a card belongs in a view at all. Sorting by priority or effort is done over the
 *  milestones in `scope` (null for all of them), and leaves out pull requests where the value
 *  would have to go in a field a pull request hasn't got. */
export function inView(card: Card, layout: Layout, scope: Set<ColumnId> | null): boolean {
  if (layout.kind === 'milestone') return true;
  if (card.kind === 'pr' && hasField(layout.kind, layout.fields)) return false;
  return scope === null || scope.has(card.milestone ?? NONE);
}

/** Where a card sorts within its column: by each `groupBy` dimension in turn, unset last,
 *  so the cards someone has already judged come first. */
function groupRank(card: Card, groupBy: DimKind[], layout: Layout, cards: Card[]): number[] {
  return groupBy.map((kind) => {
    const at = dimColumns(kind, layout, cards).indexOf(columnFor(card, kind, layout));
    return at <= 0 ? Number.MAX_SAFE_INTEGER : at;
  });
}

/** Cards by column after the filters: grouped as `groupBy` says, freshest first within a
 *  group. The order is the order they are drawn in and walked in. */
export function group(cards: Card[], f: Filters, layout: Layout, scope: Set<ColumnId> | null = null, groupBy: DimKind[] = []): Map<ColumnId, Card[]> {
  const groups = new Map<ColumnId, Card[]>();
  const rank = new Map<number, number[]>();
  for (const card of cards) {
    if (!inView(card, layout, scope) || !matches(card, f, layout.mapping)) continue;
    const id = columnFor(card, layout.kind, layout);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id)!.push(card);
    if (groupBy.length) rank.set(card.number, groupRank(card, groupBy, layout, cards));
  }
  for (const list of groups.values()) {
    list.sort((a, b) => {
      const [ra, rb] = [rank.get(a.number) ?? [], rank.get(b.number) ?? []];
      for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i]! - rb[i]!;
      return b.updated.localeCompare(a.updated);
    });
  }
  return groups;
}

/** A column's cards cut into its groups, each with a heading like "High · Medium". One
 *  group with no heading when nothing is grouped. */
export function sections(list: Card[], groupBy: DimKind[], layout: Layout): { label: string; cards: Card[] }[] {
  if (groupBy.length === 0) return list.length ? [{ label: '', cards: list }] : [];
  const out: { label: string; cards: Card[] }[] = [];
  for (const card of list) {
    const label = groupBy.map((kind) => columnLabel(columnFor(card, kind, layout), kind, layout.milestones)).join(' · ');
    if (out.at(-1)?.label === label) out.at(-1)!.cards.push(card);
    else out.push({ label, cards: [card] });
  }
  return out;
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

/** What to paste into a coding agent's session to have it weigh in on some cards: where
 *  each stands now, what you think, and how it should answer. The agent has the repository;
 *  this gives it the question. */
export function askPrompt(repo: string, asked: Card[], milestones: Map<number, Milestone>, view: string, mapping?: Mapping): string {
  const lines = asked.map((c) => {
    const where = c.milestone === null ? 'no milestone' : milestones.get(c.milestone)?.title ?? `milestone ${c.milestone}`;
    const labels = c.labels.map((l) => l.name).join(', ') || 'none';
    return `- ${repo}#${c.number} "${c.title}"\n  ${c.url}\n  Now: ${where}; priority ${priorityOf(c, mapping) || 'not set'}; effort ${effortOf(c, mapping) || 'not set'}; labels: ${labels}.`;
  });
  const one = asked.length === 1;
  return [
    `Help me triage ${one ? 'this issue' : `these ${asked.length} issues`}:`,
    ...lines,
    '',
    `What I think: ${view.trim() || '(nothing yet — tell me what you find)'}`,
    '',
    `Read ${one ? 'the issue, its thread' : 'each issue, its thread'} and the code it concerns before answering. If I have misread ${one ? 'it' : 'one'}, or the work is bigger or riskier than I think, or it overlaps another issue, tell me that first and say why.`,
    `When we agree, propose the changes with \`milestones suggest ${one ? asked[0]!.number : 'N'} ... --reason "..." --source <your model>\` — they appear on my board and I accept or reject each one. Nothing is written to GitHub until I accept. Run \`milestones board --brief\` for the rules and the full syntax.`,
  ].join('\n');
}

/** Milestones for the picker: those matching the typed text, open ones first. */
export function pickerMatches(milestones: Milestone[], order: number[], text: string): Milestone[] {
  const words = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rank = (m: Milestone) => (m.state === 'open' ? order.indexOf(m.number) : order.length);
  return milestones
    .filter((m) => words.every((w) => m.title.toLowerCase().includes(w)))
    .sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title));
}
