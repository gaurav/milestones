import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseFragment } from '../ui/shared/api.ts';
import type { Action, Card, Milestone } from '../ui/shared/api.ts';
import type { DimKind, Layout, Mapping } from '../ui/shared/logic.ts';
import {
  NONE, age, allColumns, askPrompt, cardStatus, cardValue, columnFor, columnLabel, describe, dimColumns,
  effectiveValue, effortOf, group, inView, initialColumns, initialView, inkOn, latestAction, matches, nextFocus,
  noFilters, pickerMatches, priorityOf, sections, unsettled, withValue,
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

test('the fragment carries the token and the columns asked for', () => {
  assert.deepEqual(parseFragment('#t=abc&from=Critical&from=Babel%20v1.19'), { token: 'abc', from: ['Critical', 'Babel v1.19'], by: '', group: [] });
  assert.deepEqual(parseFragment('#t=a&by=priority&group=priority,effort'), { token: 'a', from: [], by: 'priority', group: ['priority', 'effort'] });
  assert.deepEqual(parseFragment(''), { token: '', from: [], by: '', group: [] });
});

test('priority and effort are read from the field first, then a label', () => {
  assert.equal(priorityOf(card(1, { fields: { Priority: 'High' }, labels: [{ name: 'Priority: Medium', color: 'fff' }] })), 'High');
  assert.equal(priorityOf(card(1, { labels: [{ name: 'Priority: Medium', color: 'fff' }] })), 'Medium');
  assert.equal(priorityOf(card(1)), '');
  assert.equal(effortOf(card(1, { labels: [{ name: 'Size: M', color: 'fff' }] })), 'M');
  assert.equal(effortOf(card(1, { fields: { Effort: 'Low' } })), 'Low');
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
  priority: { field: 'Priority', prefix: 'priority', values: { critical: 'Urgent', high: 'High', medium: 'Medium', low: 'Low' } },
  effort: { field: 'Effort', prefix: '(?:size|effort)', values: { s: 'Low', m: 'Medium', l: 'High', low: 'Low' } },
};
const ORG = { Priority: ['Urgent', 'High', 'Medium', 'Low'], Effort: ['High', 'Medium', 'Low'] };
const layout = (kind: DimKind, fields: Record<string, string[]> = ORG): Layout =>
  ({ kind, order: [7, 8], fields, mapping: MAPPING, milestones: [milestone(7, 'Critical'), milestone(8, 'v1')] });
const label = (name: string) => ({ name, color: 'ffffff' });

test('cards group by milestone column, freshest first', () => {
  const groups = group([card(1), card(3), card(2, { milestone: 7 })], noFilters, layout('milestone'));
  assert.deepEqual(groups.get(NONE)!.map((c) => c.number), [3, 1]);
  assert.deepEqual(groups.get(7)!.map((c) => c.number), [2]);
});

test('a value is read from the field, else from a label in the field\'s terms', () => {
  assert.deepEqual(effectiveValue(card(1, { fields: { Priority: 'High' }, labels: [label('Priority: Low')] }), 'priority', MAPPING), { value: 'High', from: 'field' });
  assert.deepEqual(effectiveValue(card(1, { labels: [label('priority:critical')] }), 'priority', MAPPING), { value: 'Urgent', from: 'label' });
  assert.deepEqual(effectiveValue(card(1, { labels: [label('Size: M')] }), 'effort', MAPPING), { value: 'Medium', from: 'label' });
  assert.deepEqual(effectiveValue(card(1, { labels: [label('Effort: Low')] }), 'effort', MAPPING), { value: 'Low', from: 'label' });
  assert.deepEqual(effectiveValue(card(1, { labels: [label('Priority: Someday')] }), 'priority', MAPPING), { value: 'Someday', from: 'label' });
  assert.deepEqual(effectiveValue(card(1, { labels: [label('bug')] }), 'priority', MAPPING), { value: '', from: '' });
});

test('each dimension has its columns, the unset one first', () => {
  assert.deepEqual(dimColumns('milestone', layout('milestone'), []), [NONE, 7, 8]);
  assert.deepEqual(dimColumns('priority', layout('priority'), []), [NONE, 'Urgent', 'High', 'Medium', 'Low']);
  assert.deepEqual(dimColumns('effort', layout('effort', {}), []), [NONE, 'High', 'Medium', 'Low']);   // no field: the defaults
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

test('the priority view is scoped by milestone and leaves pull requests out where fields exist', () => {
  const cs = [card(1, { milestone: 7, fields: { Priority: 'High' } }), card(2, { milestone: 8 }), card(3),
    card(4, { milestone: 7, kind: 'pr', labels: [label('Priority: High')] })];
  const scoped = group(cs, noFilters, layout('priority'), new Set([7, NONE]));
  assert.deepEqual([...scoped].map(([id, list]) => [id, list.map((c) => c.number)]), [['High', [1]], [NONE, [3]]]);
  // No Priority field for this owner: the value lives in a label, which a pull request can carry.
  const noField = group(cs, noFilters, layout('priority', {}), null);
  assert.deepEqual(noField.get('High')!.map((c) => c.number), [4, 1]);
  assert.ok(inView(cs[3]!, layout('milestone'), new Set([8])));   // the milestone view shows everything
});

test('cards group within a column, judged ones first, and cut into headed sections', () => {
  const cs = [card(1), card(2, { fields: { Priority: 'Low' } }), card(3, { fields: { Priority: 'Urgent', Effort: 'Low' } }),
    card(4, { fields: { Priority: 'Urgent', Effort: 'High' } }), card(5, { fields: { Priority: 'Urgent', Effort: 'High' } })];
  const l = layout('milestone');
  const list = group(cs, noFilters, l, null, ['priority', 'effort']).get(NONE)!;
  assert.deepEqual(list.map((c) => c.number), [5, 4, 3, 2, 1]);
  assert.deepEqual(sections(list, ['priority', 'effort'], l).map((s) => [s.label, s.cards.length]),
    [['Urgent · High', 2], ['Urgent · Low', 1], ['Low · No effort', 1], ['No priority · No effort', 1]]);
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
  assert.equal(cardValue(c, 'priority'), 'High');
  assert.equal(cardValue(c, 'effort'), null);
  assert.deepEqual(cardValue(c, 'labels'), ['bug']);
  c = withValue(c, 'priority', null, known);
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
  assert.deepEqual(initialView(none, undefined, all, ms), { by: 'milestone', group: [], shown: all });
  const saved = { by: 'priority', group: ['effort'], shown: [7, 99] };   // 99 has since gone
  assert.deepEqual(initialView(none, saved, all, ms), { by: 'priority', group: ['effort'], shown: [7] });
  assert.deepEqual(initialView({ from: ['v1'], by: 'effort', group: ['priority'] }, saved, all, ms),
    { by: 'effort', group: ['priority'], shown: [8] });
  assert.deepEqual(initialView(none, { by: 'nonsense', group: ['x'], shown: [99] }, all, ms), { by: 'milestone', group: [], shown: all });
});
