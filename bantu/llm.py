"""Provider router: free-tier aware, rate limited, budgeted. Standard library only."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .mock import mock_reply


class OutOfBudget(Exception):
    """No provider can serve right now (daily caps or cool-downs). Try again later."""


class BudgetExceeded(Exception):
    """A per-task or per-subtask token limit was reached."""


class ProviderError(Exception):
    """Every provider failed with a non-retryable error (bad model name, bad key). Fix the config."""


class _Retryable(Exception):
    def __init__(self, msg: str, wait: int = 60):
        super().__init__(msg)
        self.wait = wait


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


class Router:
    def __init__(self, cfg: dict, state_dir: Path, mock: bool = False):
        self.cfg, self.mock = cfg, mock
        self.state_path = Path(state_dir) / "usage.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_call: dict[str, float] = {}
        self.cooldown: dict[str, float] = {}
        self.task_tokens = self.sub_tokens = 0
        self.task_limit = self.sub_limit = None
        self.log_path: Path | None = None
        self.by_model: dict[str, int] = {}
        self.state = {"date": _today(), "total": 0, "providers": {}}
        self._state_saved = False
        self._load()

    # ---- usage state ----------------------------------------------------
    def _load(self) -> None:
        fresh = {"date": _today(), "total": 0, "providers": {}}
        if not self.state_path.exists():
            if self._state_saved:
                raise RuntimeError(f"usage state file disappeared: {self.state_path}")
            self.state = fresh
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read usage state {self.state_path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
            raise RuntimeError(f"invalid usage state structure in {self.state_path}")
        if not isinstance(data.get("date"), str) or type(data.get("total")) is not int or data["total"] < 0:
            raise RuntimeError(f"invalid usage counters in {self.state_path}")
        try:
            dt.datetime.strptime(data["date"], "%Y-%m-%d")
        except ValueError as exc:
            raise RuntimeError(f"invalid usage date in {self.state_path}") from exc
        for name, provider in data["providers"].items():
            if (
                not isinstance(name, str)
                or not isinstance(provider, dict)
                or type(provider.get("tokens")) is not int
                or provider["tokens"] < 0
                or type(provider.get("requests")) is not int
                or provider["requests"] < 0
            ):
                raise RuntimeError(f"invalid provider usage entry in {self.state_path}")
        if data["total"] != sum(provider["tokens"] for provider in data["providers"].values()):
            raise RuntimeError(f"inconsistent usage totals in {self.state_path}")
        self.state = data if data["date"] == _today() else fresh
        self._state_saved = True

    def _save(self) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f"{self.state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(self.state, temporary, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
            self._state_saved = True
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def start_task(self, limit: int | None, log_path: Path) -> None:
        self.task_tokens, self.task_limit, self.log_path, self.by_model = 0, limit, log_path, {}
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def begin_sub(self, limit: int | None) -> None:
        self.sub_tokens, self.sub_limit = 0, limit

    # ---- public API -------------------------------------------------------
    def chat(self, role: str, messages: list[dict], max_tokens: int | None = None) -> str:
        max_tokens = max_tokens or self.cfg["budgets"].get("max_output_tokens", 6000)
        self._load()
        if self.task_limit and self.task_tokens >= self.task_limit:
            raise BudgetExceeded(f"task budget of {self.task_limit} tokens reached")
        if self.sub_limit and self.sub_tokens >= self.sub_limit:
            raise BudgetExceeded(f"subtask budget of {self.sub_limit} tokens reached")
        if self.state["total"] >= self.cfg["budgets"]["daily_tokens_total"]:
            raise OutOfBudget("daily total token cap reached")

        if self.mock:
            text = mock_reply(role, messages)
            self._account("mock", "mock", role, max(1, (sum(len(m["content"]) for m in messages) + len(text)) // 4))
            return text

        pool = self.cfg["roles"].get(role, self.cfg["roles"].get("default", "fast"))
        refs = self.cfg["pools"][pool]
        problems: list[str] = []
        fatal: list[str] = []
        any_wait = False
        for _ in range(4):
            waiting = False
            for ref in refs:
                prov, model = ref.split("/", 1)
                p = self.cfg["providers"].get(prov)
                key = os.environ.get(p.get("key_env", ""), "") if p else ""
                if not p or not key:
                    continue
                if self.state["providers"].get(prov, {}).get("tokens", 0) >= p.get("daily_tokens", 10**12):
                    continue
                if self.cooldown.get(prov, 0) > time.time():
                    waiting = any_wait = True
                    continue
                pause = self.last_call.get(prov, 0) + 60.0 / max(p.get("rpm", 10), 1) - time.time()
                if pause > 0:
                    time.sleep(pause)
                try:
                    text, tokens = self._call(p, model, key, messages, max_tokens)
                except _Retryable as exc:
                    self.cooldown[prov] = time.time() + exc.wait
                    problems.append(f"{ref}: {exc}")
                    waiting = any_wait = True
                    continue
                except Exception as exc:  # noqa: BLE001 - provider errors must never crash a run
                    self.cooldown[prov] = time.time() + 300
                    problems.append(f"{ref}: {exc}")
                    fatal.append(f"{ref}: {exc}")
                    continue
                self.last_call[prov] = time.time()
                self._account(prov, model, role, tokens)
                return text
            if not waiting:
                break
            ahead = [t - time.time() for t in self.cooldown.values() if t > time.time()]
            time.sleep(max(1, min(min(ahead, default=5), 30)))
        if fatal and not any_wait:
            raise ProviderError("; ".join(fatal[-3:]) + "  -> check the model name and API key (python -m bantu models)")
        raise OutOfBudget("no provider available. " + "; ".join(problems[-3:]))

    # ---- internals --------------------------------------------------------
    def _account(self, prov: str, model: str, role: str, tokens: int) -> None:
        self.state["total"] += tokens
        slot = self.state["providers"].setdefault(prov, {"tokens": 0, "requests": 0})
        slot["tokens"] += tokens
        slot["requests"] += 1
        self.task_tokens += tokens
        self.sub_tokens += tokens
        self.by_model[f"{prov}/{model}"] = self.by_model.get(f"{prov}/{model}", 0) + tokens
        self._save()
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": int(time.time()), "role": role, "model": f"{prov}/{model}", "tokens": tokens}) + "\n")

    @staticmethod
    def _call(p: dict, model: str, key: str, messages: list[dict], max_tokens: int) -> tuple[str, int]:
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        payload.update(p.get("extra_body", {}))          # e.g. {"reasoning_effort": "low"} for Gemini
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            p["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=p.get("timeout_sec", 240)) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                raise _Retryable(f"HTTP {exc.code}") from exc
            raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise _Retryable(str(exc.reason), wait=15) from exc
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            raise _Retryable(f"network: {exc or type(exc).__name__}", wait=10) from exc
        text = (data["choices"][0]["message"].get("content") or "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        tokens = (data.get("usage") or {}).get("total_tokens") or (sum(len(m["content"]) for m in messages) + len(text)) // 4
        return text, int(tokens)
