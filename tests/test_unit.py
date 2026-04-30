"""Unit tests for pure helpers in claude_sandbox.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "claude_sandbox.py"


@pytest.fixture(scope="module")
def cs():
    """Import claude_sandbox.py as a module despite its non-importable filename."""
    spec = importlib.util.spec_from_file_location("claude_sandbox", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_sandbox"] = mod
    spec.loader.exec_module(mod)
    return mod


# get_vm_name -------------------------------------------------------------


def test_vm_name_simple(cs):
    assert cs.get_vm_name("/tmp/foo") == "claude-sandbox-foo"


def test_vm_name_uppercase(cs):
    assert cs.get_vm_name("/tmp/MyApp") == "claude-sandbox-myapp"


def test_vm_name_special_chars(cs):
    assert cs.get_vm_name("/tmp/foo_bar.baz") == "claude-sandbox-foo-bar-baz"


def test_vm_name_collapses_dashes(cs):
    assert cs.get_vm_name("/tmp/foo___bar") == "claude-sandbox-foo-bar"


def test_vm_name_strips_leading_trailing_dashes(cs):
    assert cs.get_vm_name("/tmp/_foo_") == "claude-sandbox-foo"


def test_vm_name_root(cs):
    assert cs.get_vm_name("/") == "claude-sandbox-root"


def test_vm_name_trailing_slash(cs):
    assert cs.get_vm_name("/tmp/foo/") == "claude-sandbox-foo"


# parse_port_forwards -----------------------------------------------------


def test_parse_ports_single(cs):
    out = json.loads(cs.parse_port_forwards("8080"))
    assert out == [{"guestPort": 8080, "hostPort": 8080}]


def test_parse_ports_mapped(cs):
    out = json.loads(cs.parse_port_forwards("80:8080"))
    assert out == [{"guestPort": 8080, "hostPort": 80}]


def test_parse_ports_multiple(cs):
    out = json.loads(cs.parse_port_forwards("80:8080,9090,5642:1234"))
    assert out == [
        {"guestPort": 8080, "hostPort": 80},
        {"guestPort": 9090, "hostPort": 9090},
        {"guestPort": 1234, "hostPort": 5642},
    ]


def test_parse_ports_invalid_exits(cs):
    with pytest.raises(SystemExit):
        cs.parse_port_forwards("notaport")


# claude_set_args ---------------------------------------------------------


def test_claude_set_args_minimal(cs, monkeypatch):
    monkeypatch.setattr(cs.getpass, "getuser", lambda: "alice")
    args = cs.claude_set_args(
        project_dir="/proj",
        extra_ports="",
        settings_dir="/settings",
        enable_voice=False,
        vm_cpus="",
        vm_memory="",
        vm_disk="",
    )
    # Always at least: portForwards (base), provision, mounts, env -> 4 pairs.
    assert args.count("--set") == 4
    pairs = list(zip(args[::2], args[1::2], strict=True))
    keys = [v.split(" ", 1)[0] for _, v in pairs]
    assert keys == [".portForwards", ".provision", ".mounts", ".env"]


def test_claude_set_args_with_voice_and_ports(cs, monkeypatch):
    monkeypatch.setattr(cs.getpass, "getuser", lambda: "alice")
    args = cs.claude_set_args(
        project_dir="/proj",
        extra_ports="80",
        settings_dir="/settings",
        enable_voice=True,
        vm_cpus="4",
        vm_memory="8GiB",
        vm_disk="50GiB",
    )
    pairs = list(zip(args[::2], args[1::2], strict=True))
    keys = [v.split(" ", 1)[0] for _, v in pairs]
    assert keys == [
        ".portForwards",  # base
        ".portForwards",  # audio (voice)
        ".portForwards",  # extra ports
        ".provision",
        ".mounts",
        ".env",
        ".cpus",
        ".memory",
        ".disk",
    ]


def test_claude_set_args_mount_path_uses_user(cs, monkeypatch):
    monkeypatch.setattr(cs.getpass, "getuser", lambda: "alice")
    args = cs.claude_set_args(
        project_dir="/proj",
        extra_ports="",
        settings_dir="/settings",
        enable_voice=False,
        vm_cpus="",
        vm_memory="",
        vm_disk="",
    )
    mounts_value = next(
        v for _k, v in zip(args[::2], args[1::2], strict=True) if v.startswith(".mounts")
    )
    payload = mounts_value[len(".mounts += ") :]
    parsed = json.loads(payload)
    assert parsed[1]["mountPoint"] == "/home/alice.linux/.claude-shared"
    assert parsed[1]["location"] == "/settings"
    assert parsed[0] == {"location": "/proj", "writable": True}


# parse_args --------------------------------------------------------------


def test_parse_args_default(cs):
    a = cs.parse_args([])
    assert a.command == ""
    assert a.claude_args == []
    assert a.project_dir == ""


def test_parse_args_dir_and_command(cs):
    a = cs.parse_args(["shell", "-d", "/tmp"])
    assert a.command == "shell"
    assert a.project_dir == "/tmp"


def test_parse_args_dashdash_passthrough(cs):
    a = cs.parse_args(["--", "--resume", "foo"])
    assert a.command == ""
    assert a.claude_args == ["--resume", "foo"]


def test_parse_args_voice_flag(cs):
    a = cs.parse_args(["--voice"])
    assert a.enable_voice is True


def test_parse_args_resource_flags(cs):
    a = cs.parse_args(["--cpus", "4", "--mem", "8GiB", "--disk", "50GiB"])
    assert (a.vm_cpus, a.vm_memory, a.vm_disk) == ("4", "8GiB", "50GiB")


def test_parse_args_prune_with_prefix(cs):
    a = cs.parse_args(["prune", "claude-sandbox-x"])
    assert a.command == "prune"
    assert a.prune_prefix == "claude-sandbox-x"


def test_parse_args_unknown_exits(cs):
    with pytest.raises(SystemExit):
        cs.parse_args(["bogus"])


def test_parse_args_help_short_exits(cs):
    # argparse's HelpAction prints help to stdout and exits 0.
    with pytest.raises(SystemExit):
        cs.parse_args(["-h"])


# Command emission --------------------------------------------------------


def test_build_lima_shell_command_quotes_project(cs, monkeypatch):
    monkeypatch.setattr(cs, "_uuidgen", lambda: "UUID")
    out = cs._build_lima_shell_command("vm1", "/proj", "claude --foo")
    assert out.startswith("limactl shell vm1 bash -l -c ")
    assert "cd /proj" in out
    assert "CLAUDE_SANDBOX_UUID=UUID" in out
    assert "CLAUDE_HOOKS_SOCKET=/tmp/claude-hooks.sock" in out
    assert "claude --foo" in out


def test_build_lima_shell_command_handles_spaces(cs, monkeypatch):
    """A project path with a space should round-trip through shell quoting."""
    import shlex

    monkeypatch.setattr(cs, "_uuidgen", lambda: "UUID")
    out = cs._build_lima_shell_command("vm1", "/My Project", "claude")
    # The wrapper passes the final token to ``bash -l -c``. Splitting back via
    # shlex recovers the inner command exactly as bash would see it.
    parts = shlex.split(out)
    assert parts[:5] == ["limactl", "shell", "vm1", "bash", "-l"]
    # parts[6] is the string passed to ``bash -c``; bash itself will then
    # process the inner ``'...'`` quoting.
    inner = parts[6]
    assert "cd '/My Project' &&" in inner
    # Round-tripping through bash recovers the original path.
    assert shlex.split(inner)[:2] == ["cd", "/My Project"]


def test_build_shell_only_command(cs):
    out = cs._build_shell_only_command("vm1", "/proj")
    assert "cd /proj" in out
    assert "exec bash -il" in out
