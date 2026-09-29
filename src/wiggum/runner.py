from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any

from .codex import AgentResult, CodexError, run_agent
from .config import ApplicationConfig, MODES, ProjectConfig
from .git_ops import (
    GitError,
    SafetyError,
    add_detached_worktree,
    assert_safe_project,
    commit_staged,
    count_commits_ahead,
    git,
    head_sha,
    is_clean,
    merge_reviewed_commit,
    remove_worktree,
    stage_changes,
)
from .state import (
    ProjectState,
    project_lock,
    read_json,
    safe_id,
    utc_now,
    write_private_json,
    write_private_text,
)
from .self_improvement import run_self_analysis, self_analysis_eligibility


class CycleError(RuntimeError):
    pass


def _repo_prompts() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def _repo_schemas() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas"


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _candidate_id(candidate: dict[str, Any]) -> str:
    key = f"{candidate.get('title', '').strip().lower()}\n{candidate.get('problem', '').strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _clip(value: str, limit: int = 200_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[output truncated by Wiggum]\n"


def _tree_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _read_constitution(project: ProjectConfig, workspace: Path) -> tuple[str, str | None]:
    if not project.constitution:
        return "No project constitution is configured.", None
    path = workspace / project.constitution
    if not path.is_file():
        return f"Configured constitution {project.constitution!r} was not found.", None
    content = path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _clip(content, 60_000), digest


def _candidate_summary(state: ProjectState, limit: int = 30) -> list[dict[str, Any]]:
    backlog = read_json(state.backlog_path, {})
    if not isinstance(backlog, dict):
        return []
    items = list(backlog.values())
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return items[:limit]


def _weighted_score(candidate: dict[str, Any], weights: dict[str, float]) -> float | None:
    scores = candidate.get("scores")
    active = [(key, weight) for key, weight in weights.items() if weight > 0]
    total_weight = sum(weight for _, weight in active)
    if not isinstance(scores, dict) or not active or total_weight <= 0:
        return None
    if any(key not in scores for key, _ in active):
        return None
    return round(sum(float(scores[key]) * weight for key, weight in active) / total_weight, 1)


def _format_candidate_scorecard(
    candidates: list[dict[str, Any]], weights: dict[str, float]
) -> str:
    lines = ["Wiggum feature council scorecard", ""]
    for index, candidate in enumerate(candidates, start=1):
        score = _weighted_score(candidate, weights)
        candidate["weighted_score"] = score
        lines.extend(
            [
                f"{index}. {candidate.get('title', 'Untitled')} — weighted score {score if score is not None else 'n/a'}/100",
                f"   Score components: {json.dumps(candidate.get('scores', {}), sort_keys=True)}",
                f"   Value: {candidate.get('value', '')}",
                f"   Evidence: {candidate.get('evidence', '')}",
                f"   Tradeoffs: {candidate.get('tradeoffs', '')}",
                f"   Risk: {candidate.get('risk', 'unknown')}",
                "",
            ]
        )
    preferred = next((item for item in candidates if item.get("preferred")), None)
    if preferred:
        lines.append(f"Wiggum's preferred candidate: {preferred.get('title', 'Untitled')}")
    return "\n".join(lines)


class WiggumRunner:
    def __init__(self, config: ApplicationConfig, codex_executable: str = "codex"):
        self.config = config
        self.policy = config.policy
        self.codex_executable = codex_executable

    def run(
        self,
        project: ProjectConfig,
        mode: str,
        *,
        objective: str | None = None,
        cycles: int | None = None,
        forever: bool = False,
        selection: str | None = None,
        adventurousness: str | None = None,
    ) -> dict[str, Any]:
        if mode not in MODES:
            raise CycleError(f"Unknown mode {mode!r}")
        if mode in {"bugfix", "directed"} and not (objective and objective.strip()):
            raise CycleError(f"{mode} mode requires an objective")
        if cycles is not None and cycles < 1:
            raise CycleError("--cycles must be a positive integer")
        if cycles is not None and forever:
            raise CycleError("Choose either --cycles or --forever")
        max_cycles = None if forever else (cycles or self.policy.default_cycles)
        chosen_selection = selection or self.policy.default_selection
        chosen_adventurousness = adventurousness or self.policy.default_adventurousness
        if chosen_selection not in {"auto", "council"}:
            raise CycleError("Feature selection must be auto or council")

        state = ProjectState(project.state_dir)
        with project_lock(project.state_dir):
            snapshot = assert_safe_project(
                project.repo_path, project.integration_branch, project.base_branch
            )
            state.clear_control()
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + safe_id(project.alias)
            run_dir = state.start_run(
                run_id,
                {
                    "run_id": run_id,
                    "project": project.alias,
                    "mode": mode,
                    "objective": objective,
                    "started_at": utc_now(),
                    "base_sha": snapshot.sha,
                    "branch": snapshot.branch,
                    "model": self.policy.model,
                    "reasoning_effort": self.policy.reasoning_effort,
                    "selection": chosen_selection if mode == "feature" else None,
                    "adventurousness": chosen_adventurousness if mode == "feature" else None,
                },
            )
            metrics_path = state.root / "metrics.json"
            metrics = read_json(metrics_path, {"consecutive_low_value_cycles": 0})
            results: list[dict[str, Any]] = []
            self_analysis_results: list[dict[str, Any]] = []
            active_mode = mode
            active_objective = objective
            completed = 0
            stop_reason = "cycle limit reached" if max_cycles else "operator stop requested"

            while max_cycles is None or completed < max_cycles:
                evidence_bytes = _tree_bytes(state.root)
                if evidence_bytes >= self.policy.max_evidence_bytes:
                    stop_reason = (
                        f"evidence cap reached ({evidence_bytes} bytes); archive evidence before continuing"
                    )
                    break
                request = state.read_control()
                if request and request.get("action") == "stop":
                    state.clear_control()
                    stop_reason = "graceful stop request honored at cycle boundary"
                    break
                if request and request.get("action") == "pivot" and request.get("mode") in MODES:
                    active_mode = request["mode"]
                    if request.get("objective"):
                        active_objective = request["objective"]
                    if active_mode in {"bugfix", "directed"} and not (
                        active_objective and active_objective.strip()
                    ):
                        state.clear_control()
                        stop_reason = f"pivot to {active_mode} requires an objective"
                        break
                    state.clear_control()
                    write_private_json(
                        run_dir / "run.json",
                        {
                            **read_json(run_dir / "run.json", {}),
                            "active_mode": active_mode,
                            "active_objective": active_objective,
                            "last_pivot_at": utc_now(),
                        },
                    )

                ahead = count_commits_ahead(
                    project.repo_path, project.base_branch, project.integration_branch
                )
                if ahead >= self.policy.batch_review_after_commits:
                    stop_reason = f"batch review gate reached at {ahead} commits"
                    break

                completed += 1
                cycle_id = f"{run_id}-{completed:03d}"
                result = self._run_cycle(
                    project,
                    state,
                    run_dir,
                    cycle_id,
                    active_mode,
                    active_objective,
                    selection=chosen_selection,
                    adventurousness=chosen_adventurousness,
                )
                results.append(result)
                state.append_iteration(result)

                try:
                    analysis_result = run_self_analysis(self.config, self.codex_executable)
                    if analysis_result.get("eligible"):
                        self_analysis_results.append(analysis_result)
                except Exception as exc:
                    self_analysis_results.append(
                        {"eligible": False, "error": str(exc), "recorded_at": utc_now()}
                    )

                if result["status"] == "implemented":
                    metrics["consecutive_low_value_cycles"] = 0
                elif result["status"] in {
                    "no_work",
                    "no_change",
                    "deferred",
                    "baseline_failed",
                    "review_rejected",
                    "validation_failed",
                    "error",
                }:
                    metrics["consecutive_low_value_cycles"] = int(
                        metrics.get("consecutive_low_value_cycles", 0)
                    ) + 1
                write_private_json(metrics_path, metrics)

                if int(metrics.get("consecutive_low_value_cycles", 0)) >= self.policy.low_value_stop_after:
                    stop_reason = (
                        "stopped after "
                        f"{self.policy.low_value_stop_after} consecutive low-value cycles"
                    )
                    break
                ahead = count_commits_ahead(
                    project.repo_path, project.base_branch, project.integration_branch
                )
                if ahead >= self.policy.batch_review_after_commits:
                    stop_reason = f"batch review gate reached at {ahead} commits"
                    break
                if result.get("status") == "council":
                    stop_reason = "feature council proposals recorded"
                    break

            summary = {
                "run_id": run_id,
                "project": project.alias,
                "requested_mode": mode,
                "last_active_mode": active_mode,
                "last_active_objective": active_objective,
                "cycles_completed": len(results),
                "stop_reason": stop_reason,
                "results": results,
                "self_analysis": self_analysis_results,
                "final_sha": head_sha(project.repo_path),
                "completed_at": utc_now(),
            }
            write_private_json(run_dir / "run.json", summary)
            return summary

    def _run_cycle(
        self,
        project: ProjectConfig,
        state: ProjectState,
        run_dir: Path,
        cycle_id: str,
        mode: str,
        objective: str | None,
        *,
        selection: str,
        adventurousness: str,
    ) -> dict[str, Any]:
        snapshot = assert_safe_project(
            project.repo_path, project.integration_branch, project.base_branch
        )
        cycle_dir = state.start_cycle(run_dir, cycle_id)
        container = Path(tempfile.mkdtemp(prefix=f"wiggum-{safe_id(cycle_id)}-"))
        worktree = container / "repo"
        record: dict[str, Any] = {
            "cycle_id": cycle_id,
            "project": project.alias,
            "mode": mode,
            "objective": objective,
            "base_sha": snapshot.sha,
            "branch": snapshot.branch,
            "model": self.policy.model,
            "reasoning_effort": self.policy.reasoning_effort,
            "evidence_dir": str(cycle_dir),
            "started_at": utc_now(),
            "status": "error",
            "files_touched": [],
            "tests_added": [],
            "tests": [],
            "sessions": {},
            "known_limitations": [],
            "commit": None,
            "checkpoint_ref": None,
        }
        deadline = time.monotonic() + self.policy.max_cycle_seconds
        evidence_dir = cycle_dir
        try:
            add_detached_worktree(project.repo_path, worktree, snapshot.sha)
            constitution, constitution_hash = _read_constitution(project, worktree)
            record["constitution_sha256"] = constitution_hash

            baseline = self._run_checks(project, worktree, evidence_dir, "baseline", deadline)
            record["baseline_checks"] = baseline
            if self.policy.baseline_checks and baseline["status"] == "failed":
                record["status"] = "baseline_failed"
                record["reason"] = "Configured baseline checks failed before implementation"
                return self._finish_cycle(state, record)
            if not is_clean(worktree):
                record["status"] = "baseline_failed"
                record["reason"] = "Configured baseline checks changed the target worktree"
                try:
                    _, baseline_diff = stage_changes(worktree)
                    if baseline_diff:
                        write_private_text(evidence_dir / "baseline-side-effects.patch", baseline_diff)
                except (GitError, OSError) as exc:
                    record["baseline_side_effect_error"] = str(exc)
                return self._finish_cycle(state, record)

            before_screenshot, before_capture = self._capture_screenshot(
                project, worktree, evidence_dir, cycle_id, "before", deadline
            )
            record["browser_before"] = before_capture

            backlog = _candidate_summary(state)
            investigation_prompt = self._investigation_prompt(
                project,
                mode,
                objective,
                constitution,
                backlog,
                selection,
                adventurousness,
            )
            investigation = self._ask(
                workspace=worktree,
                evidence_dir=evidence_dir,
                role="investigator",
                prompt=investigation_prompt,
                schema="investigation.json",
                writable=False,
                deadline=deadline,
            )
            record["sessions"]["investigator"] = investigation.session_id
            write_private_json(evidence_dir / "investigation.json", investigation.result)
            record["investigation"] = investigation.result

            outcome = investigation.result.get("outcome")
            candidate = investigation.result.get("candidate")
            risk = investigation.result.get("risk", "green")
            if mode == "feature" and selection == "council":
                candidates = investigation.result.get("candidates", [])
                for item in candidates:
                    if isinstance(item, dict) and item.get("title"):
                        item["weighted_score"] = _weighted_score(item, self.policy.feature_weights)
                        candidate_id = _candidate_id(item)
                        state.update_candidate(
                            candidate_id,
                            **item,
                            status="discovered",
                            mode=mode,
                            decision_reason="Presented in feature council; no candidate was selected for implementation.",
                        )
                record["status"] = "council"
                record["candidates"] = candidates
                write_private_text(
                    evidence_dir / "council.md",
                    _format_candidate_scorecard(candidates, self.policy.feature_weights),
                )
                state.add_decision(
                    {
                        "cycle_id": cycle_id,
                        "mode": mode,
                        "decision": "council",
                        "reason": investigation.result.get("reason", "Scorecard recorded without implementation."),
                        "candidates": candidates,
                    }
                )
                return self._finish_cycle(state, record)

            if outcome == "no_work":
                record["status"] = "no_work"
                record["reason"] = investigation.result.get("reason", "No candidate cleared the value threshold.")
                state.add_decision(
                    {"cycle_id": cycle_id, "mode": mode, "decision": "no_work", "reason": record["reason"]}
                )
                return self._finish_cycle(state, record)

            if outcome in {"defer", "escalate"} or risk == "red":
                record["status"] = "deferred"
                record["reason"] = investigation.result.get("reason", "Human decision required by risk policy.")
                if isinstance(candidate, dict) and candidate.get("title"):
                    candidate_id = _candidate_id(candidate)
                    state.update_candidate(
                        candidate_id,
                        **candidate,
                        status="deferred",
                        mode=mode,
                        risk=risk,
                        decision_reason=record["reason"],
                        cycle_id=cycle_id,
                    )
                    record["candidate_id"] = candidate_id
                state.add_decision(
                    {
                        "cycle_id": cycle_id,
                        "mode": mode,
                        "decision": "defer",
                        "risk": risk,
                        "reason": record["reason"],
                    }
                )
                return self._finish_cycle(state, record)

            if outcome != "implement" or not isinstance(candidate, dict):
                raise CycleError("Investigator returned an inconsistent outcome/candidate pair")

            if risk == "yellow":
                confidence = float(candidate.get("confidence", 0))
                criteria = candidate.get("acceptance_criteria", [])
                if confidence < self.policy.yellow_risk_min_confidence or not criteria:
                    record["status"] = "deferred"
                    record["reason"] = (
                        "Yellow-risk work needs confidence of at least "
                        f"{self.policy.yellow_risk_min_confidence:.2f} and a testable acceptance criterion"
                    )
                    candidate_id = _candidate_id(candidate)
                    state.update_candidate(
                        candidate_id,
                        **candidate,
                        status="deferred",
                        mode=mode,
                        risk=risk,
                        cycle_id=cycle_id,
                        decision_reason=record["reason"],
                    )
                    record["candidate_id"] = candidate_id
                    state.add_decision(
                        {
                            "cycle_id": cycle_id,
                            "mode": mode,
                            "candidate_id": candidate_id,
                            "decision": "defer",
                            "risk": risk,
                            "reason": record["reason"],
                        }
                    )
                    return self._finish_cycle(state, record)

            candidate_id = _candidate_id(candidate)
            record["candidate_id"] = candidate_id
            tracked = read_json(state.backlog_path, {}).get(candidate_id)
            if isinstance(tracked, dict) and tracked.get("status") in {
                "implemented",
                "rejected",
                "superseded",
            }:
                record["status"] = "no_work"
                record["reason"] = f"Duplicate candidate already marked {tracked['status']}"
                return self._finish_cycle(state, record)
            state.update_candidate(
                candidate_id,
                **candidate,
                status="accepted",
                mode=mode,
                risk=risk,
                cycle_id=cycle_id,
                decision_reason=investigation.result.get("reason", "Candidate cleared the configured value threshold."),
            )

            impl_result: dict[str, Any] | None = None
            review_result: dict[str, Any] | None = None
            staged_diff = ""
            final_tests: dict[str, Any] = {"status": "not_run", "commands": []}
            for attempt in range(self.policy.review_fix_rounds + 1):
                impl_prompt = self._implementation_prompt(
                    mode,
                    candidate,
                    objective,
                    implementation_feedback=(
                        review_result.get("findings", []) if review_result else []
                    ),
                    validation_feedback=final_tests if final_tests.get("status") == "failed" else None,
                )
                implementation = self._ask(
                    workspace=worktree,
                    evidence_dir=evidence_dir,
                    role=f"implementer-{attempt + 1}",
                    prompt=impl_prompt,
                    schema="implementation.json",
                    writable=True,
                    deadline=deadline,
                )
                record["sessions"][f"implementer-{attempt + 1}"] = implementation.session_id
                impl_result = implementation.result
                write_private_json(evidence_dir / f"implementation-{attempt + 1}.json", impl_result)
                if impl_result.get("result") in {"blocked", "escalate"} or impl_result.get("risk") == "red":
                    record["status"] = "deferred"
                    record["reason"] = impl_result.get("summary", "Implementation hit a human-approval boundary")
                    state.update_candidate(
                        candidate_id,
                        status="deferred",
                        decision_reason=record["reason"],
                    )
                    return self._finish_cycle(state, record)
                if impl_result.get("result") == "no_change":
                    if not git(worktree, "status", "--porcelain", "--untracked-files=all").stdout:
                        record["status"] = "no_change"
                        record["reason"] = impl_result.get("summary", "Implementer found no safe change.")
                        state.update_candidate(
                            candidate_id,
                            status="deferred",
                            decision_reason=record["reason"],
                        )
                        return self._finish_cycle(state, record)

                changed, staged_diff = stage_changes(worktree)
                if not changed:
                    record["status"] = "no_change"
                    record["reason"] = impl_result.get("summary", "No file changes were produced.")
                    state.update_candidate(
                        candidate_id,
                        status="deferred",
                        decision_reason=record["reason"],
                    )
                    return self._finish_cycle(state, record)
                write_private_text(evidence_dir / "change.patch", staged_diff)
                record["files_touched"] = git(
                    worktree, "diff", "--cached", "--name-only"
                ).stdout.splitlines()
                record["tests_added"] = impl_result.get("tests_added", [])
                record["known_limitations"] = impl_result.get("limitations", [])

                final_tests = self._run_checks(
                    project, worktree, evidence_dir, f"validation-{attempt + 1}", deadline
                )
                record["tests"].append(final_tests)
                if final_tests["status"] == "failed":
                    if attempt < self.policy.review_fix_rounds:
                        git(worktree, "reset", check=False)
                        review_result = {
                            "findings": [
                                "Configured project checks failed. Fix the reported failures before review."
                            ]
                        }
                        continue
                    record["status"] = "validation_failed"
                    record["reason"] = "Configured checks failed after the final fix attempt"
                    state.update_candidate(
                        candidate_id,
                        status="deferred",
                        decision_reason=record["reason"],
                    )
                    return self._finish_cycle(state, record)

                after_screenshot, after_capture = self._capture_screenshot(
                    project, worktree, evidence_dir, cycle_id, "after", deadline
                )
                record["browser_after"] = after_capture
                review_prompt = self._review_prompt(
                    mode,
                    candidate,
                    staged_diff,
                    final_tests,
                    before_capture,
                    after_capture,
                )
                images = tuple(
                    path for path in (before_screenshot, after_screenshot) if path is not None
                )
                review = self._ask(
                    workspace=worktree,
                    evidence_dir=evidence_dir,
                    role=f"reviewer-{attempt + 1}",
                    prompt=review_prompt,
                    schema="review.json",
                    writable=False,
                    images=images,
                    deadline=deadline,
                )
                record["sessions"][f"reviewer-{attempt + 1}"] = review.session_id
                review_result = review.result
                write_private_json(evidence_dir / f"review-{attempt + 1}.json", review_result)
                if review_result.get("approved") and not review_result.get("findings") and not review_result.get("missing_checks"):
                    break
                if attempt >= self.policy.review_fix_rounds:
                    record["status"] = "review_rejected"
                    record["reason"] = review_result.get("summary", "Adversarial review did not approve the change")
                    state.update_candidate(
                        candidate_id,
                        status="deferred",
                        decision_reason=record["reason"],
                    )
                    return self._finish_cycle(state, record)
                git(worktree, "reset", check=False)

            if not review_result or not review_result.get("approved"):
                raise CycleError("Implementation left the review loop without approval")
            subject = str((impl_result or {}).get("commit_subject", ""))
            commit_sha = commit_staged(worktree, subject)
            record["commit"] = {"sha": commit_sha, "subject": subject}
            checkpoint_ref = merge_reviewed_commit(
                project.repo_path,
                project.integration_branch,
                snapshot.sha,
                commit_sha,
                cycle_id,
            )
            record["checkpoint_ref"] = checkpoint_ref
            record["status"] = "implemented"
            record["summary"] = (impl_result or {}).get("summary", "Reviewed change integrated.")
            state.update_candidate(
                candidate_id,
                status="implemented",
                decision_reason=record["summary"],
                commit=commit_sha,
                cycle_id=cycle_id,
            )
            state.add_decision(
                {
                    "cycle_id": cycle_id,
                    "mode": mode,
                    "candidate_id": candidate_id,
                    "decision": "implemented",
                    "reason": record["summary"],
                    "risk": risk,
                }
            )
            return self._finish_cycle(state, record)
        except (CycleError, CodexError, GitError, OSError, ValueError, json.JSONDecodeError) as exc:
            record["status"] = "error"
            record["reason"] = str(exc)
            try:
                if worktree.is_dir():
                    _, diff = stage_changes(worktree)
                    if diff:
                        write_private_text(evidence_dir / "failed-change.patch", diff)
            except Exception:
                pass
            return self._finish_cycle(state, record)
        finally:
            if worktree.exists():
                if record.get("status") != "implemented":
                    try:
                        _, remaining_diff = stage_changes(worktree)
                        if remaining_diff:
                            write_private_text(evidence_dir / "unaccepted-change.patch", remaining_diff)
                    except Exception:
                        pass
                remove_worktree(project.repo_path, worktree)
            shutil.rmtree(container, ignore_errors=True)

    def _finish_cycle(self, state: ProjectState, record: dict[str, Any]) -> dict[str, Any]:
        record["completed_at"] = utc_now()
        write_private_json(Path(record["evidence_dir"]) / "cycle.json", record)
        return record

    def _ask(
        self,
        *,
        workspace: Path,
        evidence_dir: Path,
        role: str,
        prompt: str,
        schema: str,
        writable: bool,
        images: tuple[Path, ...] = (),
        deadline: float | None = None,
    ) -> AgentResult:
        write_private_text(evidence_dir / f"{role}.prompt.md", prompt)
        remaining = self.policy.agent_timeout_seconds
        if deadline is not None:
            remaining = min(remaining, int(deadline - time.monotonic()))
            if remaining < 1:
                raise CycleError("The cycle time budget is exhausted")
        result = run_agent(
            executable=self.codex_executable,
            workspace=workspace,
            role=role,
            prompt=prompt,
            schema_path=_repo_schemas() / schema,
            evidence_dir=evidence_dir,
            policy=replace(self.policy, agent_timeout_seconds=remaining),
            writable=writable,
            images=images,
            timeout_seconds=remaining,
        )
        if result.truncated:
            raise CycleError(f"Codex {role} output exceeded the configured evidence limit")
        return result

    def _investigation_prompt(
        self,
        project: ProjectConfig,
        mode: str,
        objective: str | None,
        constitution: str,
        backlog: list[dict[str, Any]],
        selection: str,
        adventurousness: str,
    ) -> str:
        mode_prompt = _load_prompt(_repo_prompts() / "modes" / f"{mode}.md")
        selection_instructions = ""
        if mode == "feature":
            selection_instructions = (
                f"\nSelection workflow: {selection}. Adventurousness: {adventurousness}. "
                f"Feature ranking weights: {json.dumps(self.policy.feature_weights, sort_keys=True)}. "
                "For score components, use 0 for poor and 100 for strong; cost and risk scores mean lower cost and lower risk. "
                "Wiggum computes the weighted score from those components. "
                f"For council, return about {self.policy.candidate_count} candidates and do not implement. "
                "For auto, choose one candidate only if it clears the risk and value bar."
            )
        return "\n\n".join(
            [
                "ROLE: INVESTIGATOR. This phase is read-only; do not modify files or commit.",
                f"Active mode: {mode}.",
                f"Human objective: {objective or 'None; discover work within the selected mode.'}",
                f"{mode_prompt}{selection_instructions}",
                "Product constitution:\n" + constitution,
                "Existing Wiggum candidates and dispositions:\n" + json.dumps(backlog, indent=2),
                f"Risk boundaries: Green changes may proceed. Yellow work may proceed only when isolated, confidence is at least {self.policy.yellow_risk_min_confidence:.2f}, and a testable acceptance criterion is present. "
                "Red work must be deferred: destructive migrations/data changes, auth changes, secrets, payments, paid services, "
                "privacy/legal consequences, breaking APIs, or consequential product decisions. Size alone is not a red gate.",
                "Return the required structured investigation result. For an implement outcome provide evidence and testable acceptance criteria. "
                "A no-work outcome is successful when you explain what you inspected and why no candidate cleared the configured value bar.",
            ]
        )

    def _implementation_prompt(
        self,
        mode: str,
        candidate: dict[str, Any],
        objective: str | None,
        *,
        implementation_feedback: Any,
        validation_feedback: Any,
    ) -> str:
        mode_prompt = _load_prompt(_repo_prompts() / "modes" / f"{mode}.md")
        return "\n\n".join(
            [
                "ROLE: IMPLEMENTER. You may edit files in this isolated worktree. Do not commit, push, or alter Git history.",
                f"Mode instructions:\n{mode_prompt}",
                f"Human objective: {objective or 'Mode-directed discovery'}",
                "Accepted candidate:\n" + json.dumps(candidate, indent=2),
                "Make the smallest complete change. Add or update regression tests where practical. Run the most relevant existing tests. "
                "Do not weaken tests, swallow failures, fabricate data, or widen scope. Preserve saved data, URLs, exports, APIs, configuration, and client state by default. "
                "If the work crosses a red-risk boundary, stop without making the risky change and return result=blocked or escalate.",
                "Return a conventional semantic commit subject that describes the actual change, plus changed files, tests added/run, limitations, and risk.",
                "Independent review feedback to address:\n" + json.dumps(implementation_feedback, indent=2),
                "Validation feedback to address:\n" + json.dumps(validation_feedback, indent=2),
            ]
        )

    def _review_prompt(
        self,
        mode: str,
        candidate: dict[str, Any],
        diff: str,
        tests: dict[str, Any],
        before_capture: dict[str, Any],
        after_capture: dict[str, Any],
    ) -> str:
        review = _load_prompt(_repo_prompts() / "review.md")
        return "\n\n".join(
            [
                "ROLE: REVIEWER. Read-only. Do not modify files.",
                f"Mode: {mode}.\n{review}",
                "Candidate and acceptance criteria:\n" + json.dumps(candidate, indent=2),
                "Configured validation result:\n" + json.dumps(tests, indent=2),
                "Browser evidence metadata:\n" + json.dumps({"before": before_capture, "after": after_capture}, indent=2),
                "Staged diff:\n```diff\n" + _clip(diff, 250_000) + "\n```",
                "Return approved=true only when the diff meets acceptance criteria, configured checks pass, and no blocking correctness, compatibility, product, security, or UX concern remains.",
            ]
        )

    def _run_checks(
        self,
        project: ProjectConfig,
        workspace: Path,
        evidence_dir: Path,
        phase: str,
        deadline: float,
    ) -> dict[str, Any]:
        if not project.check_commands:
            return {"status": "not_configured", "commands": []}
        results = []
        for index, command in enumerate(project.check_commands, start=1):
            command_result: dict[str, Any] = {"argv": list(command)}
            try:
                process = subprocess.run(
                    list(command),
                    cwd=workspace,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self._remaining_timeout(deadline, self.policy.check_timeout_seconds),
                    env={**os.environ, "WIGGUM_EVIDENCE_DIR": str(evidence_dir)},
                    check=False,
                )
                command_result.update(
                    {
                        "exit_code": process.returncode,
                        "stdout": _clip(process.stdout),
                        "stderr": _clip(process.stderr),
                    }
                )
                command_result["status"] = "passed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                command_result.update({"status": "failed", "error": f"timed out after {exc.timeout} seconds"})
            except OSError as exc:
                command_result.update({"status": "failed", "error": str(exc)})
            write_private_json(evidence_dir / f"{phase}-{index}.json", command_result)
            results.append(command_result)
        status = "passed" if all(item.get("status") == "passed" for item in results) else "failed"
        return {"status": status, "commands": results}

    @staticmethod
    def _remaining_timeout(deadline: float, maximum: int) -> int:
        remaining = min(maximum, int(deadline - time.monotonic()))
        if remaining < 1:
            raise CycleError("The cycle time budget is exhausted")
        return remaining

    def _capture_screenshot(
        self,
        project: ProjectConfig,
        workspace: Path,
        evidence_dir: Path,
        cycle_id: str,
        phase: str,
        deadline: float,
    ) -> tuple[Path | None, dict[str, Any]]:
        if not project.browser_capture_command:
            return None, {"status": "not_configured"}
        screenshot = evidence_dir / f"{phase}.png"
        env = {
            **os.environ,
            "WIGGUM_SCREENSHOT_PATH": str(screenshot),
            "WIGGUM_EVIDENCE_DIR": str(evidence_dir),
            "WIGGUM_CYCLE_ID": cycle_id,
        }
        try:
            result = subprocess.run(
                list(project.browser_capture_command),
                cwd=workspace,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._remaining_timeout(deadline, self.policy.check_timeout_seconds),
                check=False,
            )
            details = {
                "status": "captured" if result.returncode == 0 and screenshot.is_file() else "failed",
                "exit_code": result.returncode,
                "screenshot": str(screenshot) if screenshot.is_file() else None,
                "stdout": _clip(result.stdout),
                "stderr": _clip(result.stderr),
            }
            write_private_json(evidence_dir / f"browser-{phase}.json", details)
            return (screenshot if screenshot.is_file() else None), details
        except (OSError, subprocess.TimeoutExpired) as exc:
            details = {"status": "failed", "error": str(exc)}
            write_private_json(evidence_dir / f"browser-{phase}.json", details)
            return None, details
