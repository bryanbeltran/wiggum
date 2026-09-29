from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RuntimePolicy
from .state import write_private_text


class CodexError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentResult:
    result: dict[str, Any]
    text: str
    return_code: int
    session_id: str | None
    trace_path: Path
    stderr_path: Path
    truncated: bool


def _read_capped(stream: Any, cap: int) -> tuple[bytes, bool]:
    stream.seek(0)
    data = stream.read(cap + 1)
    truncated = len(data) > cap
    if truncated:
        data = data[:cap]
    return data, truncated


def _session_id(events: bytes) -> str | None:
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def run_agent(
    *,
    executable: str,
    workspace: Path,
    role: str,
    prompt: str,
    schema_path: Path,
    evidence_dir: Path,
    policy: RuntimePolicy,
    writable: bool,
    images: tuple[Path, ...] = (),
    extra_readonly_dirs: tuple[Path, ...] = (),
    timeout_seconds: int | None = None,
) -> AgentResult:
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    last_message_path = evidence_dir / f"{role}.result.json"
    stdout_path = evidence_dir / f"{role}.jsonl"
    stderr_path = evidence_dir / f"{role}.stderr.txt"
    command = [
        executable,
        "exec",
        "--json",
        "--cd",
        str(workspace),
        "--model",
        policy.model,
        "--config",
        f'model_reasoning_effort="{policy.reasoning_effort}"',
        "--sandbox",
        "workspace-write" if writable else "read-only",
    ]
    if writable:
        command.append("--approve-for-me")
    for extra_dir in extra_readonly_dirs:
        if extra_dir.is_dir():
            command.extend(["--add-dir", str(extra_dir)])
    command.extend(["--output-schema", str(schema_path), "--output-last-message", str(last_message_path)])
    for image_path in images:
        if image_path.is_file():
            command.extend(["--image", str(image_path)])
    command.append("-")

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    timeout = timeout_seconds or policy.agent_timeout_seconds
    with tempfile.TemporaryFile(mode="w+b") as stdout_tmp, tempfile.TemporaryFile(mode="w+b") as stderr_tmp:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdin=subprocess.PIPE,
                stdout=stdout_tmp,
                stderr=stderr_tmp,
                env=env,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise CodexError(f"Codex {role} timed out after {timeout} seconds")
                time.sleep(0.2)
            return_code = process.returncode or 0
        except FileNotFoundError as exc:
            raise CodexError(f"Could not start Codex CLI {executable!r}; install it and authenticate first") from exc

        stdout, stdout_truncated = _read_capped(stdout_tmp, policy.max_agent_output_bytes)
        stderr, stderr_truncated = _read_capped(stderr_tmp, policy.max_agent_output_bytes)
    write_private_text(stdout_path, stdout.decode("utf-8", errors="replace"))
    write_private_text(stderr_path, stderr.decode("utf-8", errors="replace"))
    if not last_message_path.exists():
        if return_code:
            raise CodexError(
                f"Codex {role} exited with status {return_code}; see {stderr_path}"
            )
        raise CodexError(f"Codex {role} did not write its structured result")

    raw_text = last_message_path.read_text(encoding="utf-8", errors="replace")
    write_private_text(last_message_path, raw_text)
    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CodexError(f"Codex {role} result was not valid JSON; see {last_message_path}") from exc
    if not isinstance(result, dict):
        raise CodexError(f"Codex {role} result must be a JSON object")
    if return_code:
        raise CodexError(f"Codex {role} exited with status {return_code}; see {stderr_path}")
    return AgentResult(
        result=result,
        text=raw_text,
        return_code=return_code,
        session_id=_session_id(stdout),
        trace_path=stdout_path,
        stderr_path=stderr_path,
        truncated=stdout_truncated or stderr_truncated,
    )
