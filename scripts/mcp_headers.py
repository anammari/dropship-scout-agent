#!/usr/bin/env python3
"""Emit MCP request headers for the trend-research servers.

Claude Code runs this as a project-scope `headersHelper` (see `.mcp.json`): it
must write a JSON object of string key-value pairs to stdout, which Claude Code
sends as the connection's headers.

Why this script exists rather than the usual `${VAR}` expansion: for servers
declared in a project `.mcp.json`, Claude Code reads credential-looking
variables (any name containing TOKEN, KEY, SECRET, PASSWORD or AUTH, either
case) as *empty* in the `url` and `headers` fields, and strips them from the
environment before running a headers helper. `HASDATA_API_KEY` and
`APIFY_TOKEN` both match that rule, so the credential has to come off disk.
This helper therefore parses `.env` itself, matching the project's rule that
real environment variables win over `.env` (CLAUDE.md §10).

Usage:
    python3 scripts/mcp_headers.py {hasdata|apify}

Writes the header JSON on stdout. Exits non-zero with a plain diagnostic on
stderr if the credential is missing, so a misconfigured run fails loudly
instead of connecting with an empty header. The credential value is never
logged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / ".env"

# server key -> (env var holding the credential, header name, value prefix)
_SERVERS: dict[str, tuple[str, str, str]] = {
    "hasdata": ("HASDATA_API_KEY", "x-api-key", ""),
    "apify": ("APIFY_TOKEN", "Authorization", "Bearer "),
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a dotenv-style file into a mapping.

    Handles comments, blank lines, an optional `export ` prefix, and values
    wrapped in single or double quotes. Keys are returned as written.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_credential(name: str, env_file: dict[str, str]) -> str:
    """Return the credential for `name`, preferring the real environment."""
    return (os.environ.get(name) or env_file.get(name) or "").strip()


def headers_for(server: str, env_file: dict[str, str]) -> dict[str, str]:
    """Build the header mapping for one server key. Raises KeyError if unknown."""
    env_var, header, prefix = _SERVERS[server]
    credential = resolve_credential(env_var, env_file)
    if not credential:
        raise ValueError(
            f"{env_var} is empty or unset — add it to .env (see .env.example) "
            f"or export it in the environment."
        )
    return {header: f"{prefix}{credential}"}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in _SERVERS:
        print(
            f"usage: {Path(argv[0]).name} {{{'|'.join(_SERVERS)}}}",
            file=sys.stderr,
        )
        return 2
    try:
        headers = headers_for(argv[1], parse_env_file(_ENV_PATH))
    except ValueError as exc:
        print(f"[mcp_headers] {exc}", file=sys.stderr)
        return 1
    json.dump(headers, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
