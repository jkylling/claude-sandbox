"""Shared fixtures and helpers for claude-sandbox tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKES_DIR = Path(__file__).resolve().parent / "fakes"
PYTHON_WRAPPER = REPO_ROOT / "claude-sandbox"
PYTHON_SCRIPT = REPO_ROOT / "claude_sandbox.py"


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str
    calls: list[dict] = field(default_factory=list)


def _build_env(home: Path, log: Path, scenario: Path, *, with_fakes: bool = True) -> dict[str, str]:
    """Build the environment for running a script under fakes.

    With ``with_fakes=False`` the fake-binary directory is omitted from PATH,
    simulating a host that doesn't have ``limactl`` installed.
    """
    python_dir = str(Path(sys.executable).parent)
    path_dirs: list[str] = []
    if with_fakes:
        path_dirs.append(str(FAKES_DIR))
    path_dirs += [python_dir, "/usr/bin", "/bin"]
    env = {
        "HOME": str(home),
        "USER": "testuser",
        "PATH": ":".join(path_dirs),
        "CLAUDE_SANDBOX_TEST_LOG": str(log),
        "CLAUDE_SANDBOX_TEST_SCENARIO": str(scenario),
        # Ensure deterministic locale and disable any inherited claude-sandbox env.
        "LC_ALL": "C",
        "LANG": "C",
        "TERM": "dumb",
    }
    return env


def _read_calls(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


_run_counter = {"n": 0}


def _run(
    script: Path,
    args: list[str],
    scenario: dict,
    *,
    extra_env: dict[str, str] | None = None,
    with_fakes: bool = True,
) -> Result:
    """Run ``script`` in a fresh per-call HOME so bash and python invocations
    cannot influence each other through leftover state (pidfiles, sockets,
    sleeping fake-nohup processes, etc.)."""
    parent = Path(os.environ["PYTEST_HOME"])
    parent.mkdir(parents=True, exist_ok=True)
    _run_counter["n"] += 1
    home = parent / f"run-{_run_counter['n']}"
    home.mkdir()
    (home / ".claude-sandbox").mkdir()
    log = home / "calls.jsonl"
    scn = home / "scenario.json"
    scn.write_text(json.dumps(scenario))

    # If args reference paths under the *parent* home (e.g. project dir),
    # mirror them into this run's home so the script sees a real directory.
    rewritten: list[str] = []
    for a in args:
        if a.startswith(str(parent)) and not a.startswith(str(home)):
            rel = Path(a).relative_to(parent)
            new = home / rel
            new.mkdir(parents=True, exist_ok=True)
            rewritten.append(str(new))
        else:
            rewritten.append(a)

    env = _build_env(home, log, scn, with_fakes=with_fakes)
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [str(script), *rewritten],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return Result(
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        calls=_read_calls(log),
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated HOME for a test and expose it via env to helpers."""
    h = tmp_path / "home"
    h.mkdir()
    (h / ".claude-sandbox").mkdir()
    monkeypatch.setenv("PYTEST_HOME", str(h))
    return h


@pytest.fixture()
def run_py(home: Path):  # noqa: ARG001
    def go(args: list[str], scenario: dict | None = None, **kw) -> Result:
        return _run(PYTHON_WRAPPER, args, scenario or {}, **kw)

    return go


@pytest.fixture()
def run_py_direct(home: Path):  # noqa: ARG001
    """Invoke claude_sandbox.py directly (no bash wrapper) for unit-style tests."""

    def go(args: list[str], scenario: dict | None = None, **kw) -> Result:
        return _run(PYTHON_SCRIPT, args, scenario or {}, **kw)

    return go
