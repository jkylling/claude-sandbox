# claude-sandbox

Run [Claude Code](https://code.claude.com/docs/en/overview) in isolated [Lima VMs](https://lima-vm.io/). Dangerously skip permissions safely.

## Requirements

- macOS with Apple Silicon or Intel
- [Lima](https://lima-vm.io/) (`brew install lima`)
- Python 3
- [jq](https://jqlang.github.io/jq/) (`brew install jq`)
- [tmux](https://github.com/tmux/tmux) (`brew install tmux`)

## Getting Started

### 1. Install dependencies

```bash
brew install lima jq tmux
```

### 2. Create the tools VM

The tools VM is a base image with dev tools and Claude Code pre-installed. Project VMs are cloned from it, so they start fast.

```bash
./rebuild-tools-vm
```

This creates a VM named `tools-vm` from [tools-vm.yaml](tools-vm.yaml). It takes a while on first run (downloads Ubuntu, installs toolchains). You might need to tweak the resources usage of the tools VM for your hardware.

### 3. Set the base VM

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
export CLAUDE_SANDBOX_BASE_VM=tools-vm
```

This tells `claude-sandbox` to clone project VMs from the tools VM.

### 4. Configure hooks (optional, recommended)

Hook events from Claude Code (e.g. "waiting for input") are forwarded from the VM to the host and displayed as macOS desktop notifications with sound.

The sandbox's Claude settings dir (`~/.claude-sandbox/.claude`) is mounted as `~/.claude` inside the VM. Configure hooks there:

```bash
mkdir -p ~/.claude-sandbox/.claude/hooks
cp hook.sh ~/.claude-sandbox/.claude/hooks/hook.sh
```

Create `~/.claude-sandbox/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "~/.claude/hooks/hook.sh" }] }
    ],
    "Stop": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "~/.claude/hooks/hook.sh" }] }
    ]
  }
}
```

Note: paths use `~/.claude/` (not `~/.claude-sandbox/.claude/`) because that's the mount point inside the VM.

### 5. Run Claude in a project

```bash
./claude-sandbox -d ~/projects/myapp
```

### 6. Day-to-day usage

```bash
claude-sandbox                        # Start Claude in the current directory
claude-sandbox -d ~/projects/myapp    # Start in a specific project
claude-sandbox shell                  # Open a shell in the VM
claude-sandbox -- --resume            # Pass arguments to Claude CLI
claude-sandbox stop                   # Stop the current project's VM
claude-sandbox stop-all               # Stop all VMs and hooks server
claude-sandbox delete                 # Delete the current project's VM
claude-sandbox prune                  # Delete all project VMs
claude-sandbox restart-hooks          # Restart the hooks server
claude-sandbox status                 # Show all VMs and hooks server status
```

## How It Works

1. **Tools VM**: A base Lima VM with dev tools and Claude Code pre-installed ([tools-vm.yaml](tools-vm.yaml))
2. **Project VMs**: Cloned from the tools VM per project directory, with the project mounted read-write
3. **Claude Execution**: Runs Claude Code inside the VM with `--dangerously-skip-permissions`
4. **Hooks**: A host-side server ([claude-hooks-server](claude-hooks-server)) receives events via a unix socket forwarded by Lima and sends desktop notifications

Claude settings are stored in `~/.claude-sandbox/.claude/` on the host, mounted into each VM as `~/.claude`. This persists settings across VM recreations and shares them between VMs. You could set `CLAUDE_SANDBOX_SETTINGS=~/.claude` to use your host settings, but this gives the AI a path to escape the VM.

## Commands

| Command | Description |
|---------|-------------|
| (none) | Start Claude in the VM |
| `shell` | Open a shell in the VM |
| `stop` | Stop the project's VM |
| `stop-all` | Stop all sandbox VMs |
| `restart-hooks` | Restart the hooks server |
| `status` | Show all sandbox VMs status |
| `delete` | Delete the project's VM. Including Claude sessions |
| `prune` | Delete all project VMs |

## Options

| Option | Description |
|--------|-------------|
| `-d, --dir DIR` | Project directory (default: current directory) |
| `-s, --settings DIR` | Claude settings directory (default: `~/.claude-sandbox/.claude`) |
| `-c, --clone-from VM` | Clone from an existing VM |
| `-t, --template YAML` | Create from a YAML template (default: `template:default`) |
| `--voice` | Enable voice mode (proxy host mic into VM) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_SANDBOX_BASE_VM` | Default VM to clone from |
| `CLAUDE_SANDBOX_TEMPLATE` | Default template to use |
| `CLAUDE_SANDBOX_SETTINGS` | Claude settings directory |
| `CLAUDE_SANDBOX_VOICE_MODE` | Set to `true` to enable voice mode (see below) |

## Files

- `claude-sandbox` - Main script
- `claude-sandbox.yaml` - Lima VM settings (for reference/documentation)
- `claude-hooks-server` - Hooks server that sends desktop notifications
- `hook.sh` - Hook script that runs inside the VM and forwards events to the hooks server
- `tools-vm.yaml` - Lima VM template with dev tools and Claude Code pre-installed
- `rebuild-tools-vm` - Script to recreate the tools VM from `tools-vm.yaml`

## Tips

### Voice mode

Voice mode proxies the host Mac's microphone into the VM so Claude's `/voice` command works. It's disabled by default and must be opted in:

```bash
claude-sandbox --voice                    # One-off
export CLAUDE_SANDBOX_VOICE_MODE=true     # Permanent (add to ~/.zshrc)
```

Requires [SoX](https://sox.sourceforge.net/) on the host (`brew install sox`).

### Avoiding host/guest build conflicts

Since the project directory is shared between host and guest, build environments can conflict — especially when host and guest run on different architectures (e.g. macOS arm64 host vs Linux x86_64 guest). Tools like `uv` will install platform-specific packages into `.venv`, which won't work on the other side.

Add environment variables to `~/.claude-sandbox/.claude/settings.json` (or the project's `.claude/settings.json`) to isolate the guest's build artifacts:

```json
{
  "env": {
    "UV_PROJECT_ENVIRONMENT": ".venv-agent"
  }
}
```

This makes `uv` use `.venv-agent` inside the VM instead of `.venv`. Consider similar isolation for other tools (e.g. separate build directories, cache paths).

### Preventing macOS sleep

If you're running long tasks, prevent your Mac from sleeping with `caffeinate`:

```bash
caffeinate -i -t 7200
```

This prevents the idle sleep (`-i`) from activating. Kill the background process when you're done.

### Tweaking tools VM resource usage

The tools VM resource allocation is configured in [tools-vm.yaml](tools-vm.yaml):

```yaml
cpus: 11
memory: "40GiB"
disk: "100GiB"
```

To adjust resources for your machine:

1. Edit `tools-vm.yaml` and modify the `cpus`, `memory`, or `disk` values
2. Rebuild the tools VM: `./rebuild-tools-vm`

**Note:** Project VMs cloned from the tools VM inherit these settings. If you adjust resources, existing project VMs won't change—only new ones will use the updated configuration.

### Claude settings

Claude settings are stored in `~/.claude-sandbox/.claude/` on the host and mounted into each VM as `~/.claude`. This allows settings to persist across VM recreations and be shared between VMs. You can add a `CLAUDE.md`, `settings.json`, etc. there and they'll be available in every sandbox.
