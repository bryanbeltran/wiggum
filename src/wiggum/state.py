from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .security import redact_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return normalized[:100] or "item"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    return value


def write_private_text(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(redact_text(text))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_private_json(path: Path, data: Any) -> None:
    write_private_text(path, json.dumps(_redacted(data), indent=2, sort_keys=True) + "\n")


def append_private_jsonl(path: Path, data: Any) -> None:
    ensure_private_dir(path.parent)
    with path.open("a", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(_redacted(data), sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextmanager
def project_lock(state_dir: Path) -> Iterator[None]:
    ensure_private_dir(state_dir)
    lock_path = state_dir / "run.lock"
    with lock_path.open("a+") as stream:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"A Wiggum run already holds the lock at {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ProjectState:
    def __init__(self, root: Path):
        self.root = ensure_private_dir(root)

    @property
    def backlog_path(self) -> Path:
        return self.root / "backlog.json"

    @property
    def decisions_path(self) -> Path:
        return self.root / "decisions.json"

    @property
    def iterations_path(self) -> Path:
        return self.root / "iterations.jsonl"

    @property
    def control_path(self) -> Path:
        return self.root / "control" / "request.json"

    def start_run(self, run_id: str, data: dict[str, Any]) -> Path:
        path = ensure_private_dir(self.root / "runs" / safe_id(run_id))
        write_private_json(path / "run.json", data)
        return path

    def start_cycle(self, run_dir: Path, cycle_id: str) -> Path:
        return ensure_private_dir(run_dir / safe_id(cycle_id))

    def add_decision(self, decision: dict[str, Any]) -> None:
        items = read_json(self.decisions_path, [])
        if not isinstance(items, list):
            items = []
        items.append({**decision, "recorded_at": utc_now()})
        write_private_json(self.decisions_path, items)

    def update_candidate(self, candidate_id: str, **changes: Any) -> dict[str, Any]:
        backlog = read_json(self.backlog_path, {})
        if not isinstance(backlog, dict):
            backlog = {}
        old = backlog.get(candidate_id, {})
        candidate = {**old, **changes, "id": candidate_id, "updated_at": utc_now()}
        candidate.setdefault("created_at", utc_now())
        backlog[candidate_id] = candidate
        write_private_json(self.backlog_path, backlog)
        return candidate

    def append_iteration(self, record: dict[str, Any]) -> None:
        append_private_jsonl(self.iterations_path, {**record, "recorded_at": utc_now()})

    def read_control(self) -> dict[str, str] | None:
        value = read_json(self.control_path, None)
        return value if isinstance(value, dict) else None

    def request_control(
        self, action: str, mode: str | None = None, objective: str | None = None
    ) -> None:
        payload = {"action": action, "requested_at": utc_now()}
        if mode:
            payload["mode"] = mode
        if objective:
            payload["objective"] = objective
        write_private_json(self.control_path, payload)

    def clear_control(self) -> None:
        try:
            self.control_path.unlink()
        except FileNotFoundError:
            pass

    def iteration_records(self) -> list[dict[str, Any]]:
        try:
            lines = self.iterations_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
