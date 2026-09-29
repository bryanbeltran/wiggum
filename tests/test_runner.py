from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wiggum.config import get_project, load_configuration
from wiggum.runner import WiggumRunner
from wiggum.state import ProjectState


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]

prompt = sys.stdin.read()
scenario = os.environ.get("FAKE_CODEX_SCENARIO", "implement")
if "ROLE: INVESTIGATOR" in prompt:
    if scenario == "no_work":
        result = {
            "outcome": "no_work", "summary": "Inspected repository", "reason": "No candidate met the value threshold.",
            "risk": "green", "candidate": None, "candidates": [], "blockers": []
        }
    elif scenario == "council":
        scores = {
            "user_value": 80, "workflow_breadth": 70, "constitution_fit": 90, "confidence": 100,
            "existing_capability_leverage": 80, "low_maintenance_cost": 70,
            "low_regression_risk": 90, "time_to_value": 80
        }
        result = {
            "outcome": "council", "summary": "Two product opportunities are ready for human choice.",
            "reason": "The candidates combine existing workflows.", "risk": "green", "candidate": None,
            "candidates": [{
                "title": "Compare saved plans", "value": "Make schedule tradeoffs visible.",
                "evidence": "Saved plan and comparison screens exist.", "tradeoffs": "Adds a comparison view.",
                "risk": "green", "scores": scores, "preferred": True
            }], "blockers": []
        }
    elif scenario == "red":
        result = {
            "outcome": "implement", "summary": "Known migration issue", "reason": "A destructive migration is required.",
            "risk": "red", "candidate": {
                "title": "Destructive change", "problem": "Would remove data", "evidence": ["migration"],
                "acceptance_criteria": ["approval"], "value_score": 80, "confidence": 0.9
            }, "candidates": [], "blockers": []
        }
    else:
        result = {
            "outcome": "implement", "summary": "A reproducible issue was found", "reason": "Evidence supports a narrow fix.",
            "risk": "green", "candidate": {
                "title": "Record run marker", "problem": "The run marker is not written.", "evidence": ["observed missing marker"],
                "acceptance_criteria": ["marker exists after the change"], "value_score": 80, "confidence": 0.9
            }, "candidates": [], "blockers": []
        }
elif "ROLE: IMPLEMENTER" in prompt:
    Path("run-marker.txt").write_text("reviewed\n")
    result = {
        "result": "implemented", "summary": "Write the run marker.", "commit_subject": "fix: write run marker",
        "files_changed": ["run-marker.txt"], "tests_added": [], "tests_run": [], "limitations": [], "risk": "green"
    }
elif "ROLE: REVIEWER" in prompt:
    result = {"approved": True, "summary": "The narrow change meets its acceptance criteria.", "findings": [], "missing_checks": []}
else:
    result = {"summary": "No recurring issue found.", "patterns": [], "proposals": []}

Path(option("--output-last-message")).write_text(json.dumps(result))
print(json.dumps({"type": "thread.started", "thread_id": "fake-session-1"}))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "target"
        self.repo.mkdir()
        _git(self.repo, "init", "--initial-branch=main")
        _git(self.repo, "config", "user.name", "Wiggum Test")
        _git(self.repo, "config", "user.email", "wiggum@example.test")
        (self.repo / "README.md").write_text("example product\n")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "docs: create fixture")
        _git(self.repo, "switch", "-c", "feature/wiggum")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.state_dir = self.root / "state"
        (self.config_dir / "projects.toml").write_text(
            "[projects.demo]\n"
            f'path = "{self.repo}"\n'
            'integration_branch = "feature/wiggum"\n'
            'base_branch = "main"\n'
            'constitution = "PRODUCT.md"\n'
            f'state_dir = "{self.state_dir}"\n'
            'check_commands = [["python3", "-c", "print(1)"]]\n'
        )
        (self.config_dir / "policies.toml").write_text(
            '[runtime]\ndefault_cycles = 1\nlow_value_stop_after = 2\n'
            'batch_review_after_commits = 5\nreview_fix_rounds = 1\n'
            'agent_timeout_seconds = 30\ncheck_timeout_seconds = 10\n'
            'baseline_checks = true\n'
            '[model]\nid = "gpt-6-luna"\nreasoning_effort = "max"\n'
            '[feature]\ndefault_adventurousness = "product_minded"\n'
            'weights = { user_value = 0.5, confidence = 0.5 }\n'
            '[state]\n'
            f'root = "{self.root / "state-root"}"\n'
        )
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(FAKE_CODEX)
        self.fake_codex.chmod(0o755)
        self.config = load_configuration(self.config_dir)
        self.project = get_project(self.config, "demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reviewed_cycle_commits_and_fast_forwards_integration_branch(self) -> None:
        base_sha = _git(self.repo, "rev-parse", "main")
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "implement"}):
            result = WiggumRunner(self.config, str(self.fake_codex)).run(
                self.project, "harden", cycles=1
            )
        self.assertEqual(result["results"][0]["status"], "implemented")
        self.assertTrue((self.repo / "run-marker.txt").exists())
        self.assertEqual(_git(self.repo, "log", "-1", "--pretty=%s"), "fix: write run marker")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), base_sha)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        state = ProjectState(self.state_dir)
        self.assertEqual(state.iteration_records()[0]["status"], "implemented")
        backlog = state.backlog_path.read_text()
        self.assertIn('"status": "implemented"', backlog)

    def test_zero_change_cycle_preserves_no_work_rationale(self) -> None:
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "no_work"}):
            result = WiggumRunner(self.config, str(self.fake_codex)).run(
                self.project, "harden", cycles=1
            )
        cycle = result["results"][0]
        self.assertEqual(cycle["status"], "no_work")
        self.assertIn("No candidate met", cycle["reason"])
        self.assertEqual(_git(self.repo, "rev-list", "--count", "main..feature/wiggum"), "0")
        self.assertTrue((Path(cycle["evidence_dir"]) / "investigator.jsonl").is_file())
        self.assertTrue((Path(cycle["evidence_dir"]) / "cycle.json").is_file())

    def test_red_risk_is_deferred_without_calling_implementer(self) -> None:
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "red"}):
            result = WiggumRunner(self.config, str(self.fake_codex)).run(
                self.project, "harden", cycles=1
            )
        cycle = result["results"][0]
        self.assertEqual(cycle["status"], "deferred")
        self.assertFalse((Path(cycle["evidence_dir"]) / "implementer-1.jsonl").exists())
        self.assertEqual(_git(self.repo, "rev-list", "--count", "main..feature/wiggum"), "0")

    def test_unconfigured_final_validation_cannot_integrate_a_change(self) -> None:
        runner = WiggumRunner(self.config, str(self.fake_codex))
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "implement"}), patch.object(
            runner,
            "_run_checks",
            side_effect=[
                {"status": "passed", "commands": []},
                {"status": "not_configured", "commands": []},
            ],
        ):
            result = runner.run(self.project, "harden", cycles=1)
        cycle = result["results"][0]
        self.assertEqual(cycle["status"], "validation_not_configured")
        self.assertIn("refusing to integrate", cycle["reason"])
        self.assertEqual(_git(self.repo, "rev-list", "--count", "main..feature/wiggum"), "0")
        self.assertFalse((self.repo / "run-marker.txt").exists())

    def test_council_mode_computes_weighted_score_and_does_not_build(self) -> None:
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "council"}):
            result = WiggumRunner(self.config, str(self.fake_codex)).run(
                self.project, "feature", cycles=1, selection="council"
            )
        cycle = result["results"][0]
        self.assertEqual(cycle["status"], "council")
        self.assertEqual(cycle["candidates"][0]["weighted_score"], 90.0)
        self.assertTrue((Path(cycle["evidence_dir"]) / "council.md").is_file())
        self.assertFalse((Path(cycle["evidence_dir"]) / "implementer-1.jsonl").exists())
        self.assertEqual(_git(self.repo, "rev-list", "--count", "main..feature/wiggum"), "0")

    def test_bugfix_requires_a_concrete_objective(self) -> None:
        with self.assertRaisesRegex(Exception, "requires an objective"):
            WiggumRunner(self.config, str(self.fake_codex)).run(
                self.project, "bugfix", cycles=1
            )


if __name__ == "__main__":
    unittest.main()
