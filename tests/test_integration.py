"""End-to-end integration tests using a real Lima VM.

These tests are slow (multiple minutes) and require:
  - ``limactl`` on ``PATH``
  - A working virtualization backend that can boot the bundled ``template:alpine``:
    ``vz`` on macOS, or KVM on Linux with hardware virtualization. Pure software
    emulation (QEMU TCG) is too unstable for this suite and is skipped.

They are skipped by default. Run them explicitly with::

    uv run pytest -m integration

Environment overrides:
  - ``CLAUDE_SANDBOX_TEST_TEMPLATE``: use a custom Lima template/path instead
    of ``template:alpine``. Useful when the default image isn't available on
    the host's architecture.
  - ``CLAUDE_SANDBOX_TEST_BOOT_TIMEOUT``: seconds to wait for the
    ``shell``-create flow before failing the test (default 1800).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "claude-sandbox"

DEFAULT_TEMPLATE = os.environ.get("CLAUDE_SANDBOX_TEST_TEMPLATE", "template:alpine")
BOOT_TIMEOUT_S = int(os.environ.get("CLAUDE_SANDBOX_TEST_BOOT_TIMEOUT", "1800"))


def _has_virt_acceleration() -> bool:
    """True when the host can boot a Lima VM with hardware acceleration.

    macOS supports the ``vz`` framework. Linux requires ``/dev/kvm``. Anything
    else falls back to QEMU TCG, which we treat as unsupported because boots
    take 5-15 minutes and the guest agent is flaky on aarch64-on-aarch64 TCG.
    """
    if sys.platform == "darwin":
        return True
    return os.path.exists("/dev/kvm")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("limactl") is None, reason="limactl not on PATH"),
    pytest.mark.skipif(
        not _has_virt_acceleration(),
        reason="hardware virtualization unavailable (no vz on macOS, no /dev/kvm on Linux)",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sandbox(*args: str, timeout: float = BOOT_TIMEOUT_S, **kw) -> subprocess.CompletedProcess:
    """Run claude-sandbox in its own session so pytest's signal handlers don't
    propagate to the Lima host agent. ``stdin`` is closed so the interactive
    shell exits cleanly when the VM reaches READY."""
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        **kw,
    )


def lima(*args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["limactl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def vm_name_for(project: Path) -> str:
    """Mirror ``claude_sandbox.get_vm_name``'s slug rules."""
    import re

    base = project.name.lower() or "root"
    base = re.sub(r"[^a-z0-9]", "-", base).strip("-")
    base = re.sub(r"-+", "-", base)
    return f"claude-sandbox-{base}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A unique project dir with a host-side marker file."""
    p = tmp_path / f"e2e-{uuid.uuid4().hex[:8]}"
    p.mkdir()
    (p / "marker.txt").write_text("hello-from-host\n")
    return p


@pytest.fixture
def cleanup_sandbox():
    """Best-effort teardown: stop+delete every sandbox VM and the hooks server."""
    yield
    subprocess.run(
        [str(SCRIPT), "stop-all"], capture_output=True, timeout=120, stdin=subprocess.DEVNULL
    )
    subprocess.run(
        [str(SCRIPT), "prune"], capture_output=True, timeout=600, stdin=subprocess.DEVNULL
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_lifecycle(project: Path, cleanup_sandbox):
    """Create a real VM, verify mount round-trips, then stop and delete it."""
    name = vm_name_for(project)

    # status before any VM exists
    r = sandbox("status", timeout=30)
    assert r.returncode == 0, r.stderr
    assert name not in r.stdout

    # `shell` with stdin closed: creates the VM, exec's bash -il, which
    # sees EOF and exits cleanly. The VM is left running.
    r = sandbox("-d", str(project), "-t", DEFAULT_TEMPLATE, "shell")
    assert r.returncode == 0, f"create failed:\nstdout={r.stdout}\nstderr={r.stderr}"

    # status now reports VM running
    r = sandbox("status", timeout=30)
    assert name in r.stdout
    assert "Running" in r.stdout

    # Bidirectional mount: VM reads host's marker, host reads VM's writeback.
    out = lima("shell", name, "--", "cat", str(project / "marker.txt"))
    assert "hello-from-host" in out.stdout

    lima("shell", name, "--", "sh", "-c", f"echo from-vm > '{project}/from-vm.txt'")
    assert (project / "from-vm.txt").read_text().strip() == "from-vm"

    # stop
    r = sandbox("stop", "-d", str(project), timeout=180)
    assert r.returncode == 0, r.stderr
    r = sandbox("status", timeout=30)
    assert "Stopped" in r.stdout

    # delete
    r = sandbox("delete", "-d", str(project), timeout=120)
    assert r.returncode == 0, r.stderr
    r = sandbox("status", timeout=30)
    assert name not in r.stdout


def test_prune_with_prefix(tmp_path: Path, cleanup_sandbox):
    """Create two VMs with distinct slugs; prune one prefix; the other survives."""
    keep = tmp_path / "keep-me"
    drop = tmp_path / "drop-me"
    keep.mkdir()
    drop.mkdir()

    # Create two VMs sequentially. Each shell invocation returns once the VM
    # is ready and bash has logged out (no TTY).
    for project_dir in (keep, drop):
        r = sandbox("-d", str(project_dir), "-t", DEFAULT_TEMPLATE, "shell")
        assert r.returncode == 0, f"create {project_dir} failed: {r.stderr}"

    keep_name = vm_name_for(keep)
    drop_name = vm_name_for(drop)

    r = sandbox("status", timeout=30)
    assert keep_name in r.stdout and drop_name in r.stdout

    # Prune only the drop-me prefix.
    r = sandbox("prune", "claude-sandbox-drop", timeout=300)
    assert r.returncode == 0, r.stderr

    r = sandbox("status", timeout=30)
    assert keep_name in r.stdout
    assert drop_name not in r.stdout
