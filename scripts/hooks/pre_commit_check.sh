#!/bin/sh
# Claude Code PreToolUse hook (matcher: Bash). Local convenience only; CI is the authoritative gate.
# Reads the hook JSON from stdin. If it mentions "git commit", runs the check command and blocks
# the tool call (exit 2, stderr is shown to Claude) when it fails. Otherwise exits 0 immediately.
# QUASAR_CHECK_CMD overrides the check command (used by tests).

input=$(cat)

case "$input" in
*"git commit"*) ;;
*) exit 0 ;;
esac

root=$(cd "$(dirname "$0")/../.." && pwd) || exit 0
cd "$root" || exit 0

output=$(sh -c "${QUASAR_CHECK_CMD:-make check}" 2>&1)
status=$?
if [ "$status" -ne 0 ]; then
    echo "Pre-commit check failed (exit $status). Last 40 lines of output:" >&2
    printf '%s\n' "$output" | tail -n 40 >&2
    exit 2
fi
exit 0
