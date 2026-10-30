"""Run one task file: (plan) -> sub-agents with acceptance checks -> review -> report -> git branch."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .agent import AgentResult, run_subagent
from .config import ROOT, ensure_project, load_project, projects_root
from .llm import BudgetExceeded, OutOfBudget, ProviderError, Router
from .tools import ToolError, Workspace, truncate


@dataclass
class SubResult:
    id: str
    role: str
    ok: bool
    note: str
    attempts: int = 0
    files: set[str] = field(default_factory=set)


def parse_task(text: str) -> tuple[dict, str]:
    head, body = {}, text
    if "\n---" in text:
        raw, body = text.split("\n---", 1)
        body = body.lstrip("-").lstrip("\n")
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                head[k.strip().lower()] = v.strip()
    return head, body


def parse_subtasks(body: str) -> list[dict] | None:
    m = re.search(r"```subtasks\s*\n(.*?)```", body, re.S)
    return json.loads(m.group(1)) if m else None


def _role_prompt(role: str) -> str:
    path = ROOT / "roles" / f"{role}.md"
    if not path.exists():
        path = ROOT / "roles" / "coder.md"
    return path.read_text(encoding="utf-8")


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _git_root(path: Path) -> Path | None:
    code, output = _git(path, "rev-parse", "--show-toplevel")
    if code:
        return None
    return Path(output).resolve()


def _task_state_path(task_path: Path, project: str) -> Path:
    identity = f"{project}\0{task_path.name}".encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()
    return ROOT / "state" / "task_runs" / f"{key}.json"


def _project_config(cfg: dict, meta: dict, external_projects: bool) -> dict:
    sandbox = dict(cfg["sandbox"])
    if not external_projects:
        sandbox.update(meta.get("sandbox", {}))
    return {**cfg, "sandbox": sandbox}


def _load_task_state(path: Path, repo_root: Path | None) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read task resume state {path}: {exc}") from exc
    if (
        not isinstance(state, dict)
        or state.get("status") != "paused"
        or not isinstance(state.get("branch"), str)
        or not state["branch"].startswith("bantu/")
        or not isinstance(state.get("original"), str)
        or not state["original"]
        or state["original"].startswith("-")
        or not isinstance(state.get("repo_root"), str)
    ):
        raise RuntimeError(f"invalid task resume state in {path}")
    if repo_root is None or Path(state["repo_root"]).resolve() != repo_root.resolve():
        raise RuntimeError(f"task resume state belongs to a different project repository: {path}")
    return state


def _save_task_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_subtask_checkpoint(
    state: dict, default_role: str
) -> tuple[list[dict] | None, dict[str, dict]]:
    subtasks = state.get("subtasks")
    if subtasks is not None:
        if not isinstance(subtasks, list):
            raise RuntimeError("invalid task resume state: subtasks must be a list")
        seen_ids = set()
        for index, subtask in enumerate(subtasks, start=1):
            if not isinstance(subtask, dict) or not isinstance(subtask.get("goal"), str):
                raise RuntimeError(f"invalid task resume state: subtask {index} is malformed")
            subtask_id = subtask.get("id", f"s{index}")
            if not isinstance(subtask_id, str) or not subtask_id:
                raise RuntimeError(f"invalid task resume state: subtask {index} has an invalid id")
            role = subtask.get("role", default_role)
            read_files = subtask.get("read", [])
            acceptance = subtask.get("acceptance", "")
            if (
                not isinstance(role, str)
                or not role
                or not isinstance(read_files, list)
                or any(not isinstance(path, str) for path in read_files)
                or not isinstance(acceptance, str)
            ):
                raise RuntimeError(f"invalid task resume state: subtask {index} is malformed")
            if subtask_id in seen_ids:
                raise RuntimeError(f"invalid task resume state: duplicate subtask id {subtask_id}")
            seen_ids.add(subtask_id)
            subtask["id"] = subtask_id
            subtask["role"] = role
    completed = state.get("completed_subtasks", [])
    if not isinstance(completed, list):
        raise RuntimeError("invalid task resume state: completed_subtasks must be a list")
    known_ids = {subtask["id"] for subtask in subtasks or []}
    completed_by_id = {}
    for item in completed:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] not in known_ids
            or not isinstance(item.get("role", default_role), str)
            or not isinstance(item.get("note", ""), str)
            or not isinstance(item.get("attempts", 1), int)
            or isinstance(item.get("attempts", 1), bool)
            or item.get("attempts", 1) < 1
            or not isinstance(item.get("files", []), list)
            or any(not isinstance(path, str) for path in item.get("files", []))
        ):
            raise RuntimeError("invalid task resume state: completed subtask result is malformed")
        if item["id"] in completed_by_id:
            raise RuntimeError(f"invalid task resume state: duplicate completed subtask {item['id']}")
        completed_by_id[item["id"]] = {
            "id": item["id"],
            "role": item.get("role", default_role),
            "note": item.get("note", ""),
            "attempts": item.get("attempts", 1),
            "files": item.get("files", []),
        }
    return subtasks, completed_by_id


def _refresh_branch(branch_root: Path, branch: str) -> tuple[bool, str]:
    code, remote = _git(branch_root, "remote", "get-url", "origin")
    if code:
        return True, ""
    upstream_code, upstream = _git(
        branch_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"
    )
    if not upstream_code and not upstream.startswith("origin/"):
        return False, f"{branch} tracks {upstream}, not origin; configure an origin tracking branch first"
    code, output = _git(branch_root, "fetch", "origin")
    if code:
        return False, f"could not fetch origin: {output}"
    if upstream_code:
        upstream = f"refs/remotes/origin/{branch}"
        code, output = _git(branch_root, "show-ref", "--verify", "--quiet", upstream)
        if code:
            return False, f"origin has no tracking branch for {branch}; checkout a tracked base branch first"
    code, output = _git(branch_root, "merge", "--ff-only", upstream)
    if code:
        return False, f"cannot fast-forward {branch} to {upstream}: {output}"
    return True, ""


def _branch_has_commits(branch_root: Path, base: str, branch: str) -> bool:
    code, output = _git(branch_root, "rev-list", "--count", f"{base}..{branch}")
    return code == 0 and output.isdigit() and int(output) > 0


def _create_pull_request(branch_root: Path, base: str, branch: str, title: str) -> tuple[int, str]:
    body = (
        "Prepared by BANTU after the configured acceptance checks and reviewer gate passed. "
        "Please inspect the diff and CI results before merging."
    )
    result = subprocess.run(
        [
            "gh", "pr", "create", "--base", base, "--head", branch,
            "--title", title[:120], "--body", body,
        ],
        cwd=branch_root,
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _plan(router: Router, ws: Workspace, brief: str, test_cmd: str, default_role: str) -> list[dict]:
    roles = sorted(p.stem for p in (ROOT / "roles").glob("*.md") if p.stem not in {"planner", "reviewer"})
    spec = ""
    for name in ("SPEC.md", "PROJECT.md"):
        try:
            spec += f"\n--- {name} ---\n{ws.read(name, 5000)}"
        except Exception:  # noqa: BLE001
            pass
    msgs = [{"role": "system", "content": _role_prompt("planner")},
            {"role": "user", "content": f"TASK:\n{brief}\n\nAVAILABLE ROLES: {', '.join(roles)}\nPREFERRED ROLE: {default_role}\nDEFAULT ACCEPTANCE: {test_cmd}\n"
                                        f"PROJECT FILES:\n{ws.tree()}\n{spec}"}]
    for _ in range(2):
        text = router.chat("planner", msgs, max_tokens=2000)
        try:
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
            subs = data["subtasks"][:6]
            if subs and all("goal" in s for s in subs):
                return subs
        except (ValueError, KeyError, TypeError):
            pass
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": 'Invalid. Reply with JSON only: {"subtasks":[{"id","role","goal","read":[],"acceptance"}]}'}]
    return [{"id": "s1", "role": default_role, "goal": brief, "read": ["SPEC.md"], "acceptance": test_cmd}]


def _run_sub(router: Router, ws: Workspace, cfg: dict, st: dict, default_accept: str, log) -> SubResult:
    b = cfg["budgets"]
    sid, role = st.get("id", "s?"), st.get("role", "coder")
    accept = st.get("acceptance", default_accept)
    files = list(st.get("read", []))
    goal, written = st["goal"], set()
    for attempt in range(1, b["max_fix_attempts"] + 1):
        router.begin_sub(b["subtask_tokens"])
        log(f"  {sid} [{role}] attempt {attempt}")
        try:
            res = run_subagent(router, ws, role, _role_prompt(role), goal, files, b["max_steps"],
                               b["max_context_chars"], on_event=lambda n, acts: log(f"    step {n}: {','.join(acts)}"))
        except BudgetExceeded as exc:
            return SubResult(sid, role, False, f"stopped: {exc}", attempt, written)
        written |= res.written
        if res.status != "done":
            note = res.summary
            if attempt == b["max_fix_attempts"]:
                return SubResult(sid, role, False, f"{res.status}: {note}", attempt, written)
            goal = f"{st['goal']}\n\nYour previous attempt ended with '{res.status}'. Continue and finish the goal."
            continue
        if not accept:
            return SubResult(sid, role, True, res.summary, attempt, written)
        code, out = ws.run(accept)
        log(f"    acceptance `{accept}` -> exit {code}")
        if code == 0:
            return SubResult(sid, role, True, res.summary, attempt, written)
        goal = (f"{st['goal']}\n\nThe acceptance command `{accept}` FAILED. Output:\n{truncate(out, 3000)}\n"
                "Fix the implementation. Do not edit protected files.")
        files = (list(st.get("read", [])) + sorted(written))[:6]
    return SubResult(sid, role, False, f"acceptance still failing after {b['max_fix_attempts']} attempts", attempt, written)


def _review_outcome(result: AgentResult, review_text: str | None) -> tuple[bool, str]:
    if result.status != "done":
        return False, f"reviewer ended with {result.status}: {result.summary}"
    if "REVIEW.md" not in result.written or review_text is None:
        return False, "reviewer did not write REVIEW.md"
    verdicts = re.findall(r"(?im)^\s*Verdict:\s*(pass|needs work)\s*$", review_text)
    if verdicts != ["pass"]:
        return False, "review verdict must be exactly 'Verdict: pass'"
    return True, "review passed"


def _should_push_task_branch(
    push_requested: bool, status: str, review_passed: bool, paused: bool
) -> bool:
    return push_requested and status == "done" and review_passed and not paused


def run_task(cfg: dict, task_path: Path, mock: bool = False, echo=print) -> str:
    """Returns 'done', 'failed' or raises OutOfBudget (task stays queued)."""
    head, body = parse_task(task_path.read_text(encoding="utf-8"))
    project = head.get("project", "")
    repository = head.get("repository")
    push_value = head.get("push", "false").strip().lower()
    pr_value = head.get("pr", "false").strip().lower()
    if push_value not in {"true", "false"} or pr_value not in {"true", "false"}:
        raise SystemExit("Task headers 'push' and 'pr' must be 'true' or 'false'.")
    pr_requested = pr_value == "true"
    push_requested = push_value == "true" or pr_requested
    if push_requested and cfg.get("git", {}).get("mode", "branch") != "branch":
        raise SystemExit("Task header 'push: true' requires git mode 'branch'.")
    if push_requested and not cfg.get("review", True):
        raise SystemExit("Task branch publishing requires the reviewer gate to be enabled.")
    if pr_requested:
        if not shutil.which("gh"):
            raise SystemExit("Task header 'pr: true' requires GitHub CLI (gh) to be installed.")
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if auth.returncode:
            raise SystemExit("Task header 'pr: true' requires an authenticated GitHub CLI session (run gh auth login).")
    if mock and not project.startswith("demo"):
        raise SystemExit("--mock only runs against the demo projects (scripted replies would write junk elsewhere).")
    pdir = ensure_project(project, repository)
    meta = load_project(project)
    external_projects = projects_root() != (ROOT / "projects").resolve()
    pcfg = _project_config(cfg, meta, external_projects)
    default_role = meta.get("default_role", "coder")
    if push_requested and not external_projects:
        raise SystemExit("Task header 'push: true' is only supported for projects outside the BANTU repository.")
    repo_root = _git_root(pdir)
    if external_projects and cfg.get("git", {}).get("mode", "branch") == "branch":
        if repo_root is None:
            raise SystemExit(f"External project must be a Git repository: {pdir}")
        if repo_root != pdir:
            raise SystemExit(
                f"External project must be the root of its own Git repository: {pdir} "
                f"(detected repository root: {repo_root})"
            )
    task_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", task_path.stem.lower())[:30]
    run_dir = ROOT / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(msg: str) -> None:
        lines.append(msg)
        echo(msg)

    router = Router(cfg, ROOT / "state", mock=mock)
    limit = int(head.get("budget_tokens", cfg["budgets"]["task_tokens"]))
    router.start_task(limit, run_dir / "calls.jsonl")
    ws = Workspace(pdir, pcfg, protected=meta["protected"])
    log(f"BANTU task {task_id} (project {project}, budget {limit} tokens)")

    branch, original = None, None
    committed = False
    status, paused = "done", False
    review_passed = False
    branch_root = None
    state_path = _task_state_path(task_path, project)
    resume_state = _load_task_state(state_path, repo_root)
    task_state = dict(resume_state or {})
    if cfg.get("git", {}).get("mode", "branch") == "branch" and repo_root is not None:
        branch_root = repo_root
        code, dirty = _git(branch_root, "status", "--porcelain")
        if code:
            log(f"  git setup failed: {dirty}")
            status = "failed"
        elif dirty and external_projects:
            current_branch = _git(branch_root, "rev-parse", "--abbrev-ref", "HEAD")[1]
            if not resume_state or current_branch != resume_state["branch"]:
                log("  git setup failed: external project working tree is not clean; commit or stash changes first")
                status = "failed"
            else:
                code, output = _git(branch_root, "add", "-A", "--", pdir.relative_to(branch_root).as_posix())
                if code:
                    log(f"  resume checkpoint failed to stage interrupted changes: {output}")
                    status = "failed"
                else:
                    code, output = _git(branch_root, "commit", "-m", f"BANTU: checkpoint {task_path.stem}")
                    if code:
                        log(f"  resume checkpoint failed to commit interrupted changes: {output}")
                        status = "failed"
                    else:
                        log(f"  checkpointed interrupted changes on `{current_branch}`")
        elif dirty:
            log("  note: working tree not clean, so BANTU will not create a git branch for this run")
        if status == "done" and resume_state:
            original = resume_state["original"]
            branch = resume_state["branch"]
            code, output = _git(branch_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            if code:
                log(f"  resume failed: task branch '{branch}' does not exist")
                status = "failed"
                branch = None
            else:
                code, current_branch = _git(branch_root, "rev-parse", "--abbrev-ref", "HEAD")
                if code:
                    log(f"  resume failed: {output}")
                    status = "failed"
                    branch = None
                else:
                    if current_branch != original:
                        code, output = _git(branch_root, "checkout", original)
                    else:
                        code, output = 0, ""
                    if code:
                        log(f"  resume failed: {output}")
                        status = "failed"
                        branch = None
                    else:
                        ok, output = _refresh_branch(branch_root, original)
                        if not ok:
                            log(f"  resume failed: {output}")
                            status = "failed"
                            branch = None
                        else:
                            code, output = _git(branch_root, "checkout", branch)
                            if code:
                                log(f"  resume failed: {output}")
                                status = "failed"
                                branch = None
                            else:
                                code, output = _git(branch_root, "rebase", original)
                                if code:
                                    _git(branch_root, "rebase", "--abort")
                                    _git(branch_root, "checkout", original)
                                    log(f"  resume failed: task branch could not be rebased onto {original}: {output}")
                                    status = "failed"
                                    branch = None
                                else:
                                    log(f"  resuming task branch `{branch}`")
        elif status == "done" and not dirty:
            code, original_name = _git(branch_root, "rev-parse", "--abbrev-ref", "HEAD")
            if code:
                log(f"  git setup failed: {original_name}")
                status = "failed"
            else:
                original = original_name
                ok, output = _refresh_branch(branch_root, original)
                if not ok:
                    log(f"  git setup failed: {output}")
                    status = "failed"
                    original = None
                else:
                    branch = f"bantu/{task_id}"
                    code, output = _git(branch_root, "checkout", "-b", branch)
                    if code:
                        log(f"  git setup failed: {output}")
                        branch = None
                        status = "failed"
                    else:
                        task_state = {
                            "project": project,
                            "repo_root": str(branch_root),
                            "original": original,
                            "branch": branch,
                            "status": "paused",
                            "subtasks": None,
                            "completed_subtasks": [],
                        }
                        _save_task_state(
                            state_path,
                            task_state,
                        )
    if push_requested and branch is None:
        status = "failed"
        log("  git push unavailable: could not create a clean task branch")
    results: list[SubResult] = []
    try:
        if status == "done":
            subs = task_state.get("subtasks")
            if not isinstance(subs, list):
                subs = parse_subtasks(body)
            if subs is None:
                log("  no explicit subtasks; asking the planner")
                subs = _plan(router, ws, body, meta["test_cmd"], default_role)
            task_state["subtasks"] = subs
            subs, completed_by_id = _restore_subtask_checkpoint(task_state, default_role)
            for st in subs:
                st.setdefault("role", default_role)
            for item in completed_by_id.values():
                results.append(
                    SubResult(
                        id=item["id"],
                        role=item.get("role", default_role),
                        ok=True,
                        note=item.get("note", "completed before pause"),
                        attempts=item.get("attempts", 1),
                        files=set(item.get("files", [])),
                    )
                )
            if completed_by_id:
                log(f"  resuming with {len(completed_by_id)} completed subtask(s) checkpointed")
            if branch:
                task_state["completed_subtasks"] = list(completed_by_id.values())
                _save_task_state(state_path, task_state)
            for st in subs:
                if st.get("id") in completed_by_id:
                    continue
                r = _run_sub(router, ws, cfg, st, meta["test_cmd"], log)
                results.append(r)
                if not r.ok:
                    status = "failed"
                    break
                if branch:
                    completed_by_id[r.id] = {
                        "id": r.id,
                        "role": r.role,
                        "note": r.note,
                        "attempts": r.attempts,
                        "files": sorted(r.files),
                    }
                    task_state["completed_subtasks"] = list(completed_by_id.values())
                    _save_task_state(state_path, task_state)
        if status == "done" and branch:
            code, changed_output = _git(branch_root, "diff", "--name-only", f"{original}...{branch}")
            if code:
                status = "failed"
                log(f"  REVIEW FAILED: could not list task branch changes: {changed_output}")
            else:
                changed_files = {f for r in results for f in r.files}
                changed_files.update(changed_output.splitlines())
        else:
            changed_files = {f for r in results for f in r.files}
        if status == "done" and cfg.get("review", True):
            rev_ws = Workspace(pdir, pcfg, protected=meta["protected"], writable=["REVIEW.md"])
            changed = sorted(changed_files)[:8]
            router.begin_sub(cfg["budgets"]["subtask_tokens"])
            try:
                review_result = run_subagent(
                    router, rev_ws, "reviewer", _role_prompt("reviewer"),
                    "Review the changed files against SPEC.md. Write REVIEW.md.", ["SPEC.md"] + changed,
                    6, cfg["budgets"]["max_context_chars"],
                )
            except BudgetExceeded as exc:
                status = "failed"
                log(f"  REVIEW FAILED: reviewer budget reached: {exc}")
            else:
                try:
                    review_text = rev_ws.read("REVIEW.md") if "REVIEW.md" in review_result.written else None
                except ToolError as exc:
                    status = "failed"
                    log(f"  REVIEW FAILED: could not read reviewer output: {exc}")
                else:
                    review_ok, review_note = _review_outcome(review_result, review_text)
                    log(f"  REVIEW {'PASSED' if review_ok else 'FAILED'}: {review_note}")
                    if not review_ok:
                        status = "failed"
                    else:
                        review_passed = True
    except OutOfBudget as exc:
        paused = True
        status = "paused"
        log(f"  PAUSED: {exc}")
    except (BudgetExceeded, ProviderError) as exc:
        status = "failed"
        log(f"  STOPPED: {exc}")

    # ---- git + report ------------------------------------------------------
    commit_note = ""
    if branch:
        project_path = pdir.relative_to(branch_root).as_posix() or "."
        code, output = _git(branch_root, "add", "-A", "--", project_path)
        if code:
            status = "failed"
            log(f"  git stage failed: {output}")
        else:
            code, changes = _git(branch_root, "status", "--porcelain")
            if code:
                status = "failed"
                log(f"  git status failed: {changes}")
            elif changes:
                code, output = _git(branch_root, "commit", "-m", f"BANTU: {task_path.stem} ({status})")
                if code:
                    status = "failed"
                    log(f"  git commit failed: {output}")
                else:
                    committed = True
                    commit_note = (
                        f"Committed on branch `{branch}` in `{branch_root}`. "
                        f"Review: `git -C \"{branch_root}\" diff {original}..{branch}`. "
                        f"Accept: `git -C \"{branch_root}\" merge {branch}`."
                    )
        if _should_push_task_branch(push_requested, status, review_passed, paused):
            if not committed and not _branch_has_commits(branch_root, original, branch):
                status = "failed"
                log("  git push failed: no task changes were committed to the run branch")
            else:
                code, output = _git(branch_root, "push", "--set-upstream", "origin", branch)
                if code:
                    status = "failed"
                    log(f"  git push failed: {output}")
                else:
                    log(f"  pushed branch `{branch}` to origin (default branch was not changed)")
                    commit_note += f" Pushed to `origin/{branch}`."
                    if pr_requested:
                        title = f"BANTU: {body.strip().splitlines()[0] if body.strip() else task_path.stem}"
                        code, output = _create_pull_request(branch_root, original, branch, title)
                        if code:
                            status = "failed"
                            log(f"  pull request creation failed: {output}")
                        else:
                            log(f"  pull request created: {output}")
                            commit_note += f" Pull request: {output}."
        elif push_requested:
            log("  git push skipped: task did not complete with a passing reviewer verdict")
        if original:
            code, output = _git(branch_root, "checkout", original)
            if code:
                status = "failed"
                log(f"  git checkout failed; run remains on branch `{branch}`: {output}")
    if paused and branch and original:
        _save_task_state(
            state_path,
            task_state,
        )
    elif not paused:
        state_path.unlink(missing_ok=True)
    rows = "\n".join(f"| {r.id} | {r.role} | {'ok' if r.ok else 'FAILED'} | {r.attempts} | {r.note.splitlines()[0][:90] if r.note else ''} |" for r in results)
    models = "\n".join(f"- {m}: {t:,} tokens" for m, t in router.by_model.items()) or "- none"
    report = (f"# BANTU report: {task_id}\n\nStatus: **{'paused (out of budget)' if paused else status}**\n\n"
              f"| Subtask | Role | Result | Attempts | Note |\n|---|---|---|---|---|\n{rows}\n\n"
              f"Tokens used: {router.task_tokens:,} of {limit:,}\n\n{models}\n\n{commit_note}\n\n## Log\n```\n" + "\n".join(lines) + "\n```\n")
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    echo(f"Report: runs/{task_id}/report.md")
    if paused:
        raise OutOfBudget("paused")
    return status
