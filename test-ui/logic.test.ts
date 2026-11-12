import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseFragment } from '../ui/shared/api.ts';
import type { Action, Card, Milestone } from '../ui/shared/api.ts';
import {
  NONE, age, allColumns, effortOf, group, initialColumns, inkOn, latestAction, matches, nextFocus,
  noFilters, pickerMatches, priorityOf, unsettled,
} from '../ui/shared/logic.ts';

const card = (number: number, extra: Partial<Card> = {}): Card => ({
  id: `I${number}`, number, kind: 'issue', title: `Issue ${number}`, url: '', bodyHTML: '', created: '2026-01-01T00:00:00Z',
  updated: `2026-01-${String(number).padStart(2, '0')}T00:00:00Z`, author: 'a', comments: 0, assignees: [], labels: [],
  milestone: null, ...extra,
});
const milestone = (number: number, title: string, state: 'open' | 'closed' = 'open'): Milestone =>
  ({ number, title, state, due: null, description: '', url: '' });
const action = (id: number, issue: number, status: Action['status'], value: number | null = 1): Action =>
  ({ id, issue, kind: 'milestone', value, previous: null, carriedBy: null, error: null, status });

test('the fragment carries the token and the columns asked for', () => {
  assert.deepEqual(parseFragment('#t=abc&from=Critical&from=Babel%20v1.19'), { token: 'abc', from: ['Critical', 'Babel v1.19'] });
  assert.deepEqual(parseFragment(''), { token: '', from: [] });
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

test('cards group by column, freshest first', () => {
  const groups = group([card(1), card(3), card(2, { milestone: 7 })], noFilters);
  assert.deepEqual(groups.get(NONE)!.map((c) => c.number), [3, 1]);
  assert.deepEqual(groups.get(7)!.map((c) => c.number), [2]);
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
  assert.equal(latestAction(all, 5)!.id, 2);
  assert.equal(latestAction(all, 7), undefined);
  assert.deepEqual(unsettled(all).map((a) => a.id), [2, 4]);
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
