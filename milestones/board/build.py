"""Whether the built front end in `static/` can be served as it stands, and what to do if not.

Each page's HTML names its script and stylesheets by absolute path. A build that went wrong
leaves a page whose HTML loads but names files that aren't there, and the browser shows a
blank page with nothing in the terminal to say why. The usual cause: `npm run dev` started on
one branch keeps the `build.mjs` it started with, so after a checkout that changes how pages
are laid out, it writes the new HTML into the old places. These checks find that before the
page does.

`problems` decides what is broken (the page is refused, and the terminal says why);
`warnings` notices a build that works but is older than the code checked out."""

import datetime
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).parent / "static"
# The front end's sources, which exist only in a checkout.
CHECKOUT = Path(__file__).resolve().parents[2]
UI = CHECKOUT / "ui"

# Files `build.mjs` copies as they are, rather than bundling: a built copy that differs from
# its source was built from other code.
COPIED = re.compile(r"\.(html|css|svg|png|ico)$")
REFERENCE = re.compile(r'(?:src|href)="(/[^"#?]*)"')


def built_page(name: str) -> str:
    """Where `build.mjs` puts the page from ui/<name>/, relative to static/."""
    return "index.html" if name == "home" else f"{name}/index.html"


def sources(ui: Path) -> list[str]:
    """The pages in ui/: each folder with an index.html, as build.mjs finds them."""
    if not ui.is_dir():
        return []
    return sorted(d.name for d in ui.iterdir()
                  if d.is_dir() and d.name != "shared" and (d / "index.html").is_file())


def problems(static: Path = STATIC, ui: Path = UI) -> list[str]:
    """What stops a page being served: a page ui/ has that the build hasn't, and any file a
    built page asks for that isn't there. Empty when the build is whole."""
    found = []
    if not sources(ui) and not (static / "index.html").is_file():
        found.append("static/index.html is missing")
    for name in sources(ui):
        if not (static / built_page(name)).is_file():
            found.append(f"ui/{name}/ has not been built (no static/{built_page(name)})")
    for page in sorted(static.rglob("*.html")) if static.is_dir() else []:
        for ref in REFERENCE.findall(page.read_text(errors="replace")):
            if not (static / ref.lstrip("/")).is_file():
                found.append(f"static/{page.relative_to(static)} loads {ref}, "
                             "which the build doesn't have")
    return found


def warnings(static: Path = STATIC, ui: Path = UI) -> list[str]:
    """What says the build is older than the code checked out, though it can be served: a
    copied file that differs from its source, or a script changed since the bundles were
    made. Nothing outside a checkout, where there are no sources to compare."""
    if not ui.is_dir() or not static.is_dir():
        return []
    found = []
    for name in [*sources(ui), "shared"]:
        target = static if name in ("home", "shared") else static / name
        for source in sorted((ui / name).iterdir()):
            built = target / source.name
            if COPIED.search(source.name) and built.is_file() \
                    and built.read_bytes() != source.read_bytes():
                found.append(f"static/{built.relative_to(static)} differs from "
                             f"ui/{name}/{source.name}")
    bundles = [static / built_page(n).replace("index.html", "main.js") for n in sources(ui)]
    bundles = [b for b in bundles if b.is_file()]
    scripts = [p for p in ui.rglob("*.ts*") if p.suffix in (".ts", ".tsx")]
    if bundles and scripts:
        built = min(b.stat().st_mtime for b in bundles)
        newer = sorted(p for p in scripts if p.stat().st_mtime > built)
        if newer:
            found.append(f"{len(newer)} script{'s' if len(newer) > 1 else ''} in ui/ changed "
                         f"since the last build (ui/{newer[0].relative_to(ui)}"
                         + (", …" if len(newer) > 1 else "") + ")")
    return found


def watchers(checkout: Path = CHECKOUT) -> list[dict]:
    """Every `node build.mjs --watch` running in this checkout, with when it started:
    {pid, started}. Empty where `ps` or `lsof` can't say."""
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True,
                                 text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.splitlines():
        if "build.mjs" not in line or "--watch" not in line:
            continue
        # `lstart` is five words: "Fri Nov 13 00:00:12 2026".
        pid, *start, command = line.split(None, 6)
        if Path(command.split()[0]).name != "node":
            continue  # the `sh -c` that `npm run dev` starts it under

        try:
            started = datetime.datetime.strptime(" ".join(start), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        if process_cwd(int(pid)) == checkout:
            found.append({"pid": int(pid), "started": started})
    return found


def process_cwd(pid: int) -> Path | None:
    proc = Path(f"/proc/{pid}/cwd")
    try:
        return proc.resolve(strict=True)
    except OSError:
        pass
    try:  # macOS has no /proc
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = [l[1:] for l in out.splitlines() if l.startswith("n")]
    return Path(names[0]).resolve() if names else None


def remedy(found_watchers: list[dict], script: Path = CHECKOUT / "build.mjs") -> list[str]:
    """What to do about a broken or stale build, as lines for the terminal or a page."""
    if not UI.is_dir():
        return ["This copy of milestones was installed without a working front end; "
                "reinstall it."]
    steps = []
    changed = script.stat().st_mtime if script.is_file() else None
    for w in found_watchers:
        stale = changed is not None and w["started"].timestamp() < changed
        steps.append(
            f"Stop the `npm run dev` watcher (kill {w['pid']}), started "
            f"{w['started']:%a %d %b %H:%M}"
            + (": build.mjs has changed since, and a watcher keeps the build script it "
               "started with." if stale else "."))
    steps.append("From the top of the checkout, run `npm run build` "
                 "(or `npm run dev` to rebuild on every change).")
    steps.append("Reload the page.")
    return steps
