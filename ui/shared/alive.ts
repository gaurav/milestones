// Whether the app this page came from is still running. A page left open after `milestones`
// stops (q or Ctrl-C in its terminal, or a closed terminal) would otherwise go on looking
// usable while every click and key failed. So the page is switched off — covered, inert, deaf
// to keys — and says why and what to do, and switched back on if the same app comes back.
//
// Every page watches through its event stream (`watch`, from `events()` in api.ts and app.ts):
// the app sends `stopped` as it shuts down, and a stream that drops for any other reason is
// checked with a plain request. A `--reload` keeps its socket, so a request made during one
// waits rather than failing, and the page never notices. A new run of `milestones` has a new
// token, so this page's is refused (401): it can't be switched back on, only replaced by the
// link the new run gives.

type State = 'up' | 'down' | 'restarted';
let state: State = 'up';
let overlay: HTMLElement | null = null;
let polling = false;

let guarding = false;

/** Swallow every key while the page is off. In the capture phase on window, so it comes before
 *  a page's own handlers: theirs are on window too, but bubbling, and a menu's capturing one is
 *  added later, when the menu opens. Added from `watch`, not at import: the front-end tests
 *  import this module (through api.ts) where there is no window. */
function guardKeys() {
  if (guarding) return;
  guarding = true;
  window.addEventListener('keydown', (e) => {
    if (state === 'up') return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);
}

const MESSAGES: Record<Exclude<State, 'up'>, [string, string]> = {
  down: ['milestones has stopped',
    'This page can’t read or write anything until it is running again. Start it with `milestones` '
    + 'in a terminal; if it is the same run coming back, this page carries on by itself. Anything '
    + 'that was not written to GitHub is listed in the terminal it ran in.'],
  restarted: ['milestones was started again',
    'That run has a link of its own, which this page doesn’t have. Press o in its terminal, or open '
    + 'the link it printed.'],
};

function show(next: State) {
  if (next === state) return;
  state = next;
  const app = document.getElementById('app');
  if (app) app.inert = state !== 'up';
  overlay?.remove();
  overlay = null;
  if (state === 'up') return;
  const [title, text] = MESSAGES[state];
  overlay = document.createElement('div');
  overlay.className = 'stopped';
  overlay.setAttribute('role', 'alertdialog');
  const box = document.createElement('div');
  const h = document.createElement('h2');
  h.textContent = title;
  const p = document.createElement('p');
  // `code` spans for what is to be typed; the rest is plain text.
  text.split('`').forEach((part, i) => {
    if (i % 2) { const c = document.createElement('code'); c.textContent = part; p.append(c); }
    else p.append(part);
  });
  box.append(h, p);
  overlay.append(box);
  document.body.append(overlay);
}

/** Ask the app whether it is there, and whether it is still the one this page belongs to. */
async function probe(token: string): Promise<State> {
  try {
    const answer = await fetch('/api/v1/app', { headers: { 'X-Milestones-Token': token } });
    return answer.status === 401 ? 'restarted' : 'up';
  } catch {
    return 'down';
  }
}

/** Keep asking until the app answers; a new run (`restarted`) is final. */
async function poll(token: string, source: EventSource) {
  if (polling) return;
  polling = true;
  // A moment first: a stream also drops when the page is being left for another, and the
  // check would fail as the page went.
  await new Promise((done) => setTimeout(done, 500));
  try {
    for (;;) {
      const now = await probe(token);
      show(now);
      if (now === 'restarted') return;
      if (now === 'up') {
        // The same app, back: a stream that gave up for good won't come back by itself.
        if (source.readyState === EventSource.CLOSED) location.reload();
        return;
      }
      await new Promise((done) => setTimeout(done, 2000));
    }
  } finally {
    polling = false;
  }
}

/** Switch the page off when the app behind `source` goes away, and on when it is back. */
export function watch(source: EventSource, token: string) {
  guardKeys();
  source.addEventListener('stopped', () => { show('down'); setTimeout(() => poll(token, source), 1000); });
  source.addEventListener('open', () => { if (state === 'down') show('up'); });
  source.addEventListener('error', () => { poll(token, source); });
}
