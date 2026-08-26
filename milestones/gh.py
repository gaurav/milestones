"""Thin wrappers around the `gh` CLI, which provides auth, pagination and retries."""

import json
import subprocess
from urllib.parse import quote


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("gh not found; install the GitHub CLI: https://cli.github.com")
    if proc.returncode != 0:
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
    """Run a GraphQL query and return its `data` dict."""
    return json.loads(_run(["api", "graphql", "-f", f"query={query}"]))["data"]


def search_issues(query: str) -> list[dict]:
    # `gh search issues` uses the legacy search path and returns nothing anymore;
    # the REST endpoint needs advanced_search=true. Items are REST-shaped issues.
    # ponytail: first 100 results, newest-updated; chunk per-repo queries if a
    # triage session ever clears 100.
    path = f"search/issues?q={quote(query)}&sort=updated&advanced_search=true&per_page=100"
    return api(path)["items"]
