from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex import CodexError, run_agent
from .config import ApplicationConfig
from .git_ops import (
    GitError,
    SafetyError,
    add_branch_worktree,
    commit_staged,
    current_branch,
    delete_branch_if_at,
    git,
    head_sha,
    is_clean,
    remove_worktree,
    repo_root,
    stage_changes,
)
from .state import (
    ProjectState,
    ensure_private_dir,
    read_json,
    safe_id,
    utc_now,
    write_private_json,
    write_private_text,
)


def _wiggum_root() -> Path:
    return Path(__file__).resolve().parents[2]


def gather_cycle_evidence(config: ApplicationConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[Path, ...]]:
    iterations: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    state_dirs: list[Path] = []
    for alias, project in config.projects.items():
        state = ProjectState(project.state_dir)
        state_dirs.append(state.root)
        iterations.extend(
            [{**record, "project_alias": alias} for record in state.iteration_records()]
        )
        raw_decisions = read_json(state.decisions_path, [])
        if isinstance(raw_decisions, list):
            decisions.extend([{**item, "project_alias": alias} for item in raw_decisions if isinstance(item, dict)])
    iterations.sort(key=lambda item: item.get("recorded_at", item.get("completed_at", "")))
    decisions.sort(key=lambda item: item.get("recorded_at", ""))
    return iterations, decisions, tuple(state_dirs)


def detect_early_signals(
    iterations: list[dict[str, Any]], decisions: list[dict[str, Any]]
) -> list[str]:
    signals: set[str] = set()
    if any(item.get("serious_regression") or item.get("status") == "serious_regression" for item in iterations):
        signals.add("serious_regression")

    failures = {"error", "validation_failed", "review_rejected"}
    if len(iterations) >= 3 and all(item.get("status") in failures for item in iterations[-3:]):
        signals.add("mode_failure_spike")

    rollback_count = sum(1 for item in decisions if item.get("decision") == "rollback")
    if rollback_count >= 2:
        signals.add("repeated_rollback")

    blocked_reasons: dict[str, int] = {}
    for item in decisions:
        if item.get("decision") != "defer":
            continue
        reason = str(item.get("reason", "")).lower()
        key = re.sub(r"[^a-z]+", " ", reason).strip()
        if key:
            blocked_reasons[key] = blocked_reasons.get(key, 0) + 1
    if any(count >= 3 for count in blocked_reasons.values()):
        signals.add("repeated_human_blocker")

    bad_feature_count = sum(
        1
        for item in decisions
        if item.get("mode") == "feature" and item.get("decision") in {"rejected", "rollback"}
    )
    if bad_feature_count >= 3:
        signals.add("repeated_bad_feature_selection")

    # Token/time telemetry is recorded for future trend analysis. This conservative
    # signal only triggers when the latest three cycle records explicitly flag it.
    if sum(1 for item in iterations[-3:] if item.get("efficiency_regression")) >= 3:
        signals.add("efficiency_regression")
    return sorted(signals)


def self_analysis_eligibility(
    iterations: list[dict[str, Any]],
    policy: dict[str, Any],
    last_analysis_cycle: int = 0,
    early_signals: list[str] | None = None,
) -> tuple[bool, str]:
    completed = [item for item in iterations if item.get("status") not in {"error", "interrupted"}]
    count = len(completed)
    minimum = int(policy.get("minimum_cycles_before_analysis", 30))
    cadence = int(policy.get("periodic_every_new_cycles", 50))
    allowed_signals = set(policy.get("early_analysis_signals", []))
    active_signals = sorted(set(early_signals or []) & allowed_signals)
    if active_signals:
        return True, "early analysis signal: " + ", ".join(active_signals)
    if count < minimum:
        return False, f"self-analysis requires {minimum} completed cycles; found {count}"
    if last_analysis_cycle == 0:
        return True, f"first self-analysis is due after {count} completed cycles"
    if count - last_analysis_cycle < cadence:
        return False, f"self-analysis is not due until {cadence} new completed cycles accumulate"
    return True, f"periodic self-analysis is due after {count - last_analysis_cycle} new cycles"


def _cycle_id(proposal: dict[str, Any]) -> str:
    raw = json.dumps(proposal, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _check_wiggum_checkout(root: Path) -> tuple[str, str]:
    resolved = repo_root(root)
    if resolved != root.resolve():
        raise SafetyError(f"Wiggum source root does not match its Git root: {resolved}")
    branch = current_branch(root)
    if branch.lower() in {"main", "master", "trunk"}:
        raise SafetyError("Self-improvement experiments require a non-protected Wiggum branch")
    if not is_clean(root):
        raise SafetyError("Wiggum source checkout must be clean before starting a self-improvement experiment")
    return branch, head_sha(root)


def _run_wiggum_checks(worktree: Path, evidence_dir: Path) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": "src", "WIGGUM_EVIDENCE_DIR": str(evidence_dir)}
    commands = [
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-m", "compileall", "-q", "src"],
    ]
    results = []
    for index, command in enumerate(commands, start=1):
        process = subprocess.run(
            command,
            cwd=worktree,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=False,
        )
        result = {
            "argv": command,
            "exit_code": process.returncode,
            "status": "passed" if process.returncode == 0 else "failed",
            "stdout": process.stdout[-100_000:],
            "stderr": process.stderr[-100_000:],
        }
        write_private_json(evidence_dir / f"self-check-{index}.json", result)
        results.append(result)
    return {
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "commands": results,
    }


def _run_experiment(
    config: ApplicationConfig,
    codex: str,
    proposal: dict[str, Any],
    analysis_dir: Path,
    supporting_examples_min: int,
) -> dict[str, Any]:
    title = str(proposal.get("title", "Wiggum improvement"))
    identifier = _cycle_id(proposal)
    experiment_dir = ensure_private_dir(analysis_dir / f"experiment-{identifier}")
    support = proposal.get("supporting_cycle_ids", [])
    outcome: dict[str, Any] = {
        "proposal_id": identifier,
        "title": title,
        "status": "deferred",
        "supporting_cycle_ids": support,
        "started_at": utc_now(),
        "branch": None,
        "commit": None,
        "limitations": [],
    }
    if proposal.get("decision") != "implement":
        outcome["reason"] = "Self-analysis deferred this proposal."
        write_private_json(experiment_dir / "experiment.json", outcome)
        return outcome
    if len(support) < supporting_examples_min and not proposal.get("critical_single_incident"):
        outcome["reason"] = f"Proposal has fewer than {supporting_examples_min} supporting cycles."
        write_private_json(experiment_dir / "experiment.json", outcome)
        return outcome
    if not config.policy.self_improvement.get("branch_isolated", True):
        outcome["reason"] = "Policy requires self-improvement experiments to use an isolated branch."
        write_private_json(experiment_dir / "experiment.json", outcome)
        return outcome

    root = _wiggum_root()
    worktree: Path | None = None
    container: Path | None = None
    branch: str | None = None
    base_sha: str | None = None
    committed = False
    try:
        _, base_sha = _check_wiggum_checkout(root)
        branch = f"wiggum/self-improve/{safe_id(title.lower())[:36]}-{identifier}"
        container = Path(tempfile.mkdtemp(prefix=f"wiggum-self-{identifier}-"))
        worktree = container / "repo"
        add_branch_worktree(root, worktree, branch, base_sha)
        outcome["branch"] = branch
        outcome["base_sha"] = base_sha

        prompt = "\n\n".join(
            [
                "ROLE: WIGGUM SELF-IMPROVEMENT IMPLEMENTER. Work only in this isolated self-improvement branch.",
                "Implement the accepted evidence-backed proposal below. Add replayable regression/evaluation coverage where practical. "
                "Do not change the trusted/main execution path, merge, push, or promote. Do not commit; Wiggum owns commit creation.",
                "Never request or persist hidden chain-of-thought. Keep observable rationale summaries and tests.",
                "Proposal:\n" + json.dumps(proposal, indent=2),
                "Return a semantic commit subject and summarize changed files, tests, risks, and known limitations.",
            ]
        )
        write_private_text(experiment_dir / "implementer.prompt.md", prompt)
        implementation = run_agent(
            executable=codex,
            workspace=worktree,
            role="implementer",
            prompt=prompt,
            schema_path=_wiggum_root() / "schemas" / "implementation.json",
            evidence_dir=experiment_dir,
            policy=config.policy,
            writable=True,
        )
        write_private_json(experiment_dir / "implementation.json", implementation.result)
        outcome["implementer_session"] = implementation.session_id
        if implementation.result.get("risk") == "red" or implementation.result.get("result") in {"blocked", "escalate"}:
            outcome["reason"] = implementation.result.get("summary", "Self-improvement crossed a red-risk boundary")
            return outcome

        changed, diff = stage_changes(worktree)
        if not changed:
            outcome["reason"] = implementation.result.get("summary", "No Wiggum source change was produced.")
            return outcome
        write_private_text(experiment_dir / "experiment.patch", diff)
        checks = _run_wiggum_checks(worktree, experiment_dir)
        outcome["checks"] = checks
        if checks["status"] != "passed":
            outcome["reason"] = "Wiggum self-checks failed; experiment was not committed."
            return outcome

        review_prompt = "\n\n".join(
            [
                "ROLE: INDEPENDENT WIGGUM SELF-IMPROVEMENT REVIEWER. Read-only. Do not edit or promote code.",
                "Review the proposal and staged diff for correctness, evidence fit, regression coverage, privacy, rollback, and trusted-runtime isolation. "
                "Assume the change should not be accepted until the evidence proves otherwise.",
                "Proposal:\n" + json.dumps(proposal, indent=2),
                "Check results:\n" + json.dumps(checks, indent=2),
                "Staged diff:\n```diff\n" + diff[-250_000:] + "\n```",
            ]
        )
        write_private_text(experiment_dir / "reviewer.prompt.md", review_prompt)
        review = run_agent(
            executable=codex,
            workspace=worktree,
            role="reviewer",
            prompt=review_prompt,
            schema_path=_wiggum_root() / "schemas" / "review.json",
            evidence_dir=experiment_dir,
            policy=config.policy,
            writable=False,
        )
        write_private_json(experiment_dir / "review.json", review.result)
        outcome["reviewer_session"] = review.session_id
        outcome["review"] = review.result
        if not review.result.get("approved") or review.result.get("findings") or review.result.get("missing_checks"):
            outcome["reason"] = review.result.get("summary", "Independent review did not approve the experiment")
            return outcome

        subject = str(implementation.result.get("commit_subject", ""))
        commit = commit_staged(worktree, subject)
        committed = True
        outcome.update(
            {
                "status": "experiment_ready",
                "commit": commit,
                "commit_subject": subject,
                "summary": implementation.result.get("summary", "Isolated self-improvement experiment passed review."),
                "limitations": implementation.result.get("limitations", []),
                "promotion": "human approval required; not merged into the trusted runtime",
            }
        )
        return outcome
    except Exception as exc:
        outcome["reason"] = str(exc)
        if worktree and worktree.exists():
            try:
                _, diff = stage_changes(worktree)
                if diff:
                    write_private_text(experiment_dir / "failed-experiment.patch", diff)
            except Exception:
                pass
        return outcome
    finally:
        if worktree and worktree.exists():
            remove_worktree(_wiggum_root(), worktree)
        if container:
            shutil.rmtree(container, ignore_errors=True)
        if branch and base_sha and not committed:
            delete_branch_if_at(_wiggum_root(), branch, base_sha)
        outcome["completed_at"] = utc_now()
        write_private_json(experiment_dir / "experiment.json", outcome)


def run_self_analysis(config: ApplicationConfig, codex: str = "codex") -> dict[str, Any]:
    policy = config.policy.self_improvement
    if not policy.get("enabled", True):
        return {"eligible": False, "reason": "self-improvement is disabled by policy"}

    iterations, decisions, state_dirs = gather_cycle_evidence(config)
    analysis_root = ensure_private_dir(config.state_root / "self-improvement")
    meta_path = analysis_root / "state.json"
    meta = read_json(
        meta_path, {"last_analysis_cycle": 0, "last_analysis_decision_count": 0}
    )
    previous_count = int(meta.get("last_analysis_cycle", 0))
    completed_iterations = [
        item for item in iterations if item.get("status") not in {"error", "interrupted"}
    ]
    recent_iterations = completed_iterations[previous_count:]
    previous_decision_count = int(meta.get("last_analysis_decision_count", 0))
    recent_decisions = decisions[previous_decision_count:]
    early_signals = detect_early_signals(recent_iterations, recent_decisions)
    eligible, reason = self_analysis_eligibility(
        completed_iterations, policy, previous_count, early_signals
    )
    if not eligible:
        return {"eligible": False, "reason": reason, "completed_cycles": len(iterations)}

    analysis_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = ensure_private_dir(analysis_root / f"analysis-{analysis_id}")
    payload = {
        "recent_window": iterations[-50:],
        "full_history_summaries": [
            {
                "cycle_id": item.get("cycle_id"),
                "project_alias": item.get("project_alias"),
                "mode": item.get("mode"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "commit": item.get("commit"),
                "tests": item.get("tests"),
                "evidence_dir": item.get("evidence_dir"),
            }
            for item in iterations
        ],
        "recent_decisions": decisions[-100:],
        "signals": early_signals,
        "thresholds": policy,
    }
    prompt = "\n\n".join(
        [
            "ROLE: WIGGUM SELF-ANALYST. Read-only; do not modify files or promote code.",
            (_wiggum_root() / "prompts" / "self-analysis.md").read_text(encoding="utf-8"),
            "Evidence corpus summary:\n" + json.dumps(payload, indent=2),
            "The external read-only directories contain this project's redacted raw Codex JSONL event traces. "
            "Inspect recent traces and representative historical traces before proposing behavioral changes. "
            "Treat them as sensitive evidence; do not copy credentials or secrets into your proposal.",
        ]
    )
    write_private_text(evidence_dir / "analyst.prompt.md", prompt)
    analysis = run_agent(
        executable=codex,
        workspace=_wiggum_root(),
        role="self-analyst",
        prompt=prompt,
        schema_path=_wiggum_root() / "schemas" / "self-analysis.json",
        evidence_dir=evidence_dir,
        policy=config.policy,
        writable=False,
        extra_readonly_dirs=state_dirs,
    )
    result = analysis.result
    minimum_examples = int(policy.get("minimum_supporting_examples", 3))
    known_cycle_ids = {
        str(item.get("cycle_id")) for item in iterations if item.get("cycle_id")
    }
    eligible_proposals = []
    for proposal in result.get("proposals", []):
        supporting_ids = proposal.get("supporting_cycle_ids", [])
        if not isinstance(supporting_ids, list):
            supporting_ids = []
        verified_ids = sorted({str(item) for item in supporting_ids} & known_cycle_ids)
        proposal["supporting_cycle_ids"] = verified_ids
        count = len(verified_ids)
        if count >= minimum_examples or (
            count >= 1 and proposal.get("critical_single_incident")
        ):
            eligible_proposals.append(proposal)
    result["proposals"] = eligible_proposals
    result["early_signals"] = early_signals
    write_private_json(evidence_dir / "analysis.json", result)

    experiments = []
    if policy.get("auto_implement_experiments", True):
        for proposal in eligible_proposals:
            experiments.append(
                _run_experiment(config, codex, proposal, evidence_dir, minimum_examples)
            )
    result["experiments"] = experiments
    write_private_json(evidence_dir / "analysis.json", result)

    meta.update(
        {
            "last_analysis_cycle": len(completed_iterations),
            "last_analysis_decision_count": len(decisions),
            "last_analysis_id": analysis_id,
            "last_analysis_at": utc_now(),
        }
    )
    write_private_json(meta_path, meta)
    return {
        "eligible": True,
        "reason": reason,
        "analysis_id": analysis_id,
        "summary": result.get("summary", ""),
        "early_signals": early_signals,
        "proposals": eligible_proposals,
        "experiments": experiments,
        "evidence_dir": str(evidence_dir),
        "promotion": "human approval required; experiments remain isolated",
    }
