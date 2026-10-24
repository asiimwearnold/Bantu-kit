import json
import tempfile
import unittest
from pathlib import Path

from bantu.agent import AgentResult, run_subagent
from bantu.config import ROOT, load_project, project_dir
from bantu.llm import Router
from bantu.mock import mock_reply
from bantu.orchestrator import _review_outcome
from bantu.tools import Workspace


class ProjectPathTests(unittest.TestCase):
    def test_known_project_resolves_under_projects(self):
        self.assertEqual(project_dir("demo"), (ROOT / "projects" / "demo").resolve())

    def test_rejects_traversal_and_absolute_names(self):
        for name in ("..", "../bantu", str(ROOT), "demo/subproject"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    project_dir(name)

    def test_load_project_reports_invalid_name(self):
        with self.assertRaises(SystemExit):
            load_project("..")


class ProviderConfigTests(unittest.TestCase):
    def test_deepseek_is_second_in_both_model_pools(self):
        config = json.loads((ROOT / "bantu.config.json").read_text(encoding="utf-8"))
        provider = config["providers"]["deepseek"]
        self.assertEqual(provider["base_url"], "https://api.deepseek.com")
        self.assertEqual(provider["key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(config["pools"]["smart"][1], "deepseek/deepseek-flash")
        self.assertEqual(config["pools"]["fast"][1], "deepseek/deepseek-flash")


class UsageStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_dir = Path(self.tempdir.name)
        self.router = Router({"budgets": {}}, self.state_dir, mock=True)

    def test_corrupt_state_fails_closed(self):
        (self.state_dir / "usage.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "cannot read usage state"):
            self.router._load()

    def test_invalid_counters_fail_closed(self):
        (self.state_dir / "usage.json").write_text(
            json.dumps({"date": "2026-10-24", "total": -1, "providers": {}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid usage counters"):
            self.router._load()

    def test_invalid_date_fails_closed(self):
        (self.state_dir / "usage.json").write_text(
            json.dumps({"date": "not-a-date", "total": 0, "providers": {}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid usage date"):
            self.router._load()

    def test_save_replaces_state_without_leaving_temporary_files(self):
        self.router.state["total"] = 25
        self.router._save()
        self.assertEqual(json.loads((self.state_dir / "usage.json").read_text())["total"], 25)
        self.assertEqual(list(self.state_dir.glob("usage.json.*.tmp")), [])

    def test_missing_state_after_initial_load_fails_closed(self):
        self.router._save()
        (self.state_dir / "usage.json").unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            self.router._load()


class ReviewGateTests(unittest.TestCase):
    def test_only_a_written_pass_verdict_succeeds(self):
        result = AgentResult("done", "reviewed", {"REVIEW.md"})
        self.assertEqual(_review_outcome(result, "Verdict: pass\n"), (True, "review passed"))

    def test_needs_work_verdict_fails(self):
        result = AgentResult("done", "reviewed", {"REVIEW.md"})
        passed, note = _review_outcome(result, "Verdict: needs work\n")
        self.assertFalse(passed)
        self.assertIn("Verdict: pass", note)

    def test_missing_or_unfinished_review_fails(self):
        missing = AgentResult("done", "reviewed", set())
        unfinished = AgentResult("stuck", "no valid output", {"REVIEW.md"})
        self.assertFalse(_review_outcome(missing, None)[0])
        self.assertFalse(_review_outcome(unfinished, "Verdict: pass\n")[0])

    def test_mock_reviewer_emits_a_passing_verdict(self):
        response = mock_reply("reviewer", [{"role": "user", "content": "review"}])
        self.assertTrue(response.startswith("@@write REVIEW.md\n"))
        self.assertIn("Verdict: pass", response)

    def test_mock_reviewer_passes_the_review_gate_end_to_end(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            router = Router(
                {"budgets": {"daily_tokens_total": 1000, "max_output_tokens": 1000}},
                root / "state",
                mock=True,
            )
            workspace = Workspace(root / "project", {"sandbox": {}})
            result = run_subagent(router, workspace, "reviewer", "", "Review the changes", [], 2, 1000)
            self.assertTrue(_review_outcome(result, workspace.read("REVIEW.md"))[0])


if __name__ == "__main__":
    unittest.main()
