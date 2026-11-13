import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseLocation } from '../ui/shared/api.ts';
import type { Action, Card, Milestone } from '../ui/shared/api.ts';
import type { DimKind, Layout, Mapping } from '../ui/shared/logic.ts';
import {
  NONE, age, allColumns, askPrompt, cardStatus, cardValue, columnFor, columnLabel, describe, dimColumns,
  effectiveValue, effortOf, estimatedEffort, filtersFrom, group, inView, initialColumns, keyedOptions, optionForKey, initialView, inkOn, isValueLabel, latestAction, matches, nextFocus,
  noFilters, pickerMatches, priorityOf, sections, shownByLayout, sizeOf, unsettled, viewQuery, withValue,
} from '../ui/shared/logic.ts';

const card = (number: number, extra: Partial<Card> = {}): Card => ({
  id: `I${number}`, number, kind: 'issue', title: `Issue ${number}`, url: '', bodyHTML: '', created: '2026-01-01T00:00:00Z',
  updated: `2026-01-${String(number).padStart(2, '0')}T00:00:00Z`, author: 'a', comments: 0, assignees: [], labels: [],
  milestone: null, ...extra,
});
const milestone = (number: number, title: string, state: 'open' | 'closed' = 'open'): Milestone =>
  ({ number, title, state, due: null, description: '', url: '' });
const action = (id: number, issue: number, status: Action['status'], value: number | null = 1): Action =>
  ({ id, issue, kind: 'milestone' as const, value, previous: null, carriedBy: null, error: null, status });

test('the URL carries the token in its fragment and the view asked for in its query', () => {
  assert.deepEqual(parseLocation('?from=Critical&from=Babel%20v1.19', '#t=abc'), { token: 'abc', from: ['Critical', 'Babel v1.19'], by: '', group: [] });
  assert.deepEqual(parseLocation('?by=priority&group=priority,effort', '#t=a'), { token: 'a', from: [], by: 'priority', group: ['priority', 'effort'] });
  assert.deepEqual(parseLocation('', ''), { token: '', from: [], by: '', group: [] });
  // A token in the query string is not one: it would have been sent to the server.
  assert.equal(parseLocation('?t=abc', '').token, '');
});

test('priority and effort are read from the standard label first, then another label, then the field', () => {
  assert.equal(priorityOf(card(1, { fields: { Priority: 'High' }, labels: [{ name: 'Priority: Medium', color: 'fff' }] })), 'Medium');
  assert.equal(priorityOf(card(1, { fields: { Priority: 'High' } })), 'High');
  assert.equal(priorityOf(card(1)), '');
  assert.equal(effortOf(card(1, { labels: [{ name: 'Size: M', color: 'fff' }] })), 'M');
  assert.equal(effortOf(card(1, { fields: { Effort: 'Low' } })), 'Low');   // no table: as written
});

test('filters combine, and a bare number matches the issue number', () => {
  const c = card(1171, { title: 'Non-human insulins', labels: [{ name: 'bug', color: 'fff' }], closingPrs: [] });
  assert.ok(matches(c, noFilters));
  assert.ok(matches(c, { ...noFilters, text: 'insulin human' }));
  assert.ok(!matches(c, { ...noFilters, text: 'insulin pig' }));
  assert.ok(matches(c, { ...noFilters, text: '#117' }));
  assert.ok(!matches(c, { ...noFilters, text: '99' }));
  assert.ok(matches(c, { ...noFilters, label: 'bug' }));
  assert.ok(!matches(c, { ...noFilters, unlabelled: true }));
  assert.ok(!matches(c, { ...noFilters, hasPr: true }));
  assert.ok(!matches(c, { ...noFilters, kind: 'pr' }));
  assert.ok(matches(c, { ...noFilters, author: 'a' }));
  assert.ok(!matches(c, { ...noFilters, author: 'someone-else' }));
});

test('columns are no milestone, the open ones in server order, then closed ones with work', () => {
  const ms = [milestone(1, 'Soon'), milestone(2, 'v1'), milestone(3, 'Old', 'closed'), milestone(4, 'Empty old', 'closed')];
  const cs = [card(1, { milestone: 3 }), card(2, { milestone: 1 })];
  assert.deepEqual(allColumns([2, 1], ms, cs), [NONE, 2, 1, 3]);
});

test('--from picks columns by title, and falls back to all of them', () => {
  const ms = [milestone(1, 'Critical'), milestone(2, 'Babel v1.19')];
  assert.deepEqual(initialColumns([NONE, 1, 2], ms, ['critical', 'none']), [NONE, 1]);
  assert.deepEqual(initialColumns([NONE, 1, 2], ms, []), [NONE, 1, 2]);
  assert.deepEqual(initialColumns([NONE, 1, 2], ms, ['nope']), [NONE, 1, 2]);
});

const MAPPING: Mapping = {
  priority: { field: 'Priority', prefix: 'priority', options: ['Urgent', 'High', 'Medium', 'Low'],
    values: { critical: 'Urgent', urgent: 'Urgent', high: 'High', medium: 'Medium', low: 'Low' }, aliases: { p0: 'Urgent' } },
  effort: { field: 'Effort', prefix: '(?:size|effort)', options: ['XS', 'S', 'M', 'L', 'XL'],
    values: { xs: 'XS', s: 'S', m: 'M', l: 'L', xl: 'XL', low: 'S', medium: 'M', high: 'L' } },
};
const layout = (kind: DimKind): Layout =>
  ({ kind, order: [7, 8], mapping: MAPPING, milestones: [milestone(7, 'Critical'), milestone(8, 'v1')] });
const label = (name: string) => ({ name, color: 'ffffff' });

test('cards group by milestone column, freshest first', () => {
  const groups = group([card(1), card(3), card(2, { milestone: 7 })], noFilters, layout('milestone'));
  assert.deepEqual(groups.get(NONE)!.map((c) => c.number), [3, 1]);
  assert.deepEqual(groups.get(7)!.map((c) => c.number), [2]);
});

test('a value is read from its standard label, else another label, else the field, as an option', () => {
  const read = (extra: Partial<Card>, kind: 'priority' | 'effort' = 'priority') => effectiveValue(card(1, extra), kind, MAPPING);
  assert.deepEqual(read({ fields: { Priority: 'High' }, labels: [label('Priority: Low')] }), { value: 'Low', from: 'label' });
  // The standard label wins over an older one, wherever it comes in the list.
  assert.deepEqual(read({ labels: [label('Priority: Critical'), label('priority: low')] }), { value: 'Low', from: 'label' });
  assert.deepEqual(read({ labels: [label('priority:critical')] }), { value: 'Urgent', from: 'other' });
  assert.deepEqual(read({ labels: [label('P0')] }), { value: 'Urgent', from: 'other' });   // the repo's own label
  assert.deepEqual(read({ labels: [label('Size: M')] }, 'effort'), { value: 'M', from: 'other' });
  assert.deepEqual(read({ labels: [label('Effort: M')] }, 'effort'), { value: 'M', from: 'label' });
  assert.deepEqual(read({ labels: [label('Effort: Low')] }, 'effort'), { value: 'S', from: 'other' });
  assert.deepEqual(read({ fields: { Effort: 'High' } }, 'effort'), { value: 'L', from: 'field' });
  assert.deepEqual(read({ labels: [label('Priority: Someday')] }), { value: 'Someday', from: 'other' });
  assert.deepEqual(read({ labels: [label('bug')] }), { value: '', from: '' });
});

test('each dimension has its columns, the unset one first', () => {
  assert.deepEqual(dimColumns('milestone', layout('milestone'), []), [NONE, 7, 8]);
  assert.deepEqual(dimColumns('priority', layout('priority'), []), [NONE, 'Urgent', 'High', 'Medium', 'Low']);
  assert.deepEqual(dimColumns('effort', { ...layout('effort'), mapping: {} }, []), [NONE, 'XS', 'S', 'M', 'L', 'XL']);   // no table: the defaults
  assert.equal(columnLabel(NONE, 'priority', []), 'No priority');
  assert.equal(columnLabel('High', 'effort', []), 'High');
  assert.equal(columnLabel(7, 'milestone', [milestone(7, 'Critical')]), 'Critical');
});

test('a card stands in the column of its effective value, or unset', () => {
  const l = layout('priority');
  assert.equal(columnFor(card(1, { fields: { Priority: 'High' } }), 'priority', l), 'High');
  assert.equal(columnFor(card(1, { labels: [label('Priority: Critical')] }), 'priority', l), 'Urgent');
  assert.equal(columnFor(card(1, { labels: [label('Priority: Someday')] }), 'priority', l), NONE);
  assert.equal(columnFor(card(1), 'priority', l), NONE);
  assert.equal(columnFor(card(1, { milestone: 7 }), 'milestone', l), 7);
});

test('the priority view is scoped by milestone, pull requests included', () => {
  const cs = [card(1, { milestone: 7, fields: { Priority: 'High' } }), card(2, { milestone: 8 }), card(3),
    card(4, { milestone: 7, kind: 'pr', labels: [label('Priority: High')] })];
  const scoped = group(cs, noFilters, layout('priority'), new Set([7, NONE]));
  assert.deepEqual([...scoped].map(([id, list]) => [id, list.map((c) => c.number)]), [['High', [4, 1]], [NONE, [3]]]);
  assert.ok(inView(cs[3]!, layout('milestone'), new Set([8])));   // the milestone view shows everything
});

test('cards group within a column, judged ones first, and cut into headed sections', () => {
  const cs = [card(1), card(2, { fields: { Priority: 'Low' } }), card(3, { labels: [label('Priority: Urgent'), label('Effort: L')] }),
    card(4, { labels: [label('Priority: Urgent'), label('Effort: S')] }), card(5, { labels: [label('Priority: Urgent'), label('Effort: S')] })];
  const l = layout('milestone');
  const list = group(cs, noFilters, l, null, ['priority', 'effort']).get(NONE)!;
  assert.deepEqual(list.map((c) => c.number), [5, 4, 3, 2, 1]);
  assert.deepEqual(sections(list, ['priority', 'effort'], l).map((s) => [s.label, s.cards.length]),
    [['Urgent · S', 2], ['Urgent · L', 1], ['Low · No effort', 1], ['No priority · No effort', 1]]);
  assert.deepEqual(sections(list, [], l).map((s) => [s.label, s.cards.length]), [['', 5]]);
  assert.deepEqual(sections([], ['priority'], l), []);
});

test('focus moves to the card below the one leaving, else the one above', () => {
  const column = [card(1), card(2), card(3)];
  assert.equal(nextFocus(column, new Set([2]), 2), 3);
  assert.equal(nextFocus(column, new Set([3]), 3), 2);
  assert.equal(nextFocus(column, new Set([2, 3]), 2), 1);
  assert.equal(nextFocus(column, new Set([1, 2, 3]), 2), null);
});

test('the latest action that was not reverted decides a card', () => {
  const all = [action(1, 5, 'done', 1), action(2, 5, 'failed', 2), action(3, 5, 'reverted', 3), action(4, 6, 'pending', 9)];
  assert.equal(latestAction(all, 5, 'milestone')!.id, 2);
  assert.equal(latestAction(all, 5, 'title'), undefined);
  assert.equal(latestAction(all, 7, 'milestone'), undefined);
  assert.deepEqual(unsettled(all).map((a) => a.id), [2, 4]);
});

test('a card reports its worst unsettled action', () => {
  const all = [action(1, 5, 'done'), action(2, 5, 'would'), action(3, 5, 'pending'), action(4, 5, 'failed'), action(5, 5, 'pending')];
  assert.equal(cardStatus(all, 5)!.id, 4);
  assert.equal(cardStatus([action(1, 5, 'done'), action(2, 5, 'reverted')], 5), undefined);
});

test('a card value is read and written by kind', () => {
  const known = [{ name: 'bug', color: 'ff0000' }];
  let c = card(1, { labels: [{ name: 'bug', color: 'ff0000' }], fields: { Priority: 'High' } });
  assert.equal(cardValue(c, 'priority-field'), 'High');
  assert.equal(cardValue(c, 'effort-field'), null);
  assert.deepEqual(cardValue(c, 'labels'), ['bug']);
  c = withValue(c, 'priority-field', null, known);
  assert.deepEqual(c.fields, {});
  c = withValue(c, 'labels', ['bug', 'new'], known);
  assert.deepEqual(c.labels, [{ name: 'bug', color: 'ff0000' }, { name: 'new', color: 'ededed' }]);
  assert.equal(withValue(c, 'state', 'not_planned', known).closed, 'not_planned');
  assert.equal(withValue(c, 'milestone', 7, known).milestone, 7);
});

test('changes are described in a few words', () => {
  const ms = new Map([[7, milestone(7, 'Needed soon')]]);
  assert.equal(describe('milestone', 7, ms), 'Move to Needed soon');
  assert.equal(describe('milestone', null, ms), 'Take off its milestone');
  assert.equal(describe('priority', 'High', ms), 'Set Priority to High');
  assert.equal(describe('effort', null, ms), 'Clear Effort');
  assert.equal(describe('priority-field', null, ms), 'Clear the Priority field');
  assert.equal(describe('state', 'not_planned', ms), 'Close as not planned');
  assert.equal(describe('labels', { add: ['a'], remove: ['b', 'c'] }, ms), 'Add label a; Remove labels b, c');
  assert.equal(describe('labels', [], ms), 'Set labels to none');
});

test('ages are coarse', () => {
  const now = Date.parse('2026-11-11T00:00:00Z');
  assert.equal(age('2026-11-10T12:00:00Z', now), 'today');
  assert.equal(age('2026-11-01T00:00:00Z', now), '10d');
  assert.equal(age('2026-05-01T00:00:00Z', now), '6mo');
  assert.equal(age('2022-11-01T00:00:00Z', now), '4y');
});

test('label ink is black on light colours and white on dark', () => {
  assert.equal(inkOn('ffffff'), '#000');
  assert.equal(inkOn('0e8a16'), '#fff');
});

test('the picker lists matching milestones, open ones first in column order', () => {
  const ms = [milestone(3, 'Needs tests', 'closed'), milestone(1, 'Needed soon'), milestone(2, 'Needed later')];
  assert.deepEqual(pickerMatches(ms, [2, 1], 'need').map((m) => m.title), ['Needed later', 'Needed soon', 'Needs tests']);
  assert.deepEqual(pickerMatches(ms, [2, 1], 'soon').map((m) => m.title), ['Needed soon']);
});

test('the prompt for an agent says where the issue stands and what you think', () => {
  const ms = new Map([[7, milestone(7, 'Critical')]]);
  const c = card(62, { title: 'Add X', url: 'https://github.com/o/r/issues/62', milestone: 7, fields: { Priority: 'High' }, labels: [{ name: 'bug', color: 'fff' }] });
  const prompt = askPrompt('o/r', [c], ms, ' medium effort, own source ');
  assert.match(prompt, /- o\/r#62 "Add X"/);
  assert.match(prompt, /Now: Critical; priority High; effort not set; labels: bug\./);
  assert.match(prompt, /What I think: medium effort, own source\n/);
  assert.match(prompt, /milestones suggest 62 \.\.\. --reason/);
  assert.match(askPrompt('o/r', [c, card(63)], ms, ''), /these 2 issues[\s\S]*nothing yet[\s\S]*milestones suggest N /);
});

test('the board opens as asked, else as it was left, else everything by milestone', () => {
  const ms = [milestone(7, 'Critical'), milestone(8, 'v1')];
  const all = [NONE, 7, 8];
  const none = { from: [], by: '', group: [] };
  assert.deepEqual(initialView(none, undefined, all, ms), { by: 'milestone', group: [], shown: all, detail: true });
  const saved = { by: 'priority', group: ['effort'], shown: [7, 99], detail: false };   // 99 has since gone
  assert.deepEqual(initialView(none, saved, all, ms), { by: 'priority', group: ['effort'], shown: [7], detail: false });
  assert.deepEqual(initialView({ from: ['v1'], by: 'effort', group: ['priority'] }, saved, all, ms),
    { by: 'effort', group: ['priority'], shown: [8], detail: false });
  assert.deepEqual(initialView(none, { by: 'nonsense', group: ['x'], shown: [99] }, all, ms), { by: 'milestone', group: [], shown: all, detail: true });
});

test('the query written back opens the board the way it stands', () => {
  const ms = [milestone(7, 'Critical'), milestone(8, 'Babel v1.19 & after')];
  const all = [NONE, 7, 8];
  const back = (view: { by: DimKind; group: DimKind[]; shown: (number | string)[] }) =>
    initialView(parseLocation(`?${viewQuery(view, all, ms)}`, '#t=tok'), { by: 'effort', group: ['milestone'], shown: [7] }, all, ms);
  // Whatever was saved, the URL wins: the save may not have landed when the page reloads.
  assert.deepEqual(back({ by: 'priority', group: ['priority', 'effort'], shown: [NONE, 8] }),
    { by: 'priority', group: ['priority', 'effort'], shown: [NONE, 8], detail: true });
  assert.deepEqual(back({ by: 'milestone', group: [], shown: [8] }), { by: 'milestone', group: [], shown: [8], detail: true });
  // Every milestone ticked is said by leaving `from` out, not by naming them all.
  assert.equal(viewQuery({ by: 'milestone', group: [], shown: [8, 7, NONE] }, all, ms), 'by=milestone&group=none');
});

test('a label that a chip already speaks for is known by its prefix', () => {
  for (const name of ['Priority: High', 'priority:low', 'Size: M', 'Effort: Low', ' Priority : Someday']) assert.ok(isValueLabel(name), name);
  for (const name of ['bug', 'prioritise', 'needs effort', 'area: priority']) assert.ok(!isValueLabel(name), name);
  // The server's table decides, where it sends one.
  assert.ok(isValueLabel('P: 1', { priority: { field: 'Priority', prefix: 'p', values: {} } }));
});

test('no milestone ticked is said in the URL, and is not the same as saying nothing', () => {
  const ms = [milestone(7, 'Critical')];
  const all = [NONE, 7];
  const query = viewQuery({ by: 'priority', group: [], shown: [] }, all, ms);
  assert.equal(query, 'by=priority&group=none&from=');
  assert.deepEqual(initialView(parseLocation(`?${query}`, ''), { shown: [7] }, all, ms).shown, []);
  assert.deepEqual(initialView(parseLocation('?by=priority', ''), { shown: [7] }, all, ms).shown, [7]);
});

test('the filters ride in the URL while they are on, and come back from it', () => {
  const view = { by: 'milestone' as const, group: [], shown: [NONE] };
  const f = { text: 'insulin & co', label: 'help wanted', priority: 'High', kind: 'pr' as const, hasPr: true, unlabelled: true, author: 'gaurav' };
  assert.deepEqual(filtersFrom(`?${viewQuery(view, [NONE], [], f)}`), f);
  assert.equal(viewQuery(view, [NONE], [], noFilters), 'by=milestone&group=none');
  assert.deepEqual(filtersFrom('?kind=nonsense&pr=yes'), noFilters);
});

test('a value the columns or the grouping already show is shown by the layout', () => {
  assert.equal(shownByLayout('priority', 'priority', []), true);
  assert.equal(shownByLayout('priority', 'milestone', ['effort', 'priority']), true);
  assert.equal(shownByLayout('effort', 'milestone', ['priority']), false);
  assert.equal(shownByLayout('milestone', 'priority', []), false);
});

test("a pull request's size is prcoder's band of its lines changed, which is an effort option", () => {
  assert.deepEqual([0, 49, 50, 99, 100, 199, 200, 499, 500, 999, 1000].map(sizeOf),
    ['XS', 'XS', 'S', 'S', 'M', 'M', 'L', 'L', 'XL', 'XL', 'XXL']);
  const pr = (additions: number, deletions: number) => card(1, { kind: 'pr', additions, deletions });
  assert.deepEqual(estimatedEffort(pr(120, 30), MAPPING), { size: 'M', lines: 150, value: 'M' });
  assert.equal(estimatedEffort(pr(2000, 0))?.value, 'XL');   // XXL reads as XL does
  const coarse: Mapping = { effort: { field: 'Effort', prefix: 'effort', values: {}, options: ['High', 'Low'] } };
  assert.equal(estimatedEffort(pr(10, 0), coarse)?.value, '');
  assert.equal(estimatedEffort(card(2), MAPPING), null);
});

test('a number key picks the option at that place in the list', () => {
  const keys = (kind: 'priority' | 'effort') => [0, 1, 2, 3, 4, 5, 6].map((k) => optionForKey(kind, k, MAPPING));
  assert.deepEqual(keys('priority'), [undefined, 'Urgent', 'High', 'Medium', 'Low', undefined, undefined]);
  assert.deepEqual(keys('effort'), [undefined, 'XS', 'S', 'M', 'L', 'XL', undefined]);
  assert.deepEqual(keyedOptions('effort').map((o) => o.keys), [[1], [2], [3], [4], [5]]);
});
