"""Unit tests for `scripts/mcp_headers.py`.

The helper is the `headersHelper` for the trend-research MCP servers in
`.mcp.json`. Its contract is small but load-bearing: Claude Code sends exactly
what it prints on stdout, so these tests pin the header names, the Bearer
prefix, the exit codes, and — most importantly — that a missing credential
fails loudly rather than emitting an empty header the server would reject.

Hermetic: no network, and no contact with the developer's real `.env`.
`src/config.py` calls `load_dotenv()` at import, so the live credentials are
already present in `os.environ` by the time these tests run — the autouse
fixture below clears them, or `resolve_credential`'s env-wins rule would let a
real key override every fixture value (and a failing assertion would then print
it). Every expected value here is a literal, never a real credential.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import mcp_headers

_CREDENTIAL_VARS = ("HASDATA_API_KEY", "APIFY_TOKEN")


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """Keep the real `.env` out of every test in this module."""
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Redirect the helper's `.env` lookup at a tmp file the test owns."""

    def _write(body: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(mcp_headers, "_ENV_PATH", path)
        return path

    return _write


# ----------------------------------------------------------------------
# parse_env_file
# ----------------------------------------------------------------------


def test_parse_env_file_skips_comments_and_blanks(env_file):
    path = env_file('# a comment\n\nHASDATA_API_KEY="abc"\n   \nAPIFY_TOKEN="def"\n')
    assert mcp_headers.parse_env_file(path) == {
        "HASDATA_API_KEY": "abc",
        "APIFY_TOKEN": "def",
    }


def test_parse_env_file_handles_export_prefix_and_quote_styles(env_file):
    path = env_file("export APIFY_TOKEN='tok'\nHASDATA_API_KEY=\"key\"\nPLAIN=raw\n")
    assert mcp_headers.parse_env_file(path) == {
        "APIFY_TOKEN": "tok",
        "HASDATA_API_KEY": "key",
        "PLAIN": "raw",
    }


def test_parse_env_file_keeps_equals_signs_inside_values(env_file):
    path = env_file('CJ_MCP_TOKEN="a=b=c"\n')
    assert mcp_headers.parse_env_file(path)["CJ_MCP_TOKEN"] == "a=b=c"


def test_parse_env_file_returns_empty_when_file_absent(tmp_path):
    assert mcp_headers.parse_env_file(tmp_path / "nope.env") == {}


def test_parse_env_file_blank_value_is_empty_string(env_file):
    path = env_file('HASDATA_API_KEY=""\n')
    assert mcp_headers.parse_env_file(path)["HASDATA_API_KEY"] == ""


# ----------------------------------------------------------------------
# resolve_credential
# ----------------------------------------------------------------------


def test_real_environment_wins_over_env_file(monkeypatch):
    monkeypatch.setenv("HASDATA_API_KEY", "from-env")
    assert mcp_headers.resolve_credential(
        "HASDATA_API_KEY", {"HASDATA_API_KEY": "from-file"}
    ) == "from-env"


def test_env_file_used_when_environment_is_unset():
    assert mcp_headers.resolve_credential(
        "HASDATA_API_KEY", {"HASDATA_API_KEY": "from-file"}
    ) == "from-file"


def test_blank_either_side_resolves_to_empty(monkeypatch):
    monkeypatch.setenv("HASDATA_API_KEY", "   ")
    assert mcp_headers.resolve_credential("HASDATA_API_KEY", {"HASDATA_API_KEY": ""}) == ""


def test_missing_from_both_sides_is_empty():
    assert mcp_headers.resolve_credential("APIFY_TOKEN", {}) == ""


# ----------------------------------------------------------------------
# headers_for
# ----------------------------------------------------------------------


def test_hasdata_header_shape():
    assert mcp_headers.headers_for("hasdata", {"HASDATA_API_KEY": "abc"}) == {
        "x-api-key": "abc"
    }


def test_apify_header_carries_bearer_prefix():
    assert mcp_headers.headers_for("apify", {"APIFY_TOKEN": "tok"}) == {
        "Authorization": "Bearer tok"
    }


def test_missing_credential_raises_value_error():
    with pytest.raises(ValueError, match="HASDATA_API_KEY is empty or unset"):
        mcp_headers.headers_for("hasdata", {})


def test_unknown_server_raises_key_error():
    with pytest.raises(KeyError):
        mcp_headers.headers_for("nope", {})


# ----------------------------------------------------------------------
# main / CLI contract
# ----------------------------------------------------------------------


def test_main_writes_only_the_header_json_to_stdout(env_file, capsys):
    env_file('HASDATA_API_KEY="abc"\n')
    assert mcp_headers.main(["mcp_headers.py", "hasdata"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"x-api-key": "abc"}
    assert captured.err == ""


def test_main_rejects_unknown_server_with_usage(env_file, capsys):
    env_file("")
    assert mcp_headers.main(["mcp_headers.py", "nope"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_main_requires_exactly_one_argument(env_file, capsys):
    env_file("")
    assert mcp_headers.main(["mcp_headers.py"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_main_missing_credential_exits_nonzero_with_empty_stdout(env_file, capsys):
    # An empty header would be sent as-is and rejected by the server, so the
    # helper must emit nothing on stdout and explain itself on stderr.
    env_file('HASDATA_API_KEY=""\n')
    assert mcp_headers.main(["mcp_headers.py", "hasdata"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HASDATA_API_KEY is empty or unset" in captured.err


def test_main_error_message_never_contains_a_credential(env_file, capsys):
    env_file('APIFY_TOKEN=""\n')
    mcp_headers.main(["mcp_headers.py", "apify"])
    assert "Bearer" not in capsys.readouterr().err


# ----------------------------------------------------------------------
# process contract
# ----------------------------------------------------------------------


def test_subprocess_contract_stdout_is_a_json_object_of_strings(tmp_path):
    """Run it the way Claude Code does: a real process, reading a real .env.

    The script is copied into a throwaway tree so its `.env` lookup resolves
    inside the tmp dir — the dev's own credentials must never be reachable.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "mcp_headers.py"
    target.write_text(Path(mcp_headers.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(
        'HASDATA_API_KEY="hd-test-key"\nAPIFY_TOKEN="ap-test-token"\n', encoding="utf-8"
    )

    for server, expected in (
        ("hasdata", {"x-api-key": "hd-test-key"}),
        ("apify", {"Authorization": "Bearer ap-test-token"}),
    ):
        result = subprocess.run(
            [sys.executable, str(target), server],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed == expected
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items())
