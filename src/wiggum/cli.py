from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .config import (
    ADVENTUROUSNESS,
    DEFAULT_CONFIG_DIR,
    MODES,
    ConfigurationError,
    get_project,
    load_configuration,
)
from .git_ops import (
    GitError,
    count_commits_ahead,
    current_branch,
    head_sha,
    is_clean,
    revert_commit,
)
from .runner import CycleError, WiggumRunner
from .self_improvement import (
    detect_early_signals,
    gather_cycle_evidence,
    run_self_analysis,
    self_analysis_eligibility,
)
from .state import ProjectState, read_json


def _default_config_dir() -> Path:
    return Path(os.environ.get("WIGGUM_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wiggum", description="Guarded autonomous product iteration")
    parser.add_argument("--config-dir", type=Path, default=_default_config_dir())
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for mode in MODES:
        mode_parser = subparsers.add_parser(mode, help=f"Run one {mode.upper()} iteration loop")
        mode_parser.add_argument("objective", nargs="?", help="Required for bugfix and directed modes")
        mode_parser.add_argument("--project", required=True)
        mode_parser.add_argument("--cycles", type=int)
        mode_parser.add_argument("--forever", action="store_true")
        if mode == "feature":
            mode_parser.add_argument("--selection", choices=("auto", "council"))
            mode_parser.add_argument("--adventurousness", choices=ADVENTUROUSNESS)

    stop_parser = subparsers.add_parser("stop", help="Request a graceful stop after the active cycle")
    stop_parser.add_argument("--project", required=True)

    pivot_parser = subparsers.add_parser("pivot", help="Request a graceful mode pivot after the active cycle")
    pivot_parser.add_argument("mode", choices=MODES)
    pivot_parser.add_argument("--project", required=True)
    pivot_parser.add_argument("--objective", help="New objective when pivoting to bugfix or directed mode")

    status_parser = subparsers.add_parser("status", help="Show project branch, batch, and run status")
    status_parser.add_argument("--project", required=True)

    backlog_parser = subparsers.add_parser("backlog", help="Show Wiggum candidate decisions")
    backlog_parser.add_argument("--project", required=True)

    rollback_parser = subparsers.add_parser("rollback", help="Create a revert commit for Wiggum work")
    rollback_parser.add_argument("--project", required=True)
    rollback_parser.add_argument("--commit", required=True, help="Integrated Wiggum commit SHA")

    self_parser = subparsers.add_parser("self-improve", help="Check evidence thresholds for Wiggum self-analysis")
    self_parser.add_argument("--project", required=True)
    self_parser.add_argument("--analyze", action="store_true", help="Run read-only analysis when eligible")

    config_parser = subparsers.add_parser("config", help="Validate local Wiggum configuration")
    config_parser.add_subparsers(dest="config_command", required=True).add_parser("validate")
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _validate_all(config_dir: Path) -> dict[str, object]:
    config = load_configuration(config_dir)
    projects = []
    for alias in sorted(config.projects):
        project = get_project(config, alias)
        projects.append(
            {
                "alias": alias,
                "path": str(project.repo_path),
                "integration_branch": project.integration_branch,
                "base_branch": project.base_branch,
                "checks": [list(command) for command in project.check_commands],
            }
        )
    return {
        "valid": True,
        "projects": projects,
        "model": config.policy.model,
        "reasoning_effort": config.policy.reasoning_effort,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_configuration(args.config_dir)
        if args.command == "config":
            _print_json(_validate_all(args.config_dir))
            return 0

        project = get_project(config, args.project)
        state = ProjectState(project.state_dir)

        if args.command in MODES:
            result = WiggumRunner(config, codex_executable=args.codex).run(
                project,
                args.command,
                objective=args.objective,
                cycles=args.cycles,
                forever=args.forever,
                selection=getattr(args, "selection", None),
                adventurousness=getattr(args, "adventurousness", None),
            )
            _print_json(result)
            return 0

        if args.command == "stop":
            state.request_control("stop")
            print(f"Stop requested for {project.alias}; the active cycle will finish first.")
            return 0

        if args.command == "pivot":
            if args.mode in {"bugfix", "directed"} and not (
                args.objective and args.objective.strip()
            ):
                raise CycleError(f"pivoting to {args.mode} requires --objective")
            state.request_control("pivot", args.mode, args.objective)
            print(f"Pivot to {args.mode} requested for {project.alias}; it takes effect at a cycle boundary.")
            return 0

        if args.command == "status":
            branch = current_branch(project.repo_path)
            ahead = count_commits_ahead(
                project.repo_path, project.base_branch, project.integration_branch
            )
            metrics = read_json(state.root / "metrics.json", {"consecutive_low_value_cycles": 0})
            _print_json(
                {
                    "project": project.alias,
                    "branch": branch,
                    "head": head_sha(project.repo_path),
                    "clean": is_clean(project.repo_path),
                    "on_integration_branch": branch == project.integration_branch,
                    "commits_since_base": ahead,
                    "batch_review_after": config.policy.batch_review_after_commits,
                    "consecutive_low_value_cycles": metrics.get("consecutive_low_value_cycles", 0),
                    "pending_control": state.read_control(),
                    "evidence_dir": str(state.root),
                }
            )
            return 0

        if args.command == "backlog":
            _print_json(read_json(state.backlog_path, {}))
            return 0

        if args.command == "rollback":
            sha = revert_commit(project.repo_path, project.integration_branch, args.commit)
            matching_cycle = next(
                (
                    record
                    for record in state.iteration_records()
                    if isinstance(record.get("commit"), dict)
                    and record["commit"].get("sha") == args.commit
                ),
                {},
            )
            state.add_decision(
                {
                    "decision": "rollback",
                    "commit": args.commit,
                    "mode": matching_cycle.get("mode"),
                    "candidate_id": matching_cycle.get("candidate_id"),
                    "resulting_sha": sha,
                    "reason": "Operator requested a semantic revert; history remains intact.",
                }
            )
            _print_json({"reverted": args.commit, "resulting_sha": sha})
            return 0

        if args.command == "self-improve":
            if not args.analyze:
                iterations, decisions, _ = gather_cycle_evidence(config)
                meta_path = config.state_root / "self-improvement" / "state.json"
                meta = read_json(meta_path, {})
                last_count = int(meta.get("last_analysis_cycle", 0))
                last_decision_count = int(meta.get("last_analysis_decision_count", 0))
                completed = [
                    item for item in iterations if item.get("status") not in {"error", "interrupted"}
                ]
                eligible, reason = self_analysis_eligibility(
                    completed,
                    config.policy.self_improvement,
                    last_count,
                    detect_early_signals(
                        completed[last_count:], decisions[last_decision_count:]
                    ),
                )
                _print_json({"eligible": eligible, "reason": reason, "completed_cycles": len(completed)})
                return 0
            _print_json(run_self_analysis(config, args.codex))
            return 0

        parser.error(f"Unsupported command {args.command}")
    except (ConfigurationError, CycleError, GitError, RuntimeError, ValueError) as exc:
        print(f"wiggum: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
