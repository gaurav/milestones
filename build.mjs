// Builds every page under ui/<name>/ into milestones/board/static/, which the Python server
// serves. `home` lands at the root, where the app opens; any other page lands in a folder of
// its own name (`board` is served for every /r/OWNER/NAME/). ui/shared/'s stylesheets land
// at the root too, for every page to link.
// `node build.mjs --watch` rebuilds on change for `npm run dev`.
import { build, context } from 'esbuild';
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, watch as watchDir } from 'node:fs';
import { join } from 'node:path';

const watch = process.argv.includes('--watch');
const out = 'milestones/board/static';
const views = readdirSync('ui').filter((d) => d !== 'shared' && statSync(join('ui', d)).isDirectory());
const outdir = (name) => (name === 'home' ? out : join(out, name));

const builds = views.map((name) => ({
  entryPoints: [`ui/${name}/main.tsx`],
  bundle: true,
  platform: 'browser',
  format: 'esm',
  target: 'es2022',
  outdir: outdir(name),
  jsx: 'automatic',
  jsxImportSource: 'preact',
  sourcemap: watch ? 'inline' : false,
  minify: !watch,
  logLevel: 'info',
}));

// Stylesheets, pages and images are copied as they are, not bundled, so esbuild's watch never
// sees them: in watch mode each view's folder is watched for them separately.
const asset = (f) => /\.(html|css|svg|png|ico)$/.test(f);
const copy = (name, f) => cpSync(`ui/${name}/${f}`, join(outdir(name), f));

rmSync(out, { recursive: true, force: true });
for (const name of views) {
  mkdirSync(outdir(name), { recursive: true });
  for (const f of readdirSync(`ui/${name}`)) if (asset(f)) copy(name, f);
}
const shared = () => { for (const f of readdirSync('ui/shared')) if (asset(f)) cpSync(`ui/shared/${f}`, join(out, f)); };
shared();
if (watch) {
  const ctxs = await Promise.all(builds.map((o) => context(o)));
  await Promise.all(ctxs.map((c) => c.watch()));
  for (const name of views) {
    watchDir(`ui/${name}`, (_, f) => {
      if (!f || !asset(f) || !existsSync(`ui/${name}/${f}`)) return;
      copy(name, f);
      console.log(`[watch] copied ui/${name}/${f}`);
    });
  }
  watchDir('ui/shared', (_, f) => { if (f && asset(f) && existsSync(`ui/shared/${f}`)) shared(); });
  console.log(`watching ${views.length} view(s)`);
} else {
  await Promise.all(builds.map((o) => build(o)));
}
