#!/usr/bin/env node
// Drives a read-only board in a headless browser and says what held: the URL following the
// view, the filters and a reload; the toolbar's menus; the card panel's buttons. For debugging
// the parts of the page that `npm test` cannot reach (signals and the DOM), not a CI gate.
//
//   node tools/board-check.mjs [OWNER/NAME] [--shots DIR] [--port N]
//
// It starts its own board with MILESTONES_PERMISSIONS=read and a scratch XDG_STATE_HOME, and
// refuses to go on unless that board says it is read-only, so nothing here can write to GitHub
// or disturb a saved view. What it changes goes to a plan file in the scratch directory, which
// is deleted. Write paths are not covered: see the issue on testing them.
//
// Playwright is not a dependency of this repo (its browsers are some 300 MB). Install it
// anywhere and say where:  PLAYWRIGHT_FROM=/path/to/a/project/with/playwright
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const flag = (name) => { const at = args.indexOf(name); return at < 0 ? undefined : args.splice(at, 2)[1]; };
const shots = flag('--shots');
const port = flag('--port') ?? '8791';
const repo = args[0] ?? 'gaurav/milestones';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

let chromium;
try {
  const from = process.env.PLAYWRIGHT_FROM;
  ({ chromium } = from ? createRequire(join(resolve(from), 'package.json'))('playwright') : await import('playwright'));
} catch {
  console.error('Playwright not found. Set PLAYWRIGHT_FROM to a directory whose node_modules has it.');
  process.exit(2);
}

const state = mkdtempSync(join(tmpdir(), 'board-check-'));
const board = spawn('uv', ['run', '--project', root, 'milestones', 'board', repo, '--no-open', '--port', port], {
  cwd: state,   // not the checkout: a .env there must not decide the mode
  env: { ...process.env, MILESTONES_PERMISSIONS: 'read', XDG_STATE_HOME: state },
  stdio: ['ignore', 'ignore', 'pipe'],
});
const stop = () => { board.kill('SIGINT'); rmSync(state, { recursive: true, force: true }); };
process.on('exit', stop);

const url = await new Promise((done, fail) => {
  let said = '';
  board.stderr.on('data', (chunk) => {
    said += chunk;
    const found = /^http:\/\/127\.0\.0\.1\S+/m.exec(said);
    if (!found) return;
    if (/read-only: nothing will be written/.test(said)) done(found[0]);
    else fail(new Error('the board did not start read-only; not driving it'));
  });
  board.on('exit', (code) => fail(new Error(`the board exited (${code}):\n${said}`)));
});

let failed = 0;
const check = (what, ok, detail = '') => {
  if (!ok) failed += 1;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}${detail && !ok ? `  — ${JSON.stringify(detail)}` : ''}`);
};
const note = (what, value) => console.log(`     ${what}: ${JSON.stringify(value)}`);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
const shot = async (name, clip) => {
  if (!shots) return;
  mkdirSync(shots, { recursive: true });
  await page.screenshot({ path: join(shots, `${name}.png`), ...(clip ? { clip } : {}) });
};
const settle = () => page.waitForTimeout(120);
const search = () => page.evaluate(() => location.search);
const query = async () => Object.fromEntries(new URLSearchParams(await search()));
const heads = () => page.$$eval('.column-title', (els) => els.map((e) => e.textContent.replace(/^\d/, '')));
const menus = () => page.locator('.menu-body').count();
const menu = (label) => page.click(`.menu > button:has-text("${label}")`);
const focusedCard = () => page.$eval('.card.focused .card-title a', (e) => e.textContent).catch(() => null);

try {
  await page.goto(url);
  await page.waitForSelector('.column');

  // --- The URL follows the view, and a reload reads it back.
  check('the token stays in the fragment', (await page.evaluate(() => location.hash)).startsWith('#t='));
  await page.click('.segmented button:has-text("Priority")');
  await page.selectOption('.toolbar > select', 'effort');
  let q = await query();
  check('by and group are written to the query', q.by === 'priority' && q.group === 'effort', q);
  await page.click('.segmented button:has-text("Milestone")');
  const all = await heads();
  await menu('Columns');
  const boxes = page.locator('.menu-body label input');
  if (await boxes.count() > 2) {
    await boxes.nth(1).click();
    q = new URLSearchParams(await search());
    check('an unticked milestone puts the rest in from=', q.getAll('from').length === all.length - 1, q.getAll('from'));
    const left = await heads();
    await page.keyboard.press('Escape');
    await page.reload(); await page.waitForSelector('.column');
    check('a reload shows the same columns', JSON.stringify(await heads()) === JSON.stringify(left), await heads());
    await menu('Columns');
  }
  await page.click('.menu-actions button:has-text("None")');
  check('none ticked is one empty from=', (await search()).endsWith('from='), await search());
  await page.keyboard.press('Escape');
  await page.reload(); await page.waitForSelector('.toolbar');
  check('a reload keeps none ticked', (await page.locator('.column').count()) === 0);
  await menu('Columns'); await page.click('.menu-actions button:has-text("All")'); await page.keyboard.press('Escape');
  check('all ticked drops from=', !(await search()).includes('from='), await search());

  // --- Filters ride in the URL too.
  await page.fill('#search', 'the'); await page.keyboard.press('Enter');
  await page.selectOption('.filterbar select >> nth=0', 'issue');
  check('a filter that is on is marked', (await page.locator('.filterbar label.filtering').count()) === 1);
  q = await query();
  check('search and filters are in the query', q.q === 'the' && q.kind === 'issue', q);
  const shown = await page.locator('.card').count();
  await page.reload(); await page.waitForSelector('.toolbar');
  check('a reload keeps the search and filters',
    (await page.inputValue('#search')) === 'the' && (await page.locator('.card').count()) === shown);
  await page.click('.filterbar .clear');
  check('Clear goes once nothing is on', (await page.locator('.filterbar .clear').count()) === 0);
  check('clearing them clears the query', !('q' in await query()) && !('kind' in await query()), await query());

  // --- Menus: one at a time, closed from outside, by Escape, and through the description.
  await page.waitForSelector('.card');
  await page.click('.card >> nth=0');
  await menu('Columns');
  const body = await page.locator('.menu-body').boundingBox();
  check('a menu opens inside the page', body.x >= 0 && body.x + body.width <= 1440, body);
  const before = await focusedCard();
  await page.keyboard.press('j');
  check('board keys stand down behind a menu', before === await focusedCard());
  await page.click('.menu > button:has-text("?")');
  check('opening one menu closes the other', (await menus()) === 1);
  await page.keyboard.press('j'); await settle();
  check('but they work while the list of keys is open', (await page.locator('.card').count()) < 2 || before !== await focusedCard());
  await page.keyboard.press('k');
  await page.keyboard.press(' ');
  await page.keyboard.press('Escape'); await settle();
  check('Escape closes a menu and keeps the selection', (await menus()) === 0 && (await page.locator('.card.selected').count()) === 1);
  await page.keyboard.press('Escape'); await settle();
  check('a second Escape clears the selection', (await page.locator('.card.selected').count()) === 0);
  await menu('Columns'); await page.mouse.click(300, 500); await settle();
  check('a click outside closes a menu', (await menus()) === 0);
  if (await page.locator('.detail-strip').count()) await page.click('.detail-strip .open');
  const frame = await page.locator('iframe.body').boundingBox();
  if (frame) {
    await menu('Columns');
    await page.mouse.click(frame.x + frame.width / 2, frame.y + Math.min(40, frame.height / 2)); await settle();
    check('so does a click in the description', (await menus()) === 0);
  }

  // --- The card panel's buttons, where this build has them.
  if (await page.locator('.places .place').count()) {
    const places = () => page.$$eval('.places .place, .detail-strip .place', (e) => e.map((x) => x.textContent));
    const was = await places();
    await page.click('.places .place >> nth=1'); await settle();
    check('the priority button opens the priority dialog', /^Priority for/.test(await page.textContent('.picker-head')));
    await page.keyboard.press('1'); await settle();
    check('choosing sets it, as planned on a read-only board',
      (await places())[1] !== was[1] && (await page.locator('.card.focused .status.would').count()) === 1, await places());
    await shot('panel-open');
    await page.click('.detail .collapse'); await settle();
    check('the collapsed strip carries the same buttons', JSON.stringify(await places()) !== '[]' && (await places())[1] !== was[1]);
    note('strip width', Math.round((await page.locator('.detail-strip').boundingBox()).width));
    await shot('panel-strip');
    await page.click('.detail-strip .place >> nth=0'); await settle();
    check('the strip opens the milestone picker', /^Move #/.test(await page.textContent('.picker-head')));
    await page.keyboard.press('Escape'); await settle();
    check('Escape closes a dialog opened by a click', (await page.locator('.overlay').count()) === 0);
    await page.keyboard.press('z'); await settle();
    await page.click('.detail-strip .open'); await settle();
    check('z undoes the change', JSON.stringify(await places()) === JSON.stringify(was), await places());
  } else {
    note('card panel buttons', 'not in this build; skipped');
  }

  // --- How tall the top bar and the filter row are at each width: reported, since what is right depends on the repo's name.
  for (const width of [1440, 1280, 1000, 760]) {
    await page.setViewportSize({ width, height: 900 });
    note(`top bar height at ${width}px`, Math.round((await page.locator('.toolbar').boundingBox()).height));
    note(`filter row height at ${width}px`, Math.round((await page.locator('.filterbar').boundingBox()).height));
    await shot(`bar-${width}`, { x: 0, y: 0, width, height: 200 });
  }
  check('no errors on the page', errors.length === 0, errors);
} finally {
  await browser.close();
}
console.log(failed ? `\n${failed} failed` : '\nall held');
process.exit(failed ? 1 : 0);
