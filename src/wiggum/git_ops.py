from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .state import safe_id


class GitError(RuntimeError):
    pass


class SafetyError(GitError):
    pass


@dataclass(frozen=True)
class GitSnapshot:
    branch: str
    sha: str
    root: Path


_NEVER_COMMIT = (
    ".env",
    ".env.*",
    "*credentials*",
    "*secrets*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
)
_ALLOWED_ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template")
_PROTECTED_BRANCH_NAMES = {"main", "master", "trunk"}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return result


def repo_root(repo: Path) -> Path:
    result = git(repo, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def current_branch(repo: Path) -> str:
    result = git(repo, "branch", "--show-current")
    branch = result.stdout.strip()
    if not branch:
        raise SafetyError("The target checkout is detached; Wiggum requires its integration branch")
    return branch


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def is_clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain", "--untracked-files=all").stdout


def assert_safe_project(repo: Path, integration_branch: str, base_branch: str) -> GitSnapshot:
    resolved = repo_root(repo)
    if resolved != repo.resolve():
        raise SafetyError(f"Configured target must be the repository root: {resolved}")
    branch = current_branch(repo)
    if branch != integration_branch:
        raise SafetyError(
            f"Target is on {branch!r}; switch to configured integration branch {integration_branch!r} first"
        )
    if branch == base_branch or branch.lower() in _PROTECTED_BRANCH_NAMES:
        raise SafetyError(f"Refusing to operate on protected base branch {branch!r}")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{base_branch}", check=False).returncode != 0:
        raise SafetyError(f"Configured base branch {base_branch!r} is not available locally")
    if not is_clean(repo):
        raise SafetyError("Target checkout has uncommitted or untracked files; clean it before a Wiggum run")
    return GitSnapshot(branch=branch, sha=head_sha(repo), root=resolved)


def add_detached_worktree(repo: Path, path: Path, base_sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), base_sha)


def add_branch_worktree(repo: Path, path: Path, branch: str, base_sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise SafetyError(f"Self-improvement branch already exists: {branch}")
    git(repo, "worktree", "add", "-b", branch, str(path), base_sha)


def delete_branch_if_at(repo: Path, branch: str, expected_sha: str) -> None:
    ref = f"refs/heads/{branch}"
    result = git(repo, "rev-parse", "--verify", ref, check=False)
    if result.returncode == 0 and result.stdout.strip() == expected_sha:
        git(repo, "branch", "-D", branch)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)


def _changed_paths(worktree: Path) -> list[str]:
    changed = git(worktree, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
    untracked = git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z"
    ).stdout.split("\0")
    return sorted({name for name in [*changed, *untracked] if name})


def _protected_path(path: str) -> bool:
    name = Path(path).name.lower()
    if name in _ALLOWED_ENV_EXAMPLES:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in _NEVER_COMMIT)


def stage_changes(worktree: Path) -> tuple[bool, str]:
    paths = _changed_paths(worktree)
    blocked = [path for path in paths if _protected_path(path)]
    if blocked:
        raise SafetyError("Refusing to stage secret-like files: " + ", ".join(blocked))
    git(worktree, "add", "--all")
    if git(worktree, "diff", "--cached", "--check", check=False).returncode != 0:
        raise SafetyError("Staged changes contain whitespace errors; inspect the cycle evidence")
    changed = bool(git(worktree, "diff", "--cached", "--name-only").stdout.strip())
    diff = git(worktree, "diff", "--cached", "--binary", "--no-ext-diff").stdout
    return changed, diff


def commit_staged(worktree: Path, subject: str) -> str:
    prefix = subject.split(":", 1)[0].strip().lower() if ":" in subject else ""
    allowed = {"feat", "fix", "refactor", "docs", "test", "chore", "perf", "build", "ci"}
    if prefix not in allowed or not subject.strip() or len(subject) > 100:
        raise SafetyError(
            "Commit subject must be a concise semantic message such as 'fix: preserve saved dates'"
        )
    if git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        raise SafetyError("There are no staged changes to commit")
    git(worktree, "commit", "-m", subject)
    return head_sha(worktree)


def merge_reviewed_commit(
    repo: Path,
    integration_branch: str,
    base_sha: str,
    commit_sha: str,
    checkpoint_id: str,
) -> str:
    if current_branch(repo) != integration_branch:
        raise SafetyError("Integration branch changed during the cycle; commit was not integrated")
    if not is_clean(repo):
        raise SafetyError("Integration checkout changed during the cycle; commit was not integrated")
    if head_sha(repo) != base_sha:
        pending_ref = f"refs/heads/wiggum/pending/{safe_id(checkpoint_id)}"
        git(repo, "update-ref", pending_ref, commit_sha)
        raise SafetyError(
            f"Integration branch advanced during the cycle; preserved reviewed commit at {pending_ref}"
        )

    checkpoint_ref = f"refs/wiggum/checkpoints/{safe_id(checkpoint_id)}"
    git(repo, "update-ref", checkpoint_ref, base_sha)
    result = git(repo, "merge", "--ff-only", commit_sha, check=False)
    if result.returncode:
        pending_ref = f"refs/heads/wiggum/pending/{safe_id(checkpoint_id)}"
        git(repo, "update-ref", pending_ref, commit_sha)
        raise GitError(
            "Fast-forward integration failed; preserved the commit at "
            f"{pending_ref}: {result.stderr.strip()}"
        )
    if head_sha(repo) != commit_sha:
        raise GitError("Git merge completed at an unexpected revision")
    return checkpoint_ref


def count_commits_ahead(repo: Path, base_branch: str, integration_branch: str) -> int:
    result = git(repo, "rev-list", "--count", f"{base_branch}..{integration_branch}", check=False)
    if result.returncode:
        raise GitError(f"Could not count commits since {base_branch!r}: {result.stderr.strip()}")
    return int(result.stdout.strip())


def revert_commit(repo: Path, integration_branch: str, commit_sha: str) -> str:
    if current_branch(repo) != integration_branch or not is_clean(repo):
        raise SafetyError("Rollback requires a clean checkout on the configured integration branch")
    if git(repo, "merge-base", "--is-ancestor", commit_sha, "HEAD", check=False).returncode:
        raise SafetyError("The requested commit is not in the integration branch history")
    git(repo, "revert", "--no-edit", commit_sha)
    return head_sha(repo)
