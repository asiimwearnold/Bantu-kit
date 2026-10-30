from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import ROOT
from .llm import OutOfBudget
from .orchestrator import run_task

POLL_SECONDS = 10
HEARTBEAT_SECONDS = 30
MAX_REPORT_CHARS = 30000
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
REPORT_PATH_PATTERN = re.compile(r"^Report: (runs/[^\r\n]+/report\.md)$", re.MULTILINE)
BRANCH_PATTERN = re.compile(r"Committed on branch `([^`]+)`")


def validate_repository_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("BANTU worker accepts public HTTPS Git URLs without credentials or custom ports.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("BANTU worker cannot access local or internal Git hosts.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ValueError("BANTU worker could not resolve the Git host.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("BANTU worker cannot access private or reserved Git hosts.")
    return value


class CrmWorkerClient:
    def __init__(self, api_url: str, token: str, worker_id: str):
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BANTU_CRM_API_URL must be an HTTP or HTTPS URL.")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("BANTU_CRM_API_URL must use HTTPS outside localhost.")
        if len(token) < 32:
            raise ValueError("BANTU_WORKER_TOKEN must contain at least 32 characters.")
        self.base_url = api_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def request(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/internal/bantu/{path.lstrip('/')}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"BANTU CRM request failed: {exc}") from exc
        try:
            result = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("BANTU CRM returned an invalid response.") from exc
        if not isinstance(result, dict):
            raise RuntimeError("BANTU CRM returned an invalid response shape.")
        return result

    def claim(self) -> dict | None:
        response = self.request("tasks/claim", {"workerId": self.worker_id})
        task = response.get("task")
        if task is None:
            return None
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not TASK_ID_PATTERN.fullmatch(task["id"])
            or not isinstance(task.get("repository"), str)
            or not isinstance(task.get("request"), str)
        ):
            raise RuntimeError("BANTU CRM returned a malformed task.")
        return task

    def heartbeat(self, task_id: str) -> None:
        self.request(f"tasks/{task_id}/heartbeat", {"workerId": self.worker_id})

    def complete(self, task_id: str, payload: dict) -> None:
        self.request(f"tasks/{task_id}/complete", {**payload, "workerId": self.worker_id})


def _task_file(task: dict) -> Path:
    path = ROOT / "tasks" / "running" / f"crm-{task['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"project: crm-{task['id']}\n"
        f"repository: {task['repository']}\n"
        "push: true\n"
        "---\n"
        f"{task['request'].strip()}\n"
    )
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def _run_claimed_task(client: CrmWorkerClient, cfg: dict, task: dict, log=print) -> None:
    task_path = _task_file(task)
    events: list[str] = []
    stopped = threading.Event()
    heartbeat_errors: list[Exception] = []

    def heartbeat_loop() -> None:
        while not stopped.wait(HEARTBEAT_SECONDS):
            try:
                client.heartbeat(task["id"])
            except Exception as exc:
                heartbeat_errors.append(exc)
                log(f"BANTU worker heartbeat failed for task {task['id']}: {exc}")
                return

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    status = "FAILED"
    failure = None
    try:
        status = run_task(cfg, task_path, echo=lambda line: (events.append(line), log(line)))
    except OutOfBudget:
        status = "PAUSED"
    except (Exception, SystemExit) as exc:
        failure = str(exc) or type(exc).__name__
        log(f"BANTU task {task['id']} stopped: {failure}")
    finally:
        stopped.set()
        heartbeat.join(timeout=2)

    report = ""
    report_match = REPORT_PATH_PATTERN.search("\n".join(events))
    if report_match:
        report_path = ROOT / report_match.group(1)
        if report_path.is_file():
            report = report_path.read_text(encoding="utf-8")[-MAX_REPORT_CHARS:]
    branch_match = BRANCH_PATTERN.search(report)
    branch = branch_match.group(1) if branch_match else None
    if heartbeat_errors:
        status = "FAILED"
        failure = "BANTU worker lost its task lease before it could report completion."
    client.complete(
        task["id"],
        {
            "status": status.upper(),
            "branch": branch,
            "report": report or None,
            "failure": failure,
        },
    )
    destination = ROOT / "tasks" / {
        "DONE": "done",
        "FAILED": "failed",
        "PAUSED": "inbox",
    }[status.upper()] / task_path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if task_path.exists():
        shutil.move(str(task_path), str(destination))


def run_worker(cfg: dict, api_url: str, token: str, poll_seconds: int = POLL_SECONDS) -> None:
    if poll_seconds < 1 or poll_seconds > 300:
        raise ValueError("BANTU worker poll interval must be between 1 and 300 seconds.")
    worker_id = f"bantu-{uuid.uuid4()}"
    client = CrmWorkerClient(api_url, token, worker_id)
    print(f"BANTU worker connected to CRM at {api_url}")
    while True:
        try:
            task = client.claim()
            if task is None:
                time.sleep(poll_seconds)
                continue
            print(f"BANTU worker claimed task {task['id']}")
            try:
                task["repository"] = validate_repository_url(task["repository"])
            except ValueError as exc:
                client.complete(
                    task["id"],
                    {
                        "status": "FAILED",
                        "branch": None,
                        "report": None,
                        "failure": str(exc),
                    },
                )
                continue
            _run_claimed_task(client, cfg, task)
        except KeyboardInterrupt:
            print("BANTU worker stopped.")
            return
        except Exception as exc:
            print(f"BANTU worker error: {exc}")
            time.sleep(poll_seconds)


def worker_settings() -> tuple[str, str, int]:
    api_url = os.environ.get("BANTU_CRM_API_URL", "").strip()
    token = os.environ.get("BANTU_WORKER_TOKEN", "").strip()
    try:
        poll_seconds = int(os.environ.get("BANTU_WORKER_POLL_SECONDS", POLL_SECONDS))
    except ValueError as exc:
        raise ValueError("BANTU_WORKER_POLL_SECONDS must be an integer.") from exc
    if not api_url:
        raise ValueError("Set BANTU_CRM_API_URL in the BANTU worker environment.")
    if not token:
        raise ValueError("Set BANTU_WORKER_TOKEN in the BANTU worker environment.")
    return api_url, token, poll_seconds
