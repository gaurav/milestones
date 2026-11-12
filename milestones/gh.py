"""Thin wrappers around the `gh` CLI, which provides auth, pagination and retries."""

import functools
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

from . import env


class ReadOnly(SystemExit):
    """A write refused because MILESTONES_PERMISSIONS=read. A SystemExit, like every other
    failed call here, so a walk that already reports a failed write and carries on does the
    same with this one; its message says what would have been written."""


@functools.cache
def settings() -> tuple[dict[str, str], dict[str, str]]:
    """The env settings and where each came from, read once per run."""
    return env.load()


def read_only() -> bool:
    return env.permissions(settings()[0]) == "read"


def _refuse_write(what: str) -> None:
    """The one place a write is allowed or refused: `api` and `mutate` both come through
    here, so a new write is covered by using either."""
    if read_only():
        raise ReadOnly(f"read-only ({env.PERMISSIONS}=read): would {what}")


def path_owner(path: str) -> str | None:
    """The owner a REST path is about, to pick that owner's token; None for a path that
    spans owners (a search) or names none."""
    match = re.match(r"/?(?:repos|orgs|users)/([^/?]+)", path)
    return match.group(1) if match else None


def _run(args: list[str], partial_ok: bool = False, owner: str | None = None) -> str:
    key = env.token_key(settings()[0], owner)
    # gh reads GH_TOKEN ahead of its own stored login. Unset, the child inherits ours.
    child_env = {**os.environ, "GH_TOKEN": settings()[0][key]} if key else None
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=child_env)
    except FileNotFoundError:
        raise SystemExit("gh not found; install the GitHub CLI: https://cli.github.com")
    # gh signals a GraphQL error by exiting nonzero even when it has printed a
    # perfectly good partial answer alongside it; partial_ok keeps that answer.
    if proc.returncode != 0 and not (partial_ok and proc.stdout.strip()):
        raise SystemExit(f"gh {' '.join(args[:3])}... failed:\n{proc.stderr.strip()}")
    return proc.stdout


def api(path: str, method: str = "GET", paginate: bool = False, **fields):
    """Call the REST API. Returns parsed JSON; a flat list when paginated."""
    if method != "GET":
        _refuse_write(" ".join([method, path, *(f"{k}={v}" for k, v in fields.items())]))
    args = ["api", "-X", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    for key, value in fields.items():
        # -f sends raw strings; -F types numbers/booleans (needed for issue milestone numbers)
        flag = "-f" if isinstance(value, str) else "-F"
        args += [flag, f"{key}={value}"]
    args.append(path)
    out = _run(args, owner=path_owner(path))
    if not out.strip():
        return None
    data = json.loads(out)
    if paginate:
        # --slurp wraps each page in an outer list; a search page is a dict around
        # its "items" rather than a list of results in its own right.
        return [item for page in data
                for item in (page["items"] if isinstance(page, dict) else page)]
    return data


def graphql(query: str, owner: str | None = None) -> dict:
    """Run a GraphQL query and return its `data` dict. `owner` picks that owner's token,
    for a query about one owner; a query across several goes out on the default one.

    A query aliasing many repos at once resolves the ones it can and returns a
    null for the rest — a configured repo that has been deleted, made private, or
    mistyped by hand. Warn about those and hand back everything that did resolve;
    one bad name in the config shouldn't cost you every other repo.
    """
    body = json.loads(_run(["api", "graphql", "-f", f"query={query}"], partial_ok=True,
                           owner=owner))
    errors = body.get("errors") or []
    if body.get("data") is None:
        raise SystemExit("gh api graphql failed:\n" +
                         "\n".join(e.get("message", str(e)) for e in errors))
    for error in errors:
        print(f"warning: {error.get('message', error)}", file=sys.stderr)
    return body["data"]


def mutate(what: str, query: str, owner: str | None = None) -> dict:
    """Run a GraphQL mutation. `what` is the write in a few words, for the message a
    read-only run gives instead of making it. Every mutation goes through here, never
    through `graphql`, which has no way to tell a write from a read."""
    _refuse_write(what)
    return graphql(query, owner)


def probe(query: str, owner: str | None = None) -> list[str]:
    """Run a GraphQL query for what it can and can't see: the error messages, none if it
    all resolved. Unlike `graphql` it prints nothing, since an error is the answer here."""
    key = env.token_key(settings()[0], owner)
    child_env = {**os.environ, "GH_TOKEN": settings()[0][key]} if key else None
    proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                          capture_output=True, text=True, env=child_env)
    try:
        errors = json.loads(proc.stdout).get("errors") or []
    except ValueError:
        return [proc.stderr.strip() or "no answer"]
    return [e.get("message", str(e)) for e in errors]


def search_issues(query: str) -> list[dict]:
    # `gh search issues` uses the legacy search path and returns nothing anymore;
    # the REST endpoint needs advanced_search=true. Items are REST-shaped issues.
    # GitHub caps search itself at 1000 results per query, silently; the caller splits
    # by repo, so no single query has come near that, but a count built on one would be
    # a lower bound without a word said. Say the word.
    path = f"search/issues?q={quote(query)}&sort=updated&advanced_search=true&per_page=100"
    items = api(path, paginate=True) or []
    if len(items) >= 1000:
        print(f"warning: search stops at 1000 results; more match {query!r}", file=sys.stderr)
    return items
