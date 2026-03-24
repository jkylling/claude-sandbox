# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

claude-sandbox runs Claude Code inside isolated Lima VMs on macOS. It creates per-project VMs (cloned from a pre-built tools VM or from a Lima template), mounts the project directory read-write, and runs Claude with `--dangerously-skip-permissions` safely inside the VM.

## Architecture

- **`claude-sandbox`** (bash) — Main entry point. Manages VM lifecycle (create/clone/start/stop/delete), argument parsing, and launches Claude inside a tmux session via `limactl shell`.
- **`tools-vm.yaml`** (Lima config) — Defines the base "tools VM" with Ubuntu 24.04, dev toolchains (Go, Rust, Node/fnm, Python/uv, JVM/SDKMAN, Docker, clang), and Claude Code pre-installed. Project VMs are cloned from this.
- **`rebuild-tools-vm`** (bash) — Tears down and recreates the tools VM from `tools-vm.yaml`, then verifies all tools are installed.
- **`claude-hooks-server`** (Python 3) — Unix socket HTTP server on the host that receives hook events forwarded from VMs and sends macOS desktop notifications via `osascript`.
- **`hook.sh`** — Runs inside the VM on Claude hook events, wraps the payload with metadata, and `curl`s it to the host via the forwarded unix socket.
- **`claude-sandbox.yaml`** — Reference/documentation-only Lima config showing the Claude-specific settings that `claude-sandbox` applies via `--set` flags.

## Key Design Decisions

- VM creation settings (mounts, provision scripts, port forwards) are applied via `limactl --set` flags in `claude_set_args()` rather than static YAML, because Lima's `--set ".base ..."` is broken.
- Claude settings live in `~/.claude-sandbox/.claude/` on the host, mounted as `~/.claude-shared` in the VM. The provision script symlinks specific files (credentials, settings, hooks, CLAUDE.md) into `~/.claude/` — runtime state like `.claude.json` stays per-VM to avoid virtiofs flock issues.
- VM names are derived from the project directory name: `claude-sandbox-<sanitized-dirname>`.
- The hooks server auto-starts when launching Claude and uses a PID file at `~/.claude-sandbox/claude-hooks-server.pid`.

## Common Commands

```bash
./rebuild-tools-vm              # Recreate the tools VM (slow, downloads Ubuntu + installs toolchains)
./claude-sandbox -d ~/project   # Launch Claude in a project
./claude-sandbox shell          # Shell into the VM for debugging
./claude-sandbox status         # Show all VMs and hooks server state
./claude-sandbox stop-all       # Stop everything
./claude-sandbox prune          # Delete all project VMs
```

## Dependencies

macOS only. Requires: `lima`, `jq`, `tmux`, `python3`.
