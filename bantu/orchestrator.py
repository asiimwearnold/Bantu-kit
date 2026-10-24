"""Run one task file: (plan) -> sub-agents with acceptance checks -> review -> report -> git branch."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .agent import run_subagent
from .config import ROOT, load_project, project_dir
from .llm import BudgetExceeded, OutOfBudget, ProviderError, Router
from .tools import Workspace, truncate


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


def _git(*args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


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


def run_task(cfg: dict, task_path: Path, mock: bool = False, echo=print) -> str:
    """Returns 'done', 'failed' or raises OutOfBudget (task stays queued)."""
    head, body = parse_task(task_path.read_text(encoding="utf-8"))
    project = head.get("project", "")
    if mock and not project.startswith("demo"):
        raise SystemExit("--mock only runs against the demo projects (scripted replies would write junk elsewhere).")
    meta = load_project(project)
    pcfg = {**cfg, "sandbox": {**cfg["sandbox"], **meta.get("sandbox", {})}}   # per-project language/toolchain
    default_role = meta.get("default_role", "coder")
    pdir = project_dir(project)
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
    if cfg.get("git", {}).get("mode", "branch") == "branch" and (ROOT / ".git").exists():
        if _git("status", "--porcelain")[1] == "":
            original = _git("rev-parse", "--abbrev-ref", "HEAD")[1]
            branch = f"bantu/{task_id}"
            _git("checkout", "-b", branch)
        else:
            log("  note: working tree not clean, so BANTU will not create a git branch for this run")

    results: list[SubResult] = []
    status, paused = "done", False
    try:
        subs = parse_subtasks(body)
        if subs is None:
            log("  no explicit subtasks; asking the planner")
            subs = _plan(router, ws, body, meta["test_cmd"], default_role)
        for st in subs:
            st.setdefault("role", default_role)
            r = _run_sub(router, ws, cfg, st, meta["test_cmd"], log)
            results.append(r)
            if not r.ok:
                status = "failed"
                break
        if status == "done" and cfg.get("review", True):
            rev_ws = Workspace(pdir, pcfg, protected=meta["protected"], writable=["REVIEW.md"])
            changed = sorted({f for r in results for f in r.files})[:8]
            router.begin_sub(cfg["budgets"]["subtask_tokens"])
            try:
                run_subagent(router, rev_ws, "reviewer", _role_prompt("reviewer"),
                             "Review the changed files against SPEC.md. Write REVIEW.md.", ["SPEC.md"] + changed,
                             6, cfg["budgets"]["max_context_chars"])
            except BudgetExceeded:
                log("  reviewer skipped: budget reached")
    except OutOfBudget as exc:
        paused = True
        log(f"  PAUSED: {exc}")
    except (BudgetExceeded, ProviderError) as exc:
        status = "failed"
        log(f"  STOPPED: {exc}")

    # ---- git + report ------------------------------------------------------
    commit_note = ""
    if branch:
        _git("add", "-A", str(pdir.relative_to(ROOT)))
        if _git("status", "--porcelain")[1]:
            _git("commit", "-m", f"BANTU: {task_path.stem} ({status})")
            commit_note = (f"Committed on branch `{branch}`. Review: `git diff {original}..{branch}`. "
                           f"Accept: `git merge {branch}`.")
        _git("checkout", original)
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
