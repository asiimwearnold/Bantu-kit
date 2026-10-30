import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from bantu.agent import AgentResult, run_subagent
from bantu.config import ROOT, ensure_project, load_project, project_dir
from bantu.llm import OutOfBudget, Router
from bantu.mock import mock_reply
from bantu.daemon import _daemon_lock
from bantu.orchestrator import (
    SubResult,
    _create_pull_request,
    _project_config,
    _refresh_branch,
    _restore_subtask_checkpoint,
    _review_outcome,
    _should_push_task_branch,
    parse_task,
    run_task,
)
from bantu.tools import Workspace
from bantu.worker import CrmWorkerClient, _run_claimed_task, validate_repository_url


class ProjectPathTests(unittest.TestCase):
    def test_known_project_resolves_under_projects(self):
        with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": ""}):
            self.assertEqual(project_dir("demo"), (ROOT / "projects" / "demo").resolve())

    def test_rejects_traversal_and_absolute_names(self):
        with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": ""}):
            for name in ("..", "../bantu", str(ROOT), "demo/subproject"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        project_dir(name)

    def test_uses_external_projects_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            projects = Path(tempdir).resolve()
            (projects / "sente").mkdir()
            with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": str(projects)}):
                self.assertEqual(project_dir("sente"), projects / "sente")
                with self.assertRaises(ValueError):
                    project_dir("..")

    def test_external_project_cannot_override_host_sandbox_policy(self):
        cfg = {
            "sandbox": {
                "mode": "docker",
                "image": "bantu-sandbox",
                "network": "none",
                "timeout_sec": 120,
                "allow": ["python"],
            }
        }
        meta = {
            "sandbox": {
                "mode": "local",
                "image": "untrusted-image",
                "network": "host",
                "allow": ["powershell"],
            }
        }
        project_cfg = _project_config(cfg, meta, external_projects=True)
        self.assertEqual(project_cfg["sandbox"], cfg["sandbox"])
        self.assertEqual(cfg["sandbox"]["mode"], "docker")

    def test_bundled_project_can_select_its_trusted_sandbox_profile(self):
        cfg = {"sandbox": {"mode": "docker", "image": "bantu-sandbox", "allow": ["python"]}}
        meta = {"sandbox": {"image": "bantu-node", "allow": ["node", "npm"]}}
        project_cfg = _project_config(cfg, meta, external_projects=False)
        self.assertEqual(
            project_cfg["sandbox"],
            {"mode": "docker", "image": "bantu-node", "allow": ["node", "npm"]},
        )
        self.assertEqual(cfg["sandbox"]["image"], "bantu-sandbox")

    def test_rejects_credentials_embedded_in_repository_url(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": tempdir}):
                with self.assertRaises(SystemExit) as raised:
                    ensure_project("sente", "https://user:secret@example.com/sente.git")
        self.assertNotIn("secret", str(raised.exception))

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


class CrmWorkerTests(unittest.TestCase):
    def test_rejects_non_https_and_credentialed_repository_urls(self):
        for value in (
            "ssh://git@github.com/team/project.git",
            "file:///C:/private/repo",
            "https://user:password@example.com/project.git",
            "https://localhost/project.git",
            "https://127.0.0.1/project.git",
            "https://example.com:8443/project.git",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_repository_url(value)

    def test_client_requires_long_token_and_local_http(self):
        with self.assertRaisesRegex(ValueError, "at least 32"):
            CrmWorkerClient("http://localhost:3001", "short", "worker-1")
        with self.assertRaisesRegex(ValueError, "HTTPS outside localhost"):
            CrmWorkerClient("http://crm.example.com", "x" * 32, "worker-1")

    def test_repository_host_must_resolve_only_to_public_addresses(self):
        with patch(
            "bantu.worker.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("10.0.0.8", 443)),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "private or reserved"):
                validate_repository_url("https://git.example.com/team/project.git")

    def test_claim_rejects_malformed_task_data(self):
        client = CrmWorkerClient("http://localhost:3001", "x" * 32, "worker-1")
        with patch.object(client, "request", return_value={"task": {"id": "../bad"}}):
            with self.assertRaisesRegex(RuntimeError, "malformed task"):
                client.claim()

    def test_completion_keeps_the_configured_worker_identity(self):
        client = CrmWorkerClient("http://localhost:3001", "x" * 32, "worker-1")
        with patch.object(client, "request") as request:
            client.complete("task-1", {"workerId": "other-worker", "status": "FAILED"})
        request.assert_called_once_with(
            "tasks/task-1/complete",
            {"status": "FAILED", "workerId": "worker-1"},
        )

    def test_task_exception_is_reported_as_failed(self):
        class Client:
            result = None

            def heartbeat(self, _task_id):
                return None

            def complete(self, _task_id, payload):
                self.result = payload

        client = Client()
        task = {
            "id": "task-1",
            "repository": "https://github.com/team/project.git",
            "request": "Make a focused change and run the tests.",
        }
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("bantu.worker.ROOT", root),
                patch("bantu.worker.run_task", side_effect=RuntimeError("test failure")),
            ):
                _run_claimed_task(client, {}, task, log=lambda _line: None)
            self.assertEqual(client.result["status"], "FAILED")
            self.assertEqual(client.result["failure"], "test failure")
            self.assertTrue((root / "tasks" / "failed" / "crm-task-1.md").is_file())


class TaskCheckpointTests(unittest.TestCase):
    def test_rejects_completed_results_not_in_saved_plan(self):
        with self.assertRaisesRegex(RuntimeError, "completed subtask result is malformed"):
            _restore_subtask_checkpoint(
                {
                    "subtasks": [{"id": "s1", "goal": "Update README."}],
                    "completed_subtasks": [{"id": "s2", "role": "coder"}],
                },
                "coder",
            )

    def test_rejects_malformed_saved_subtask_fields(self):
        with self.assertRaisesRegex(RuntimeError, "subtask 1 is malformed"):
            _restore_subtask_checkpoint(
                {"subtasks": [{"id": "s1", "role": 7, "goal": "Update README."}]},
                "coder",
            )


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
    def test_task_branch_push_requires_completed_passing_review(self):
        self.assertTrue(_should_push_task_branch(True, "done", True, False))
        self.assertFalse(_should_push_task_branch(True, "done", False, False))
        self.assertFalse(_should_push_task_branch(True, "done", True, True))
        self.assertFalse(_should_push_task_branch(True, "failed", True, False))
        self.assertFalse(_should_push_task_branch(False, "done", True, False))

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

    def test_pull_request_creation_uses_github_cli_without_merging(self):
        with patch("bantu.orchestrator.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["gh"], returncode=0, stdout="https://github.com/team/project/pull/1\n", stderr=""
            )
            code, output = _create_pull_request(
                Path("repo"), "main", "bantu/task", "BANTU: documentation quickstart"
            )
        self.assertEqual(code, 0)
        self.assertIn("/pull/1", output)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["gh", "pr", "create"])
        self.assertIn("--base", args)
        self.assertIn("main", args)
        self.assertNotIn("merge", args)

    def test_repository_branch_fast_forwards_to_fetched_origin(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for upstream refresh tests")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            seed = root / "seed"
            seed.mkdir()

            def git(cwd, *args):
                return subprocess.run(
                    ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
                )

            git(seed, "init", "-b", "main")
            git(seed, "config", "user.name", "BANTU Test")
            git(seed, "config", "user.email", "bantu-test@example.invalid")
            (seed / "version.txt").write_text("one\n", encoding="utf-8")
            git(seed, "add", "-A")
            git(seed, "commit", "-m", "Initial")
            remote = root / "origin.git"
            git(root, "clone", "--bare", str(seed), str(remote))
            local = root / "local"
            git(root, "clone", str(remote), str(local))

            updater = root / "updater"
            git(root, "clone", str(remote), str(updater))
            git(updater, "config", "user.name", "BANTU Test")
            git(updater, "config", "user.email", "bantu-test@example.invalid")
            (updater / "version.txt").write_text("two\n", encoding="utf-8")
            git(updater, "commit", "-am", "Update upstream")
            git(updater, "push", "origin", "main")

            refreshed, note = _refresh_branch(local, "main")
            self.assertTrue(refreshed, note)
            self.assertEqual((local / "version.txt").read_text(encoding="utf-8"), "two\n")


class SandboxIsolationTests(unittest.TestCase):
    def test_run_uses_a_temporary_copy_without_git_or_env(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            (root / ".env").write_text("TEST_SECRET=value\n", encoding="utf-8")
            (root / ".env.local").write_text("ANOTHER_SECRET=value\n", encoding="utf-8")
            (root / "probe.py").write_text(
                "from pathlib import Path\n"
                "assert not Path('.git').exists()\n"
                "assert not Path('.env').exists()\n"
                "assert not Path('.env.local').exists()\n"
                "Path('sandbox-marker').write_text('isolated')\n",
                encoding="utf-8",
            )
            workspace = Workspace(
                root,
                {"sandbox": {"mode": "local", "allow": ["python"], "timeout_sec": 10}},
            )
            code, output = workspace.run("python probe.py")
            self.assertEqual((code, output), (0, "(no output)"))
            self.assertFalse((root / "sandbox-marker").exists())


class DaemonLockTests(unittest.TestCase):
    def test_queue_lock_rejects_a_second_daemon_instance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            lock = Path(tempdir) / ".daemon.lock"
            with _daemon_lock(lock):
                with self.assertRaisesRegex(RuntimeError, "another BANTU daemon"):
                    with _daemon_lock(lock):
                        pass


class ExternalRepositoryWorkflowTests(unittest.TestCase):
    def test_paused_reviewer_does_not_push_task_branch(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for the external repository workflow test")

        class PausedReviewerRouter:
            def __init__(self, cfg, state_dir, mock=False):
                self.by_model = {}
                self.task_tokens = 0
                self.subtasks = 0

            def start_task(self, limit, log_path):
                pass

            def begin_sub(self, limit):
                pass

            def chat(self, role, messages, max_tokens=None):
                if role == "reviewer":
                    raise OutOfBudget("review provider unavailable")
                raise AssertionError(f"unexpected provider call for role {role}")

        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            projects = temp_root / "projects"
            project = projects / "demo-external"
            shutil.copytree(ROOT / "projects" / "demo", project)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=project,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "-b", "main")
            git("config", "user.name", "BANTU Test")
            git("config", "user.email", "bantu-test@example.invalid")
            git("add", "-A")
            git("commit", "-m", "Initial external project")
            remote = temp_root / "origin.git"
            subprocess.run(
                ["git", "clone", "--bare", str(project), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            git("remote", "add", "origin", str(remote))

            task = temp_root / "paused-review-task.md"
            task.write_text(
                "project: demo-external\npush: true\n---\nUpdate the project README.\n\n"
                "```subtasks\n"
                '[{"id":"s1","role":"coder","goal":"Update README.","read":["README.md"],'
                '"acceptance":"true"}]\n'
                "```\n",
                encoding="utf-8",
            )
            bantu_home = temp_root / "bantu-home"
            cfg = {
                "budgets": {
                    "task_tokens": 10000,
                    "subtask_tokens": 5000,
                    "max_steps": 3,
                    "max_fix_attempts": 1,
                    "max_context_chars": 10000,
                    "daily_tokens_total": 20000,
                },
                "sandbox": {"mode": "local", "allow": ["python"], "timeout_sec": 30},
                "git": {"mode": "branch"},
                "review": True,
            }

            def write_task_change(router, ws, cfg, subtask, default_accept, log):
                (project / "README.md").write_text("Updated README.\n", encoding="utf-8")
                return SubResult("s1", "coder", True, "README updated", 1, {"README.md"})

            with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": str(projects)}):
                with patch("bantu.orchestrator.Router", PausedReviewerRouter):
                    with patch("bantu.orchestrator._run_sub", side_effect=write_task_change):
                        with patch("bantu.orchestrator.ROOT", bantu_home):
                            with patch("bantu.orchestrator._role_prompt", return_value="test role"):
                                logs = []
                                try:
                                    run_task(cfg, task, echo=logs.append)
                                except OutOfBudget:
                                    pass
                                else:
                                    self.fail("\n".join(logs))

            pushed_branches = subprocess.run(
                ["git", "--git-dir", str(remote), "branch", "--list", "bantu/*"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(pushed_branches, [])
            self.assertEqual(git("branch", "--show-current").stdout.strip(), "main")

    def test_paused_task_resumes_its_branch_and_pushes_after_review(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for the external repository workflow test")

        class Router:
            def __init__(self, fail_review, cfg, state_dir, mock=False):
                self.by_model = {}
                self.task_tokens = 0
                self.fail_review = fail_review

            def start_task(self, limit, log_path):
                pass

            def begin_sub(self, limit):
                pass

            def chat(self, role, messages, max_tokens=None):
                if role != "reviewer":
                    raise AssertionError(f"unexpected provider call for role {role}")
                if self.fail_review:
                    raise OutOfBudget("temporary provider outage")
                return mock_reply(role, messages)

        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            projects = temp_root / "projects"
            project = projects / "demo-resume"
            shutil.copytree(ROOT / "projects" / "demo", project)

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=project, check=True, capture_output=True, text=True
                )

            git("init", "-b", "main")
            git("config", "user.name", "BANTU Test")
            git("config", "user.email", "bantu-test@example.invalid")
            git("add", "-A")
            git("commit", "-m", "Initial project")
            remote = temp_root / "origin.git"
            subprocess.run(
                ["git", "clone", "--bare", str(project), str(remote)],
                check=True, capture_output=True, text=True,
            )
            git("remote", "add", "origin", str(remote))

            task = temp_root / "resume-task.md"
            task.write_text(
                "project: demo-resume\npush: true\n---\nUpdate the project README.\n\n"
                "```subtasks\n"
                '[{"id":"s1","role":"coder","goal":"Update README.","read":["README.md"],'
                '"acceptance":"true"}]\n'
                "```\n",
                encoding="utf-8",
            )
            bantu_home = temp_root / "bantu-home"
            cfg = {
                "budgets": {
                    "task_tokens": 10000,
                    "subtask_tokens": 5000,
                    "max_steps": 3,
                    "max_fix_attempts": 1,
                    "max_context_chars": 10000,
                    "daily_tokens_total": 20000,
                },
                "sandbox": {"mode": "local", "allow": ["python"], "timeout_sec": 30},
                "git": {"mode": "branch"},
                "review": True,
            }

            def write_task_change(router, ws, cfg, subtask, default_accept, log):
                coder_calls.append(subtask["id"])
                (project / "README.md").write_text("Updated README.\n", encoding="utf-8")
                return SubResult("s1", "coder", True, "README updated", 1, {"README.md"})

            coder_calls = []
            with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": str(projects)}):
                with patch("bantu.orchestrator.Router", side_effect=lambda *a, **kw: Router(True, *a, **kw)):
                    with patch("bantu.orchestrator._run_sub", side_effect=write_task_change):
                        with patch("bantu.orchestrator.ROOT", bantu_home):
                            with patch("bantu.orchestrator._role_prompt", return_value="test role"):
                                logs = []
                                try:
                                    run_task(cfg, task, echo=logs.append)
                                except OutOfBudget:
                                    pass
                                else:
                                    self.fail("\n".join(logs))
                paused_branch = git("branch", "--list", "bantu/*").stdout.strip().removeprefix("* ").strip()
                self.assertEqual(git("branch", "--show-current").stdout.strip(), "main")
                git("checkout", paused_branch)
                (project / "crash-checkpoint.txt").write_text(
                    "Recovered after interrupted task.\n", encoding="utf-8"
                )
                with patch("bantu.orchestrator.Router", side_effect=lambda *a, **kw: Router(False, *a, **kw)):
                    with patch("bantu.orchestrator._run_sub", side_effect=write_task_change):
                        with patch("bantu.orchestrator.ROOT", bantu_home):
                            with patch("bantu.orchestrator._role_prompt", return_value="test role"):
                                self.assertEqual(run_task(cfg, task, echo=lambda _: None), "done")
            self.assertEqual(coder_calls, ["s1"])

            remote_branches = subprocess.run(
                ["git", "--git-dir", str(remote), "branch", "--list", "bantu/*"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertEqual(len(remote_branches), 1)
            self.assertIn(paused_branch, remote_branches[0])
            checkpoint = subprocess.run(
                ["git", "--git-dir", str(remote), "show", f"{paused_branch}:crash-checkpoint.txt"],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn("Recovered after interrupted task.", checkpoint)
            self.assertEqual(git("branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(list((bantu_home / "state" / "task_runs").glob("*.json")), [])

    def test_mock_task_branches_and_commits_in_external_project_only(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for the external repository workflow test")

        class ScriptedRouter:
            def __init__(self, cfg, state_dir, mock=False):
                self.by_model = {}
                self.task_tokens = 0

            def start_task(self, limit, log_path):
                self.limit = limit

            def begin_sub(self, limit):
                pass

            def chat(self, role, messages, max_tokens=None):
                response = mock_reply(role, messages)
                tokens = max(1, (sum(len(m["content"]) for m in messages) + len(response)) // 4)
                self.task_tokens += tokens
                self.by_model["mock/mock"] = self.by_model.get("mock/mock", 0) + tokens
                return response

        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            projects = temp_root / "product-repositories"
            project = projects / "demo-external"
            project.parent.mkdir()
            shutil.copytree(ROOT / "projects" / "demo", project)
            (project / "src" / "demo" / "__init__.py").unlink(missing_ok=True)
            (project / "src" / "demo" / "greet.py").write_text(
                "def greet(name):\n    return name\n",
                encoding="utf-8",
            )
            (project / "tests" / "test_greet.py").write_text(
                "import unittest\n"
                "from src.demo.greet import greet\n\n"
                "class GreetTests(unittest.TestCase):\n"
                "    def test_greet(self):\n"
                "        self.assertEqual(greet('Ada'), 'Hello, Ada!')\n",
                encoding="utf-8",
            )
            (project / "bantu.project.json").write_text(
                json.dumps({
                    "test_cmd": "python -m unittest discover -s tests",
                    "protected": ["tests/", "bantu.project.json", "SPEC.md"],
                }),
                encoding="utf-8",
            )
            (project / "SPEC.md").write_text("Demo project test specification.\n", encoding="utf-8")

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=project,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init")
            git("config", "user.name", "BANTU Test")
            git("config", "user.email", "bantu-test@example.invalid")
            git("add", "-A")
            git("commit", "-m", "Initial external project")
            original_branch = git("branch", "--show-current").stdout.strip()
            remote = temp_root / "sente-origin.git"
            subprocess.run(
                ["git", "clone", "--bare", str(project), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            original_remote_commit = subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{original_branch}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            def remove_readonly(func, path, exc_info):
                os.chmod(path, stat.S_IWRITE)
                func(path)

            shutil.rmtree(project, onerror=remove_readonly)

            task = temp_root / "external-demo-task.md"
            task.write_text(
                f"project: demo-external\nrepository: {remote.as_uri()}\npush: true\nbudget_tokens: 10000\n---\n"
                "Implement the greeting function.\n\n"
                "```subtasks\n"
                '[{"id":"s1","role":"coder","goal":"Implement greet().",'
                '"read":["tests/test_greet.py"],'
                '"acceptance":"python -m unittest discover -s tests"}]\n'
                "```\n",
                encoding="utf-8",
            )
            bantu_home = temp_root / "bantu-home"
            cfg = {
                "budgets": {
                    "task_tokens": 10000,
                    "subtask_tokens": 5000,
                    "max_steps": 3,
                    "max_fix_attempts": 1,
                    "max_context_chars": 10000,
                    "daily_tokens_total": 20000,
                },
                "sandbox": {"mode": "local", "allow": ["python"], "timeout_sec": 30},
                "git": {"mode": "branch"},
                "review": True,
            }
            with patch.dict(os.environ, {"BANTU_PROJECTS_ROOT": str(projects)}):
                with patch("bantu.orchestrator.Router", ScriptedRouter):
                    with patch("bantu.orchestrator.ROOT", bantu_home):
                        with patch("bantu.orchestrator._role_prompt", return_value="test role"):
                            result = run_task(cfg, task, echo=lambda _: None)

            self.assertEqual(result, "done")
            self.assertEqual(git("branch", "--show-current").stdout.strip(), original_branch)
            self.assertEqual(git("status", "--porcelain").stdout.strip(), "")
            branches = git("branch", "--list", "bantu/*").stdout.splitlines()
            self.assertEqual(len(branches), 1)
            branch_name = branches[0].strip().lstrip("* ").strip()
            changed = git("diff", "--name-only", f"{original_branch}..{branch_name}").stdout.splitlines()
            self.assertIn("src/demo/greet.py", changed)
            self.assertIn("REVIEW.md", changed)
            pushed_branches = subprocess.run(
                ["git", "--git-dir", str(remote), "branch", "--list", "bantu/*"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(len(pushed_branches), 1)
            remote_branch = pushed_branches[0].strip().lstrip("* ").strip()
            remote_greet = subprocess.run(
                ["git", "--git-dir", str(remote), "show", f"{remote_branch}:src/demo/greet.py"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("Hello, ", remote_greet)
            current_remote_main = subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{original_branch}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(current_remote_main, original_remote_commit)
            self.assertFalse((ROOT / "projects" / "demo-external").exists())

    def test_add_command_writes_repository_and_push_headers(self):
        from bantu.__main__ import cmd_add

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            args = Namespace(
                text="Review the repository",
                project="sente",
                repository="https://example.com/team/sente.git",
                push=True,
                pr=True,
                budget=12000,
            )
            with patch("bantu.__main__.ROOT", root):
                cmd_add(args, {})
            task = next((root / "tasks" / "inbox").glob("*.md"))
            header, _ = parse_task(task.read_text(encoding="utf-8"))
            self.assertEqual(header["project"], "sente")
            self.assertEqual(header["repository"], args.repository)
            self.assertEqual(header["push"], "true")
            self.assertEqual(header["pr"], "true")
            self.assertEqual(header["budget_tokens"], "12000")


if __name__ == "__main__":
    unittest.main()
