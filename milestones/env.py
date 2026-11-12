"""Settings read from the environment and from env files: which GitHub token `gh` is handed,
and whether this run may write at all. `env.default` at the top of the repo is the template."""

import os
import re
from pathlib import Path

PREFIX = "MILESTONES_"
TOKEN = "MILESTONES_GH_TOKEN"
PERMISSIONS = "MILESTONES_PERMISSIONS"
MODES = ("read", "write")

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(text: str) -> dict[str, str]:
    """The KEY=VALUE lines of an env file. A value may be quoted; an unquoted one ends at
    a ` #` comment. Anything that isn't an assignment is skipped rather than refused, so a
    shell-flavoured file someone already has doesn't stop the tool."""
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        values[key] = value
    return values


def files(cwd: Path | None = None, environ=None) -> list[Path]:
    """Where an env file is looked for, nearest first: the directory you run from, then
    beside the config."""
    environ = os.environ if environ is None else environ
    config_home = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [(cwd or Path.cwd()) / ".env", config_home / "milestones.env"]


def load(cwd: Path | None = None, environ=None) -> tuple[dict[str, str], dict[str, str]]:
    """(settings, where each came from). The real environment wins over `./.env`, which wins
    over `milestones.env` beside the config. Only MILESTONES_* keys are taken from a file:
    a project's own `.env` is full of things that are none of this tool's business."""
    environ = os.environ if environ is None else environ
    settings, sources = {}, {}
    for path in reversed(files(cwd, environ)):
        try:
            found = parse(path.read_text())
        except OSError:
            continue
        for key, value in found.items():
            if key.startswith(PREFIX) and value:
                settings[key], sources[key] = value, str(path)
    for key, value in environ.items():
        if key.startswith(PREFIX) and value:
            settings[key], sources[key] = value, "environment"
    return settings, sources


def owner_key(owner: str) -> str:
    """The per-owner token's name: MILESTONES_GH_TOKEN_NCATSTRANSLATOR, and `my-org` as
    MY_ORG, since a variable name can't hold a hyphen."""
    return f"{TOKEN}_{re.sub(r'[^A-Za-z0-9]', '_', owner).upper()}"


def token_key(settings: dict[str, str], owner: str | None) -> str | None:
    """The name of the setting whose token a call about `owner` uses, or None for gh's own
    login. A fine-grained token belongs to one owner, hence one setting per owner."""
    if owner and owner_key(owner) in settings:
        return owner_key(owner)
    return TOKEN if TOKEN in settings else None


def permissions(settings: dict[str, str]) -> str:
    mode = settings.get(PERMISSIONS, "write").lower()
    if mode not in MODES:
        raise SystemExit(f"{PERMISSIONS} is {mode!r}; it must be one of: {', '.join(MODES)}")
    return mode
