from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import ROOT, load_config, projects_root


def cmd_run(args, cfg):
    from .llm import OutOfBudget
    from .orchestrator import run_task
    try:
        status = run_task(cfg, Path(args.task), mock=args.mock)
    except OutOfBudget:
        print("Paused: out of free tokens. Re-run later.")
        return 2
    return 0 if status == "done" else 1


def cmd_daemon(args, cfg):
    from .daemon import run_daemon
    run_daemon(cfg, mock=args.mock)


def cmd_worker(args, cfg):
    from .worker import run_worker, worker_settings

    try:
        api_url, token, poll_seconds = worker_settings()
        run_worker(cfg, api_url, token, poll_seconds)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_add(args, cfg):
    inbox = ROOT / "tasks" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", args.text.lower())[:40].strip("-") or "task"
    path = inbox / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}.md"
    head = f"project: {args.project}\n"
    if args.repository:
        head += f"repository: {args.repository}\n"
    if args.push:
        head += "push: true\n"
    if args.pr:
        head += "pr: true\n"
    if args.budget:
        head += f"budget_tokens: {args.budget}\n"
    path.write_text(f"{head}---\n{args.text}\n", encoding="utf-8")
    print(f"Queued {path.relative_to(ROOT)}")


def cmd_status(args, cfg):
    usage = ROOT / "state" / "usage.json"
    data = json.loads(usage.read_text()) if usage.exists() else {"date": "-", "total": 0, "providers": {}}
    print(f"Tokens today ({data.get('date')}): {data['total']:,} of {cfg['budgets']['daily_tokens_total']:,}")
    for prov, v in data["providers"].items():
        cap = cfg["providers"].get(prov, {}).get("daily_tokens")
        print(f"  {prov}: {v['tokens']:,} tokens, {v['requests']} requests" + (f" (cap {cap:,})" if cap else ""))
    for d in ("inbox", "running", "done", "failed"):
        print(f"tasks/{d}: {len(list((ROOT / 'tasks' / d).glob('*.md')))}")
    reports = sorted((ROOT / "runs").glob("*/report.md"))[-3:]
    for r in reports:
        print("recent:", r.relative_to(ROOT))


def cmd_models(args, cfg):
    for name, p in cfg["providers"].items():
        key = os.environ.get(p.get("key_env", ""), "")
        if not key or "base_url" not in p:
            print(f"{name}: no key set, skipped")
            continue
        req = urllib.request.Request(p["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                ids = sorted(m.get("id", "?") for m in json.load(resp).get("data", []))
            print(f"{name}: " + ", ".join(ids[:40]))
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: could not list models ({exc})")


def cmd_doctor(args, cfg):
    ok = True

    def check(label, passed, hint=""):
        nonlocal ok
        ok &= bool(passed)
        print(("OK   " if passed else "FAIL ") + label + ("" if passed else f"  -> {hint}"))

    check("Python 3.10+", sys.version_info >= (3, 10), "install a newer Python")
    check("git available", shutil.which("git"), "install git")
    keys = [n for n, p in cfg["providers"].items() if os.environ.get(p.get("key_env", ""))]
    check("at least one provider key set", keys, "copy .env.example to .env and add a key")
    print("     providers with keys:", ", ".join(keys) or "none")
    project_root = projects_root()
    images = {}
    if not project_root.is_dir():
        print(f"WARN projects root '{project_root}' does not exist")
    else:
        for meta in sorted(project_root.glob("*/bantu.project.json")):
            img = json.loads(meta.read_text()).get("sandbox", {}).get("image")
            if img:
                images[img] = meta.parent.name
            if project_root != (ROOT / "projects").resolve() and cfg.get("git", {}).get("mode", "branch") == "branch":
                repo = subprocess.run(
                    ["git", "-C", str(meta.parent), "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True,
                )
                repo_root = Path(repo.stdout.strip()).resolve() if repo.returncode == 0 else None
                check(
                    f"external project '{meta.parent.name}' is its own Git repository",
                    repo_root == meta.parent.resolve(),
                    f"initialize Git at '{meta.parent}' and commit a clean baseline",
                )
    if cfg["sandbox"]["mode"] == "docker":
        has = shutil.which("docker")
        check("docker installed", has, "install Docker Desktop, or set BANTU_SANDBOX=local (less safe)")
        if has:
            r = subprocess.run(["docker", "image", "inspect", cfg["sandbox"]["image"]], capture_output=True)
            check(f"sandbox image '{cfg['sandbox']['image']}' built", r.returncode == 0,
                  "docker build -t bantu-sandbox -f docker/Dockerfile.sandbox docker/")
        for img, proj in images.items():
            if has:
                r = subprocess.run(["docker", "image", "inspect", img], capture_output=True)
                print(("OK   " if r.returncode == 0 else "WARN ") + f"sandbox image '{img}' for project '{proj}'"
                      + ("" if r.returncode == 0 else "  -> not built yet (only needed if you run that project)"))
    else:
        print("WARN sandbox mode is 'local': agent commands run on this machine. Use docker where you can.")
    print("\nAll good." if ok else "\nFix the FAIL lines above.")


def main():
    parser = argparse.ArgumentParser(prog="bantu", description="BANTU agent orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run one task file now")
    p.add_argument("task")
    p.add_argument("--mock", action="store_true", help="scripted replies, zero tokens")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("daemon", help="watch tasks/inbox and run tasks as they appear")
    p.add_argument("--mock", action="store_true")
    p.set_defaults(fn=cmd_daemon)
    p = sub.add_parser("worker", help="poll the CRM for approved BANTU development tasks")
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("add", help="queue a task from text")
    p.add_argument("text")
    p.add_argument("--project", required=True)
    p.add_argument("--repository", help="clone or verify this Git remote URL")
    p.add_argument("--push", action="store_true", help="push the task branch to origin after review passes")
    p.add_argument("--pr", action="store_true", help="push the reviewed branch and open a pull request (requires gh)")
    p.add_argument("--budget", type=int)
    p.set_defaults(fn=cmd_add)
    for name, fn, helptext in (("status", cmd_status, "token usage and queue"),
                               ("models", cmd_models, "list model ids each provider offers"),
                               ("doctor", cmd_doctor, "check the setup")):
        sub.add_parser(name, help=helptext).set_defaults(fn=fn)
    args = parser.parse_args()
    sys.exit(args.fn(args, load_config()) or 0)


if __name__ == "__main__":
    main()
