"""Thin wrappers around the `gh` CLI, which provides auth, pagination and retries."""

import json
import subprocess
import sys
from urllib.parse import quote


def _run(args: list[str], partial_ok: bool = False) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("gh not found; install the GitHub CLI: https://cli.github.com")
    # gh signals a GraphQL error by exiting nonzero even when it has printed a
    # perfectly good partial answer alongside it; partial_ok keeps that answer.
    if proc.returncode != 0 and not (partial_ok and proc.stdout.strip()):
        raise SystemExit(f"gh {' '.join(args[:3])}... failed:\n{proc.stderr.strip()}")
    return proc.stdout


def api(path: str, method: str = "GET", paginate: bool = False, **fields):
    """Call the REST API. Returns parsed JSON; a flat list when paginated."""
    args = ["api", "-X", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    for key, value in fields.items():
        # -f sends raw strings; -F types numbers/booleans (needed for issue milestone numbers)
        flag = "-f" if isinstance(value, str) else "-F"
        args += [flag, f"{key}={value}"]
    args.append(path)
    out = _run(args)
    if not out.strip():
        return None
    data = json.loads(out)
    if paginate:  # --slurp wraps each page in an outer list
        return [item for page in data for item in page]
    return data


def graphql(query: str) -> dict:
    """Run a GraphQL query and return its `data` dict.

    A query aliasing many repos at once resolves the ones it can and returns a
    null for the rest — a configured repo that has been deleted, made private, or
    mistyped by hand. Warn about those and hand back everything that did resolve;
    one bad name in the config shouldn't cost you every other repo.
    """
    body = json.loads(_run(["api", "graphql", "-f", f"query={query}"], partial_ok=True))
    errors = body.get("errors") or []
    if body.get("data") is None:
        raise SystemExit("gh api graphql failed:\n" +
                         "\n".join(e.get("message", str(e)) for e in errors))
    for error in errors:
        print(f"warning: {error.get('message', error)}", file=sys.stderr)
    return body["data"]


def search_issues(query: str) -> list[dict]:
    # `gh search issues` uses the legacy search path and returns nothing anymore;
    # the REST endpoint needs advanced_search=true. Items are REST-shaped issues.
    # ponytail: GitHub caps search itself at 1000 results per query; the caller
    # splits by repo, so no single query has come near that.
    path = f"search/issues?q={quote(query)}&sort=updated&advanced_search=true&per_page=100"
    # Not api(paginate=True): that flattens pages that are lists, and search pages
    # are dicts wrapping an "items" list.
    pages = json.loads(_run(["api", "--paginate", "--slurp", path]))
    return [item for page in pages for item in page["items"]]
