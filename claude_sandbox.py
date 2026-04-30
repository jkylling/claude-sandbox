#!/usr/bin/env python3
"""claude-sandbox — run Claude Code in isolated Lima VMs.

Creates project VMs from a YAML template (default) or by cloning an existing
VM. Pure-stdlib so it can be invoked directly without a venv.

Designed to be driven by the small ``claude-sandbox`` bash wrapper. All log
output goes to stderr. Normal user-facing text (help, status) goes to stdout
with exit code 0. When the script wants the wrapper to ``eval`` a final
command (start / shell), it prints the command on stdout and exits with code
``EVAL_EXIT_CODE`` (100).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EVAL_EXIT_CODE = 100

SCRIPT_DIR = Path(__file__).resolve().parent
SANDBOX_DIR = Path.home() / ".claude-sandbox"

# Lima portForwards fragments shared by all project VMs
CLAUDE_PORT_FORWARDS_BASE = [
    {
        "guestSocket": "/tmp/claude-hooks.sock",
        "hostSocket": "{{.Home}}/.claude-sandbox/claude-hooks.sock",
        "reverse": True,
    }
]
CLAUDE_PORT_FORWARDS_AUDIO = [
    {
        "guestSocket": "/tmp/audio-proxy.sock",
        "hostSocket": "{{.Home}}/.claude-sandbox/audio-proxy.sock",
        "reverse": True,
    }
]

# Provision script run inside each project VM the first time it boots.
CLAUDE_PROVISION_USER = r"""#!/bin/bash
set -eux
STAMP="$HOME/.claude-sandbox-provisioned"
if [ -f "$STAMP" ]; then exit 0; fi

# Ensure ~/.local/bin is in PATH (for login shells via .profile)
if ! grep -q 'local/bin' ~/.profile 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
fi
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude &> /dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi

# Symlink shared Claude config files into the local ~/.claude directory.
# ~/.claude-shared is a virtiofs mount shared across VMs; ~/.claude is local.
# Only credentials, settings, hooks, and CLAUDE.md are shared — runtime state
# like .claude.json stays per-VM to avoid cross-VM write races (virtiofs flock
# doesn't provide mutual exclusion between VMs).
SHARED="$HOME/.claude-shared"
LOCAL="$HOME/.claude"
mkdir -p "$LOCAL"
for name in .credentials.json settings.json hooks CLAUDE.md; do
  if [ -e "$SHARED/$name" ]; then
    ln -sf "$SHARED/$name" "$LOCAL/$name"
  fi
done

# Seed ~/.claude.json (the global runtime state file, separate from ~/.claude/)
# so the onboarding wizard doesn't re-trigger. Merges into any existing content
# since the install script may have already created this file.
python3 -c "
import json, os
path = os.path.expanduser('~/.claude.json')
try:
    with open(path) as f: cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError): cfg = {}
cfg.update({'hasCompletedOnboarding': True, 'theme': 'dark', 'installMethod': 'native', 'autoUpdates': False})
with open(path, 'w') as f: json.dump(cfg, f)
os.chmod(path, 0o600)
"

touch "$STAMP"
""".rstrip("\n")

# ANSI colors (only used when stderr is a TTY)
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _color(code: str) -> str:
    return code if sys.stderr.isatty() else ""


def log_info(msg: str) -> None:
    print(f"{_color(GREEN)}[claude-sandbox]{_color(NC)} {msg}", file=sys.stderr)


def log_warn(msg: str) -> None:
    print(f"{_color(YELLOW)}[claude-sandbox]{_color(NC)} {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    print(f"{_color(RED)}[claude-sandbox]{_color(NC)} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _from_env(arg_value: str, env_var: str, default: str = "") -> str:
    """Resolve a config value: explicit CLI flag wins, then env var, then default."""
    return arg_value or os.environ.get(env_var) or default


def parse_port_forwards(spec: str) -> str:
    """Parse "80:8080,9090" into a JSON list of {guestPort, hostPort} dicts."""
    entries: list[dict[str, int]] = []
    for mapping in spec.split(","):
        if ":" in mapping:
            host_s, guest_s = mapping.split(":", 1)
        else:
            host_s = guest_s = mapping
        if not (host_s.isdigit() and guest_s.isdigit()):
            log_error(f"Invalid port mapping: {mapping}")
            sys.exit(1)
        entries.append({"guestPort": int(guest_s), "hostPort": int(host_s)})
    return json.dumps(entries, separators=(",", ":"))


def get_vm_name(project_dir: str) -> str:
    """Sanitize a directory path into a Lima VM name."""
    base = os.path.basename(project_dir.rstrip("/"))
    if not base or base == "/":
        base = "root"
    base = base.lower()
    base = re.sub(r"[^a-z0-9]", "-", base)
    base = re.sub(r"-+", "-", base)
    base = base.strip("-")
    return f"claude-sandbox-{base}"


def claude_set_args(
    project_dir: str,
    extra_ports: str,
    settings_dir: str,
    enable_voice: bool,
    vm_cpus: str | None,
    vm_memory: str | None,
    vm_disk: str | None,
) -> list[str]:
    """Build the list of ``--set`` flags Lima expects when creating a VM."""
    user = getpass.getuser()
    # Lima's macOS/vz convention exposes the host user as ``<user>.linux`` inside the
    # VM. On other Lima drivers the home is plain ``/home/<user>``; revisit if/when
    # this tool ever runs against non-vz drivers.
    sep = (",", ":")
    provision = json.dumps([{"mode": "user", "script": CLAUDE_PROVISION_USER}], separators=sep)
    mounts = json.dumps(
        [
            {"location": project_dir, "writable": True},
            {
                "location": settings_dir,
                "mountPoint": f"/home/{user}.linux/.claude-shared",
                "writable": True,
            },
        ],
        separators=sep,
    )
    env = json.dumps({"UV_PROJECT_ENVIRONMENT": ".venv-agent"}, separators=sep)

    base_ports = json.dumps(CLAUDE_PORT_FORWARDS_BASE, separators=sep)
    audio_ports = json.dumps(CLAUDE_PORT_FORWARDS_AUDIO, separators=sep)

    args: list[str] = []
    args += ["--set", f".portForwards += {base_ports}"]
    if enable_voice:
        args += ["--set", f".portForwards += {audio_ports}"]
    if extra_ports:
        args += ["--set", f".portForwards += {parse_port_forwards(extra_ports)}"]
    args += ["--set", f".provision += {provision}"]
    args += ["--set", f".mounts += {mounts}"]
    args += ["--set", f".env += {env}"]
    if vm_cpus:
        args += ["--set", f".cpus = {vm_cpus}"]
    if vm_memory:
        args += ["--set", f'.memory = "{vm_memory}"']
    if vm_disk:
        args += ["--set", f'.disk = "{vm_disk}"']
    return args


# ---------------------------------------------------------------------------
# External-tool wrappers
# ---------------------------------------------------------------------------


def _run(cmd: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper that always captures text and never raises."""
    return subprocess.run(list(cmd), text=True, capture_output=True, check=False)


def check_lima() -> None:
    if shutil.which("limactl") is None:
        log_error("limactl not found. Please install Lima first: brew install lima")
        sys.exit(1)


# Cached ``limactl list`` output. Reset via ``_invalidate_vm_cache`` whenever
# we mutate VM state (start/stop/clone/delete). Within a single command this
# turns N+ separate ``limactl list`` invocations into one.
_vm_status_cache: dict[str, str] | None = None


def _vm_status_map() -> dict[str, str]:
    global _vm_status_cache
    if _vm_status_cache is None:
        out = _run(["limactl", "list", "--format", "{{.Name}} {{.Status}}"]).stdout
        cache: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if parts:
                cache[parts[0]] = parts[1] if len(parts) == 2 else ""
        _vm_status_cache = cache
    return _vm_status_cache


def _invalidate_vm_cache() -> None:
    global _vm_status_cache
    _vm_status_cache = None


def vm_exists(name: str) -> bool:
    return name in _vm_status_map()


def get_vm_status(name: str) -> str:
    return _vm_status_map().get(name, "")


def is_vm_running(name: str) -> bool:
    return get_vm_status(name) == "Running"


def is_vm_in_error(name: str) -> bool:
    return get_vm_status(name) == "Broken"


def list_sandbox_vms(prefix: str = "claude-sandbox") -> list[str]:
    return [n for n in _vm_status_map() if n.startswith(prefix)]


# ---------------------------------------------------------------------------
# Hooks / audio sidecar servers
# ---------------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    """Equivalent of ``kill -0 PID``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command(pid: int) -> str:
    """``ps -p PID -o command=``, returns the command line or empty string."""
    return _run(["ps", "-p", str(pid), "-o", "command="]).stdout.strip()


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


class Server:
    """A nohup-spawned host-side sidecar (hooks server or audio proxy).

    Bundles every piece of state that ``start``/``stop``/``is_running`` need
    so the two server flavors don't need parallel functions. Pidfile, log and
    socket paths are derived from ``name`` / ``socket_name`` under
    ``SANDBOX_DIR``.
    """

    def __init__(
        self,
        *,
        label: str,
        name: str,
        binary: Path,
        socket_name: str,
        binary_args: Callable[[Path], list[str]],
        signature: str,
        start_failure_fatal: bool,
    ) -> None:
        self.label = label
        self.binary = binary
        self.socket = SANDBOX_DIR / socket_name
        self.pidfile = SANDBOX_DIR / f"{name}.pid"
        self.log = SANDBOX_DIR / f"{name}.log"
        self.binary_args = binary_args(self.socket)
        self.signature = signature
        self.start_failure_fatal = start_failure_fatal

    def is_running(self) -> bool:
        if self.pidfile.is_file():
            try:
                pid = int(self.pidfile.read_text().strip())
            except ValueError:
                pid = 0
            if pid and _process_alive(pid) and self.signature in _process_command(pid):
                return True
        # Stale: clean up
        _unlink_quiet(self.socket)
        _unlink_quiet(self.pidfile)
        return False

    def start(self) -> None:
        if self.is_running():
            return
        log_info(f"Starting {self.label}...")
        with open(self.log, "ab") as log_fh:
            proc = subprocess.Popen(
                ["nohup", str(self.binary), *self.binary_args],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self.pidfile.write_text(f"{proc.pid}\n")
        # Poll for the socket showing up rather than sleeping a fixed second:
        # under load the server may take >1s to bind, but on the happy path we
        # want to return as soon as it's actually ready.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.socket.exists() and self.is_running():
                return
            if not _process_alive(proc.pid):
                break
            time.sleep(0.1)
        if self.start_failure_fatal:
            log_error(f"Failed to start {self.label}. Check {self.log}")
            try:
                sys.stderr.write(self.log.read_text())
            except OSError:
                pass
            sys.exit(1)
        log_warn(f"Failed to start {self.label}. Check {self.log}")

    def stop(self) -> None:
        if self.pidfile.is_file():
            try:
                pid = int(self.pidfile.read_text().strip())
            except ValueError:
                pid = 0
            if pid > 0:
                self._terminate(pid)
            _unlink_quiet(self.pidfile)
        _unlink_quiet(self.socket)

    def _terminate(self, pid: int) -> None:
        """Send SIGTERM, wait up to 3s, then SIGKILL if still alive."""
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                log_info(f"Stopped {self.label}")
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
            log_warn(f"Force-killed {self.label} (pid {pid}) after timeout")
        except OSError:
            pass


HOOKS = Server(
    label="hooks server",
    name="claude-hooks-server",
    binary=SCRIPT_DIR / "claude-hooks-server",
    socket_name="claude-hooks.sock",
    binary_args=lambda sock: ["--socket", str(sock)],
    signature="claude-hooks-server",
    start_failure_fatal=True,
)

AUDIO = Server(
    label="audio server",
    name="audio-server",
    binary=SCRIPT_DIR / "audio-proxy" / "macos-audio-server",
    socket_name="audio-proxy.sock",
    binary_args=lambda sock: [str(sock)],
    signature="macos-audio-server",
    start_failure_fatal=False,
)


# ---------------------------------------------------------------------------
# VM lifecycle
# ---------------------------------------------------------------------------


def create_vm_from_clone(vm_name: str, project_dir: str, base_vm: str, set_args: list[str]) -> None:
    if not vm_exists(base_vm):
        log_error(f"Base VM '{base_vm}' not found.")
        log_error("Create it first, then try again.")
        sys.exit(1)
    if is_vm_running(base_vm):
        log_info("Stopping base VM for cloning...")
        subprocess.run(["limactl", "stop", base_vm], check=True)
        _invalidate_vm_cache()
    log_info(f"Cloning '{base_vm}' to '{vm_name}'...")
    subprocess.run(["limactl", "clone", base_vm, vm_name, *set_args, "-y"], check=True)
    _invalidate_vm_cache()
    log_info(f"Project VM '{vm_name}' created from clone")


def create_vm_from_template(
    vm_name: str, project_dir: str, template: str, set_args: list[str]
) -> None:
    log_info(f"Creating '{vm_name}' from template...")
    log_warn("This may take a while on first run...")
    subprocess.run(
        ["limactl", "start", f"--name={vm_name}", template, *set_args, "-y"],
        check=True,
    )
    _invalidate_vm_cache()
    log_info(f"Project VM '{vm_name}' created from template")


def stop_vm(name: str, force: bool = False) -> None:
    if is_vm_running(name) or force:
        log_info(f"Stopping VM '{name}'...")
        cmd = ["limactl", "stop"]
        if force:
            cmd.append("-f")
        cmd.append(name)
        subprocess.run(cmd, check=True)
        _invalidate_vm_cache()


def stop_all_vms(force: bool = False) -> None:
    log_info("Stopping all sandbox VMs...")
    for vm in list_sandbox_vms():
        stop_vm(vm, force)
    HOOKS.stop()
    AUDIO.stop()
    log_info("All stopped")


def delete_vm(name: str) -> None:
    if not vm_exists(name):
        return
    stop_vm(name)
    log_info(f"Deleting VM '{name}'...")
    subprocess.run(["limactl", "delete", "-f", name], check=True)
    _invalidate_vm_cache()


def prune_project_vms(prefix: str = "claude-sandbox") -> None:
    log_info(f"Deleting project VMs matching prefix '{prefix}'...")
    vms = list_sandbox_vms(prefix)
    for vm in vms:
        delete_vm(vm)
    if not vms:
        log_info(f"No project VMs matching '{prefix}' to delete")
    else:
        log_info(f"Deleted {len(vms)} project VM(s)")


def ensure_vm_running(vm_name: str, *, was_just_created: bool = False) -> None:
    """Bring a VM up. Recovers from the ``Broken`` state by stopping first."""
    if is_vm_running(vm_name):
        if not was_just_created:
            log_info(f"VM '{vm_name}' already running")
        return
    if is_vm_in_error(vm_name):
        log_warn(f"VM '{vm_name}' is in error state, stopping before restart...")
        subprocess.run(["limactl", "stop", vm_name], check=False)
        _invalidate_vm_cache()
    log_info(f"Starting VM '{vm_name}'...")
    subprocess.run(["limactl", "start", vm_name, "-y"], check=True)
    _invalidate_vm_cache()


# ---------------------------------------------------------------------------
# Status / help
# ---------------------------------------------------------------------------


def show_status(base_vm: str, template: str) -> None:
    print("=== Claude Sandbox Status ===")
    print()
    print(f"Base VM: {base_vm or '(not set)'}")
    print(f"Template: {template or '(not set)'}")
    print()
    print("Hooks server: " + ("Running" if HOOKS.is_running() else "Not running"))
    print("Audio server: " + ("Running" if AUDIO.is_running() else "Not running"))
    print()
    print("VMs:")
    found = False
    for vm in list_sandbox_vms():
        found = True
        print(f"  {vm}: {get_vm_status(vm)}")
    if not found:
        print("  No sandbox VMs created yet")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

# Defaults applied after argparse runs. We use ``argparse.SUPPRESS`` on every
# option so the subparser doesn't clobber values the top-level parser already
# set when the same flag appears on both sides of a subcommand.
_DEFAULTS = {
    "project_dir": "",
    "settings_dir": "",
    "base_vm": "",
    "template": "",
    "port_forwards": "",
    "vm_cpus": "",
    "vm_memory": "",
    "vm_disk": "",
    "enable_voice": False,
    "force_stop": False,
    "command": "",
    "prune_prefix": "",
}

_SUBCOMMAND_HELP = {
    "shell": "Open a shell in the VM instead of Claude",
    "stop": "Stop the project's VM",
    "delete": "Delete the project's VM",
    "status": "Show all sandbox VMs status",
    "stop-all": "Stop all sandbox VMs",
    "restart-hooks": "Restart the hooks server",
    "prune": "Delete all project VMs (optionally matching PREFIX)",
}

_EPILOG = """\
Pass arguments to claude after a literal '--' (e.g. claude-sandbox -- --resume).

environment variables:
  CLAUDE_SANDBOX_BASE_VM     Default VM to clone from
  CLAUDE_SANDBOX_TEMPLATE    Default template to use
  CLAUDE_SANDBOX_SETTINGS    Claude settings directory
  CLAUDE_SANDBOX_VOICE_MODE  Enable voice mode when set to "true"
"""


def _add_common_options(p: argparse.ArgumentParser) -> None:
    """Add options shared by the top-level parser and every subparser."""
    s = argparse.SUPPRESS
    p.add_argument(
        "-d",
        "--dir",
        dest="project_dir",
        metavar="DIR",
        default=s,
        help="Project directory (default: current directory)",
    )
    p.add_argument(
        "-s",
        "--settings",
        dest="settings_dir",
        metavar="DIR",
        default=s,
        help="Claude settings directory (default: ~/.claude-sandbox/.claude)",
    )
    p.add_argument(
        "-c",
        "--clone-from",
        dest="base_vm",
        metavar="VM",
        default=s,
        help="Clone from an existing VM",
    )
    p.add_argument(
        "-t",
        "--template",
        dest="template",
        metavar="YAML",
        default=s,
        help="Create from a YAML template (default: template:default)",
    )
    p.add_argument(
        "-p",
        "--ports",
        dest="port_forwards",
        metavar="PORTS",
        default=s,
        help="Expose ports, e.g. 80:8080,9090 (hostPort:guestPort)",
    )
    p.add_argument(
        "--voice",
        dest="enable_voice",
        action="store_true",
        default=s,
        help="Enable voice mode (proxy host mic into VM)",
    )
    p.add_argument(
        "--cpus", dest="vm_cpus", metavar="N", default=s, help="Number of CPUs for the VM"
    )
    p.add_argument(
        "--mem",
        dest="vm_memory",
        metavar="SIZE",
        default=s,
        help="Memory for the VM (e.g. 8GiB)",
    )
    p.add_argument(
        "--disk",
        dest="vm_disk",
        metavar="SIZE",
        default=s,
        help="Disk size for the VM (e.g. 60GiB)",
    )
    p.add_argument(
        "-f",
        "--force",
        dest="force_stop",
        action="store_true",
        default=s,
        help="Force stop VM (use with stop/stop-all)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-sandbox",
        description=(
            "Run Claude Code in an isolated Lima VM. With no command, starts "
            "Claude in the project's VM."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_options(parser)
    sub = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    for cmd, help_text in _SUBCOMMAND_HELP.items():
        sp = sub.add_parser(cmd, help=help_text, description=help_text)
        _add_common_options(sp)
        if cmd == "prune":
            sp.add_argument("prune_prefix", nargs="?", metavar="PREFIX", default=argparse.SUPPRESS)
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse ``argv``. Anything after a literal ``--`` is captured as
    ``claude_args`` and forwarded to the inner ``claude`` invocation."""
    try:
        sep = argv.index("--")
        main_argv, claude_args = argv[:sep], argv[sep + 1 :]
    except ValueError:
        main_argv, claude_args = argv, []
    ns = _build_parser().parse_args(main_argv)
    for key, default in _DEFAULTS.items():
        if not hasattr(ns, key) or getattr(ns, key) is None:
            setattr(ns, key, default)
    ns.claude_args = claude_args
    return ns


# ---------------------------------------------------------------------------
# Final command emission
# ---------------------------------------------------------------------------


def _uuidgen() -> str:
    return str(uuid.uuid4())


def _wrap_in_lima_shell(vm_name: str, inner: str) -> str:
    return f"limactl shell {shlex.quote(vm_name)} bash -l -c {shlex.quote(inner)}"


def _build_lima_shell_command(vm_name: str, project_dir: str, claude_cmd: str) -> str:
    """Build the bash command sent to ``limactl shell ... bash -l -c <cmd>``."""
    inner = (
        f"cd {shlex.quote(project_dir)} && "
        f"CLAUDE_SANDBOX_UUID={_uuidgen()} "
        f"CLAUDE_HOOKS_SOCKET=/tmp/claude-hooks.sock "
        f"{claude_cmd}"
    )
    return _wrap_in_lima_shell(vm_name, inner)


def _build_shell_only_command(vm_name: str, project_dir: str) -> str:
    return _wrap_in_lima_shell(vm_name, f"cd {shlex.quote(project_dir)} && exec bash -il")


def _maybe_rename_tmux_session() -> None:
    """If running inside tmux, prefix the current session name with claude-sandbox-."""
    current = _run(["tmux", "display-message", "-p", "#S"]).stdout.strip()
    if current and not current.startswith("claude"):
        subprocess.run(["tmux", "rename-session", f"claude-sandbox-{current}"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    a = parse_args(argv)

    check_lima()
    _ensure_dir(SANDBOX_DIR)

    base_vm = _from_env(a.base_vm, "CLAUDE_SANDBOX_BASE_VM")
    template = _from_env(a.template, "CLAUDE_SANDBOX_TEMPLATE", "template:default")
    settings_dir = _from_env(
        a.settings_dir, "CLAUDE_SANDBOX_SETTINGS", str(SANDBOX_DIR / ".claude")
    )
    enable_voice = (
        a.enable_voice or os.environ.get("CLAUDE_SANDBOX_VOICE_MODE", "").lower() == "true"
    )

    project_dir = a.project_dir or os.getcwd()
    if a.project_dir and not os.path.isdir(a.project_dir):
        log_error(f"Directory not found: {a.project_dir}")
        return 1
    project_dir = str(Path(project_dir).resolve())

    _ensure_dir(Path(settings_dir))
    vm_name = get_vm_name(project_dir)

    cmd = a.command
    if cmd == "stop":
        stop_vm(vm_name, a.force_stop)
        log_info("Stopped")
        return 0
    if cmd == "stop-all":
        stop_all_vms(a.force_stop)
        return 0
    if cmd == "restart-hooks":
        HOOKS.stop()
        HOOKS.start()
        log_info("Hooks server restarted")
        return 0
    if cmd == "status":
        show_status(base_vm, template)
        return 0
    if cmd == "delete":
        delete_vm(vm_name)
        log_info("Deleted")
        return 0
    if cmd == "prune":
        prune_project_vms(a.prune_prefix or "claude-sandbox")
        return 0

    # default + shell paths require the VM to be up
    HOOKS.start()
    if enable_voice:
        AUDIO.start()

    set_args = claude_set_args(
        project_dir,
        a.port_forwards,
        settings_dir,
        enable_voice,
        a.vm_cpus,
        a.vm_memory,
        a.vm_disk,
    )

    if not vm_exists(vm_name):
        if base_vm:
            create_vm_from_clone(vm_name, project_dir, base_vm, set_args)
            ensure_vm_running(vm_name, was_just_created=True)
        else:
            create_vm_from_template(vm_name, project_dir, template, set_args)
    else:
        if a.port_forwards:
            if is_vm_running(vm_name):
                log_error(f"Cannot add ports to running VM '{vm_name}'")
                log_error("Stop the VM first: claude-sandbox stop")
                return 1
            ports_json = parse_port_forwards(a.port_forwards)
            log_info(f"Adding port forwards: {a.port_forwards}")
            subprocess.run(
                [
                    "limactl",
                    "edit",
                    vm_name,
                    "--set",
                    f".portForwards += {ports_json}",
                    "-y",
                ],
                check=True,
            )
        ensure_vm_running(vm_name)

    log_info(f"Project: {project_dir}")
    print("", file=sys.stderr)  # blank line, matching bash `echo`

    if cmd == "shell":
        # exec into a login shell inside the VM
        print(f"exec {_build_shell_only_command(vm_name, project_dir)}")
        return EVAL_EXIT_CODE

    # Default: start Claude
    claude_cmd = "claude"
    for arg in a.claude_args:
        claude_cmd += " " + shlex.quote(arg)
    claude_cmd += " --permission-mode bypassPermissions"

    lima_cmd = _build_lima_shell_command(vm_name, project_dir, claude_cmd)

    if os.environ.get("TMUX"):
        _maybe_rename_tmux_session()
        # Don't exec — keep the tmux pane after the inner shell exits.
        print(f"bash -c {shlex.quote(lima_cmd)}")
    else:
        session = f"{vm_name}-{os.getpid()}"
        print(f"exec tmux new-session -s {shlex.quote(session)} {shlex.quote(lima_cmd)}")
    return EVAL_EXIT_CODE


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
