from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wiggum.config import ConfigurationError, get_project, load_configuration


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "private-state"
        (self.config_dir / "policies.toml").write_text(
            '[model]\nid = "gpt-6-luna"\nreasoning_effort = "max"\n'
            '[runtime]\ndefault_cycles = 1\nlow_value_stop_after = 5\n'
            '[feature]\ndefault_adventurousness = "product_minded"\n'
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_projects(self, integration_branch: str = "feature/wiggum") -> None:
        (self.config_dir / "projects.toml").write_text(
            "[projects.demo]\n"
            f'path = "{self.repo}"\n'
            f'integration_branch = "{integration_branch}"\n'
            'base_branch = "main"\n'
            f'state_dir = "{self.state}"\n'
            'check_commands = [["python3", "-c", "print(1)"]]\n'
        )

    def test_default_model_uses_lowest_cost_max_effort_setting(self) -> None:
        self.write_projects()
        config = load_configuration(self.config_dir)
        self.assertEqual(config.policy.model, "gpt-6-luna")
        self.assertEqual(config.policy.reasoning_effort, "max")

    def test_project_paths_are_resolved_and_git_commands_are_argv(self) -> None:
        self.write_projects()
        project = get_project(load_configuration(self.config_dir), "demo")
        self.assertEqual(project.repo_path, self.repo.resolve())
        self.assertEqual(project.check_commands, (("python3", "-c", "print(1)"),))

    def test_project_without_check_commands_is_rejected(self) -> None:
        self.write_projects()
        projects_path = self.config_dir / "projects.toml"
        projects_path.write_text(
            projects_path.read_text().replace(
                'check_commands = [["python3", "-c", "print(1)"]]\n', ""
            )
        )
        with self.assertRaisesRegex(
            ConfigurationError, "cannot integrate unvalidated changes"
        ):
            load_configuration(self.config_dir)

    def test_integration_branch_cannot_equal_protected_base(self) -> None:
        self.write_projects("main")
        config = load_configuration(self.config_dir)
        with self.assertRaises(ConfigurationError):
            get_project(config, "demo")

    def test_missing_projects_file_gives_setup_guidance(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "projects.example.toml"):
            load_configuration(self.config_dir)


if __name__ == "__main__":
    unittest.main()
