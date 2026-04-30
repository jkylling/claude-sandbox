#!/usr/bin/env python3
"""Recording-and-replaying fake binary.

Symlinked under multiple names (``limactl``, ``tmux``, ``ps``, ``uuidgen``,
``nohup``, ``claude-hooks-server``, ``macos-audio-server``). When invoked,
appends a JSONL record of ``{tool, argv}`` to ``$CLAUDE_SANDBOX_TEST_LOG`` and
returns canned stdout/stderr/exit-code from a scenario file at
``$CLAUDE_SANDBOX_TEST_SCENARIO``.

Scenario file shape::

    {
      "<tool>": [
        {"match": ["arg1", "arg2", ...] | null, "stdout": "...", "stderr": "...", "rc": 0},
        ...
      ],
      ...
    }

The first matching entry wins; ``match: null`` matches anything. If no entry
matches, a tool-specific default is used.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOL = Path(sys.argv[0]).name
ARGV = sys.argv[1:]


def _record() -> None:
    log_path = os.environ.get("CLAUDE_SANDBOX_TEST_LOG")
    if not log_path:
        return
    record = {"tool": TOOL, "argv": ARGV}
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _load_scenario() -> dict:
    sp = os.environ.get("CLAUDE_SANDBOX_TEST_SCENARIO")
    if not sp:
        return {}
    p = Path(sp)
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def _argv_matches(match: list[str] | None, argv: list[str]) -> bool:
    if match is None:
        return True
    return list(match) == list(argv)


def _default(tool: str, argv: list[str]) -> tuple[str, str, int]:
    """Tool-specific default response when no scenario entry matches."""
    if tool == "uuidgen":
        return ("00000000-0000-0000-0000-000000000000\n", "", 0)
    if tool == "ps":
        # When checking liveness of a server pid, pretend it's our server.
        if "-p" in argv and "command=" in argv:
            return ("claude-hooks-server --socket /x\n", "", 0)
        return ("", "", 0)
    if tool == "nohup":
        # Simulate a long-running daemon. We also touch the unix socket the
        # parent polls for, since ``Server.start`` waits for the socket to
        # show up rather than sleeping a fixed delay. The socket path follows
        # ``<binary> --socket <path>`` (hooks server) or ``<binary> <path>``
        # (audio server).
        for i, a in enumerate(argv):
            if a == "--socket" and i + 1 < len(argv):
                Path(argv[i + 1]).touch()
                break
        else:
            if len(argv) >= 2:
                last = argv[-1]
                if last.endswith(".sock"):
                    Path(last).touch()
        # Sleep is short to avoid lingering.
        os.execvp("sleep", ["sleep", "30"])
    if tool == "tmux":
        if argv[:1] == ["display-message"]:
            return ("test-session\n", "", 0)
        return ("", "", 0)
    return ("", "", 0)


def main() -> int:
    _record()
    scenario = _load_scenario()
    for entry in scenario.get(TOOL, []):
        if _argv_matches(entry.get("match"), ARGV):
            sys.stdout.write(entry.get("stdout", ""))
            sys.stderr.write(entry.get("stderr", ""))
            return int(entry.get("rc", 0))
    out, err, rc = _default(TOOL, ARGV)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main())
