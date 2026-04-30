socket=${CLAUDE_HOOKS_SOCKET:-$HOME/.claude-sandbox/claude-hooks.sock}
input=$(cat)
data=$(echo "$input" | jq -c --arg pid "$PPID" --arg uuid "$CLAUDE_SANDBOX_UUID" --arg time "$(date -Ins)" --arg project "$CLAUDE_PROJECT_DIR" '{
    time: $time,
    pid: $pid,
    uuid: (if $uuid != "" then $uuid else null end),
    project: (if $project != "" then $project else null end),
    hook_payload: .,
  } | del(..|select(. == null))')
curl --max-time 2 --unix-socket "$socket" http://localhost -X POST --json "$data"
exit 0
