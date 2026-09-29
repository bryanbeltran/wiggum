from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from wiggum.runner import self_analysis_eligibility
from wiggum.security import redact_text
from wiggum.state import ProjectState, project_lock, read_json, write_private_text


class StateTests(unittest.TestCase):
    def test_redacts_common_secret_formats(self) -> None:
        source = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuv Authorization: Bearer abc.def.ghi"
        result = redact_text(source)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", result)
        self.assertNotIn("abc.def.ghi", result)
        self.assertIn("[REDACTED]", result)

    def test_private_state_writes_restrict_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state" / "note.txt"
            write_private_text(path, "token=ghp_abcdefghijklmnopqrstuvwxyz1234567890")
            self.assertEqual(path.read_text(), "token=[REDACTED_TOKEN]")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_control_requests_and_iteration_journal_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = ProjectState(Path(temp) / "state")
            state.request_control("pivot", "polish")
            self.assertEqual(state.read_control()["mode"], "polish")
            state.append_iteration({"cycle_id": "cycle-1", "status": "implemented"})
            self.assertEqual(state.iteration_records()[0]["cycle_id"], "cycle-1")
            state.clear_control()
            self.assertIsNone(state.read_control())
            self.assertIsInstance(json.loads(state.iterations_path.read_text().strip()), dict)

    def test_run_lock_rejects_a_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            with project_lock(root):
                with self.assertRaisesRegex(RuntimeError, "already holds the lock"):
                    with project_lock(root):
                        pass

    def test_self_analysis_uses_minimum_evidence_and_periodic_cadence(self) -> None:
        policy = {"minimum_cycles_before_analysis": 30, "periodic_every_new_cycles": 50}
        cycles = [{"status": "no_work"} for _ in range(30)]
        self.assertTrue(self_analysis_eligibility(cycles, policy, 0)[0])
        self.assertFalse(self_analysis_eligibility(cycles, policy, 1)[0])
        self.assertFalse(self_analysis_eligibility(cycles[:29], policy, 0)[0])


if __name__ == "__main__":
    unittest.main()
