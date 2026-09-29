from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    pass


DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
MODES = ("bugfix", "harden", "feature", "polish", "directed")
ADVENTUROUSNESS = ("conservative", "product_minded", "inventive", "founder")


@dataclass(frozen=True)
class RuntimePolicy:
    default_cycles: int
    low_value_stop_after: int
    batch_review_after_commits: int
    review_fix_rounds: int
    agent_timeout_seconds: int
    check_timeout_seconds: int
    max_cycle_seconds: int
    baseline_checks: bool
    max_agent_output_bytes: int
    max_evidence_bytes: int
    model: str
    reasoning_effort: str
    default_adventurousness: str
    default_selection: str
    candidate_count: int
    feature_weights: dict[str, float]
    self_improvement: dict[str, Any]


@dataclass(frozen=True)
class ProjectConfig:
    alias: str
    repo_path: Path
    integration_branch: str
    base_branch: str
    constitution: str | None
    state_dir: Path
    check_commands: tuple[tuple[str, ...], ...]
    browser_capture_command: tuple[str, ...] | None


@dataclass(frozen=True)
class ApplicationConfig:
    config_dir: Path
    state_root: Path
    policy: RuntimePolicy
    projects: dict[str, ProjectConfig]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Missing configuration file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc


def _path(value: str | Path, base: Path) -> Path:
    expanded = Path(value).expanduser()
    return expanded.resolve() if expanded.is_absolute() else (base / expanded).resolve()


def _command_list(value: Any, key: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{key} must be an array of argv arrays")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ConfigurationError(f"Each {key} entry must be a non-empty string array")
        commands.append(tuple(command))
    return tuple(commands)


def load_configuration(config_dir: Path = DEFAULT_CONFIG_DIR) -> ApplicationConfig:
    config_dir = config_dir.expanduser().resolve()
    raw_policy = _read_toml(config_dir / "policies.toml")
    if not (config_dir / "projects.toml").is_file():
        raise ConfigurationError(
            f"Missing {config_dir / 'projects.toml'}; copy projects.example.toml and edit it"
        )
    raw_projects = _read_toml(config_dir / "projects.toml")

    runtime = raw_policy.get("runtime", {})
    model = raw_policy.get("model", {})
    feature = raw_policy.get("feature", {})
    self_improvement = raw_policy.get("self_improvement", {})
    effort = str(model.get("reasoning_effort", "max"))
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigurationError("model.reasoning_effort must be none, low, medium, high, xhigh, or max")

    adventurousness = str(feature.get("default_adventurousness", "product_minded"))
    if adventurousness not in ADVENTUROUSNESS:
        raise ConfigurationError(
            "feature.default_adventurousness must be one of: " + ", ".join(ADVENTUROUSNESS)
        )
    selection = str(feature.get("default_selection", "auto"))
    if selection not in {"auto", "council"}:
        raise ConfigurationError("feature.default_selection must be 'auto' or 'council'")

    weights = feature.get("weights", {})
    if not isinstance(weights, dict) or any(
        not isinstance(value, (int, float)) or value < 0 for value in weights.values()
    ):
        raise ConfigurationError("feature.weights must map names to non-negative numbers")

    policy = RuntimePolicy(
        default_cycles=int(runtime.get("default_cycles", 1)),
        low_value_stop_after=int(runtime.get("low_value_stop_after", 5)),
        batch_review_after_commits=int(runtime.get("batch_review_after_commits", 15)),
        review_fix_rounds=int(runtime.get("review_fix_rounds", 1)),
        agent_timeout_seconds=int(runtime.get("agent_timeout_seconds", 3600)),
        check_timeout_seconds=int(runtime.get("check_timeout_seconds", 1800)),
        max_cycle_seconds=int(runtime.get("max_cycle_seconds", 14400)),
        baseline_checks=bool(runtime.get("baseline_checks", True)),
        max_agent_output_bytes=int(runtime.get("max_agent_output_bytes", 20_000_000)),
        max_evidence_bytes=int(runtime.get("max_evidence_bytes", 100_000_000)),
        model=str(model.get("id", "gpt-6-luna")),
        reasoning_effort=effort,
        default_adventurousness=adventurousness,
        default_selection=selection,
        candidate_count=int(feature.get("candidate_count", 5)),
        feature_weights={str(key): float(value) for key, value in weights.items()},
        self_improvement=dict(self_improvement),
    )
    if policy.default_cycles < 1 or policy.low_value_stop_after < 1 or policy.max_cycle_seconds < 1:
        raise ConfigurationError("Cycle counts and low-value stop threshold must be positive")
    if policy.batch_review_after_commits < 1:
        raise ConfigurationError("batch_review_after_commits must be positive")

    raw_project_map = raw_projects.get("projects", {})
    if not isinstance(raw_project_map, dict) or not raw_project_map:
        raise ConfigurationError(
            f"No projects configured in {config_dir / 'projects.toml'}; copy projects.example.toml and edit it"
        )

    state_root_default = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")) / "wiggum"
    state_root_raw = raw_policy.get("state", {}).get("root", str(state_root_default))
    state_root = _path(state_root_raw, config_dir)
    projects: dict[str, ProjectConfig] = {}
    for alias, raw in raw_project_map.items():
        if not isinstance(raw, dict):
            raise ConfigurationError(f"Project {alias!r} must be a TOML table")
        path_value = raw.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ConfigurationError(f"Project {alias!r} must define a non-empty path")
        repo_path = _path(path_value, config_dir)
        state_dir = (
            _path(raw["state_dir"], config_dir)
            if raw.get("state_dir")
            else state_root / "projects" / alias
        )
        capture = raw.get("browser_capture_command")
        if capture is not None:
            parsed_capture = _command_list([capture], f"projects.{alias}.browser_capture_command")
            capture_command = parsed_capture[0]
        else:
            capture_command = None
        project = ProjectConfig(
            alias=str(alias),
            repo_path=repo_path,
            integration_branch=str(raw.get("integration_branch", "")),
            base_branch=str(raw.get("base_branch", "main")),
            constitution=str(raw["constitution"]) if raw.get("constitution") else None,
            state_dir=state_dir,
            check_commands=_command_list(
                raw.get("check_commands", []), f"projects.{alias}.check_commands"
            ),
            browser_capture_command=capture_command,
        )
        if project.state_dir == repo_path or repo_path in project.state_dir.parents:
            raise ConfigurationError(
                f"Project {alias!r} state_dir must be outside the target repository"
            )
        projects[str(alias)] = project

    return ApplicationConfig(
        config_dir=config_dir,
        state_root=state_root,
        policy=policy,
        projects=projects,
    )


def get_project(config: ApplicationConfig, alias: str) -> ProjectConfig:
    try:
        project = config.projects[alias]
    except KeyError as exc:
        available = ", ".join(sorted(config.projects))
        raise ConfigurationError(f"Unknown project {alias!r}; configured projects: {available}") from exc
    if not project.repo_path.is_dir():
        raise ConfigurationError(f"Project path does not exist: {project.repo_path}")
    if not project.integration_branch:
        raise ConfigurationError(f"Project {alias!r} must define integration_branch")
    if project.integration_branch == project.base_branch:
        raise ConfigurationError("integration_branch must differ from the protected base_branch")
    return project
