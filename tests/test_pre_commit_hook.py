"""Tests for the local PreToolUse hook script. The real `make check` is never run here (it would recurse)."""

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre_commit_check.sh"
BLOCKING_EXIT_CODE = 2


def payload(command: str) -> str:
    """Shape of the PreToolUse JSON that Claude Code pipes to a hook for the Bash tool."""
    return json.dumps(
        {
            "session_id": "abc123",
            "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
            "cwd": "/home/user/my-project",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "test", "timeout": 120000},
            "tool_use_id": "toolu_01ABC123",
        }
    )


def run_hook(stdin: str, check_cmd: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "QUASAR_CHECK_CMD": check_cmd}
    return subprocess.run([str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env, check=False)


def test_non_commit_command_exits_zero_without_running_check(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    result = run_hook(payload("ls -la"), f"touch {marker}; false")
    assert result.returncode == 0
    assert not marker.exists()


def test_commit_with_passing_check_exits_zero() -> None:
    result = run_hook(payload('git commit -m "feat: x"'), "true")
    assert result.returncode == 0


def test_commit_with_failing_check_blocks_and_reports_output_on_stderr() -> None:
    result = run_hook(payload('git commit -m "feat: x"'), "echo boom >&2; false")
    assert result.returncode == BLOCKING_EXIT_CODE
    assert "boom" in result.stderr


def test_failing_check_reports_stdout_output_too() -> None:
    result = run_hook(payload("git commit -am wip"), "echo lint-failed; false")
    assert result.returncode == BLOCKING_EXIT_CODE
    assert "lint-failed" in result.stderr


def test_failing_check_output_is_truncated_to_tail() -> None:
    result = run_hook(payload("git commit -m x"), "seq 1 500; false")
    assert result.returncode == BLOCKING_EXIT_CODE
    assert "500" in result.stderr
    assert "\n1\n" not in result.stderr


def test_check_runs_from_project_root(tmp_path: Path) -> None:
    env = {**os.environ, "QUASAR_CHECK_CMD": "pwd >&2; false"}
    result = subprocess.run(
        [str(SCRIPT)],
        input=payload("git commit -m x"),
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == BLOCKING_EXIT_CODE
    assert str(SCRIPT.parents[2]) in result.stderr
