"""Behavioral tests for each user-visible claude-sandbox command.

These exercise the Python implementation under the fake-binary harness and
assert the external-tool calls that should (or should not) be emitted.
"""

from __future__ import annotations

import pytest

# Shared scenarios ----------------------------------------------------------

SCENARIO_NO_VMS: dict = {
    "limactl": [
        {"match": ["list", "--format", "{{.Name}} {{.Status}}"], "stdout": ""},
    ]
}

SCENARIO_TWO_RUNNING: dict = {
    "limactl": [
        {
            "match": ["list", "--format", "{{.Name}} {{.Status}}"],
            "stdout": (
                "claude-sandbox-foo Running\nclaude-sandbox-bar Running\nother-vm Running\n"
            ),
        },
    ]
}

SCENARIO_FOO_STOPPED: dict = {
    "limactl": [
        {
            "match": ["list", "--format", "{{.Name}} {{.Status}}"],
            "stdout": "claude-sandbox-foo Stopped\n",
        },
    ]
}

SCENARIO_FOO_RUNNING: dict = {
    "limactl": [
        {
            "match": ["list", "--format", "{{.Name}} {{.Status}}"],
            "stdout": "claude-sandbox-foo Running\n",
        },
    ]
}


# Helpers -------------------------------------------------------------------


def argvs(result, tool: str) -> list[list[str]]:
    """All argv lists captured for a given fake binary."""
    return [c["argv"] for c in result.calls if c["tool"] == tool]


def has_call(result, tool: str, argv: list[str]) -> bool:
    return argv in argvs(result, tool)


def project_dir(home, name: str = "foo"):
    p = home / name
    p.mkdir()
    return p


# help / unknown ------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help(run_py, flag):
    r = run_py([flag])
    assert r.rc == 0
    assert "usage: claude-sandbox" in r.stdout
    assert r.calls == []


def test_unknown_command(run_py):
    r = run_py(["bogus"], SCENARIO_NO_VMS)
    assert r.rc != 0


def test_missing_limactl_fails(run_py):
    """Without limactl on PATH, claude-sandbox should refuse to run."""
    r = run_py(["status"], with_fakes=False)
    assert r.rc != 0


# status --------------------------------------------------------------------


def test_status_no_vms(run_py):
    r = run_py(["status"], SCENARIO_NO_VMS)
    assert r.rc == 0
    assert "No sandbox VMs created yet" in r.stdout


def test_status_with_vms(run_py):
    r = run_py(["status"], SCENARIO_TWO_RUNNING)
    assert r.rc == 0
    assert "claude-sandbox-foo" in r.stdout
    assert "claude-sandbox-bar" in r.stdout
    # The non-sandbox VM should not be reported as a sandbox VM in the listing.
    assert "  other-vm:" not in r.stdout


# stop ----------------------------------------------------------------------


def test_stop_when_running_calls_limactl_stop(run_py, home):
    p = project_dir(home)
    r = run_py(["stop", "-d", str(p)], SCENARIO_FOO_RUNNING)
    assert r.rc == 0
    assert has_call(r, "limactl", ["stop", "claude-sandbox-foo"])


def test_stop_when_stopped_does_not_call_limactl_stop(run_py, home):
    p = project_dir(home)
    r = run_py(["stop", "-d", str(p)], SCENARIO_FOO_STOPPED)
    assert r.rc == 0
    stops = [a for a in argvs(r, "limactl") if a[:1] == ["stop"]]
    assert stops == []


def test_stop_force_passes_force_flag(run_py, home):
    p = project_dir(home)
    r = run_py(["stop", "-f", "-d", str(p)], SCENARIO_FOO_STOPPED)
    assert r.rc == 0
    # -f forces stop even when status is Stopped.
    assert has_call(r, "limactl", ["stop", "-f", "claude-sandbox-foo"])


def test_stop_all_stops_only_sandbox_vms(run_py):
    r = run_py(["stop-all"], SCENARIO_TWO_RUNNING)
    assert r.rc == 0
    stop_calls = [a for a in argvs(r, "limactl") if a[:1] == ["stop"]]
    stopped = [a[-1] for a in stop_calls]
    assert "claude-sandbox-foo" in stopped
    assert "claude-sandbox-bar" in stopped
    assert "other-vm" not in stopped


# delete --------------------------------------------------------------------


def test_delete_existing_calls_limactl_delete(run_py, home):
    p = project_dir(home)
    r = run_py(["delete", "-d", str(p)], SCENARIO_FOO_STOPPED)
    assert r.rc == 0
    assert has_call(r, "limactl", ["delete", "-f", "claude-sandbox-foo"])


def test_delete_nonexistent_does_nothing(run_py, home):
    p = project_dir(home, "ghost")
    r = run_py(["delete", "-d", str(p)], SCENARIO_NO_VMS)
    assert r.rc == 0
    deletes = [a for a in argvs(r, "limactl") if a[:1] == ["delete"]]
    assert deletes == []


# prune ---------------------------------------------------------------------


def test_prune_all_deletes_every_sandbox_vm(run_py):
    r = run_py(["prune"], SCENARIO_TWO_RUNNING)
    assert r.rc == 0
    deleted = [a[-1] for a in argvs(r, "limactl") if a[:1] == ["delete"]]
    assert sorted(deleted) == ["claude-sandbox-bar", "claude-sandbox-foo"]


def test_prune_with_prefix_only_deletes_matching(run_py):
    scenario = {
        "limactl": [
            {
                "match": ["list", "--format", "{{.Name}}"],
                "stdout": "claude-sandbox-foo\nclaude-sandbox-bar\n",
            },
            {
                "match": ["list", "--format", "{{.Name}} {{.Status}}"],
                "stdout": "claude-sandbox-foo Stopped\nclaude-sandbox-bar Stopped\n",
            },
        ]
    }
    r = run_py(["prune", "claude-sandbox-f"], scenario)
    assert r.rc == 0
    deleted = [a[-1] for a in argvs(r, "limactl") if a[:1] == ["delete"]]
    assert deleted == ["claude-sandbox-foo"]


def test_prune_empty_does_nothing(run_py):
    r = run_py(["prune"], SCENARIO_NO_VMS)
    assert r.rc == 0
    deletes = [a for a in argvs(r, "limactl") if a[:1] == ["delete"]]
    assert deletes == []


# start path ----------------------------------------------------------------


def _start_scenario(base_vm: str | None) -> dict:
    """Scenario where the project VM doesn't exist yet."""
    if base_vm is None:
        return {
            "limactl": [
                {"match": ["list", "--format", "{{.Name}} {{.Status}}"], "stdout": ""},
            ]
        }
    return {
        "limactl": [
            {
                "match": ["list", "--format", "{{.Name}} {{.Status}}"],
                "stdout": f"{base_vm} Stopped\n",
            },
        ]
    }


def test_start_via_template_creates_vm(run_py, home):
    p = project_dir(home, "myapp")
    r = run_py(["-d", str(p), "-t", "template:ubuntu"], _start_scenario(base_vm=None))
    assert r.rc == 0
    starts = [a for a in argvs(r, "limactl") if a[:1] == ["start"]]
    assert starts, "expected a 'limactl start ...' call"
    start = starts[0]
    assert "--name=claude-sandbox-myapp" in start
    assert "template:ubuntu" in start


def test_start_via_clone_clones_then_starts(run_py, home):
    p = project_dir(home, "myapp")
    r = run_py(["-d", str(p), "-c", "tools-vm"], _start_scenario(base_vm="tools-vm"))
    assert r.rc == 0
    clone_calls = [a for a in argvs(r, "limactl") if a[:1] == ["clone"]]
    assert clone_calls, "expected a 'limactl clone ...' call"
    assert clone_calls[0][:3] == ["clone", "tools-vm", "claude-sandbox-myapp"]
    # After cloning, the VM must be started.
    assert any(a[:2] == ["start", "claude-sandbox-myapp"] for a in argvs(r, "limactl"))


def test_start_in_tmux_does_not_exec_new_session(run_py, home):
    """When TMUX is set, claude-sandbox should rename the surrounding session
    and run via ``bash -c`` rather than spawning a new tmux session."""
    p = project_dir(home, "myapp")
    r = run_py(
        ["-d", str(p), "-t", "template:ubuntu"],
        _start_scenario(base_vm=None),
        extra_env={"TMUX": "/tmp/fake-tmux-socket,123,0"},
    )
    assert r.rc == 0
    tmux_subs = [a[:1] for a in argvs(r, "tmux")]
    assert ["display-message"] in tmux_subs
    assert ["rename-session"] in tmux_subs
    assert ["new-session"] not in tmux_subs


def test_start_forwards_claude_args(run_py, home):
    """Trailing args after ``--`` are forwarded to the inner ``claude`` invocation."""
    p = project_dir(home, "myapp")
    r = run_py(
        ["-d", str(p), "-t", "template:ubuntu", "--", "--resume"],
        _start_scenario(base_vm=None),
    )
    assert r.rc == 0
    tmux_calls = argvs(r, "tmux")
    assert tmux_calls, "expected the tmux new-session call"
    # The lima command embeds the inner ``claude --resume ... --permission-mode ...``.
    lima_cmd = tmux_calls[-1][-1]
    assert "claude --resume --permission-mode bypassPermissions" in lima_cmd
