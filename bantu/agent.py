"""One sub-agent: a short, bounded loop of model call -> actions -> results."""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import Router
from .tools import ToolError, Workspace, parse_actions, truncate

PROTOCOL = """\
HOW TO ACT. Reply with action blocks only, in plain text (no JSON, no markdown fences inside blocks):

@@write relative/path.py
<full file content>
@@end

@@run
python -m pytest -q
@@end

@@read relative/path.py

@@finish
<2-4 line summary of what you did and anything left undone>
@@end

Rules: several blocks per reply are fine and save tokens. Paths are relative to the project root.
Write whole files, never diffs. Do not explain at length. Protected files cannot be edited.
When the goal is met (or you are blocked) send @@finish."""


@dataclass
class AgentResult:
    status: str                      # done | max_steps | stuck
    summary: str = ""
    written: set[str] = field(default_factory=set)


def _context(ws: Workspace, files: list[str], per_file: int = 12000) -> str:
    parts = []
    for rel in files:
        try:
            parts.append(f"--- {rel} ---\n{ws.read(rel, per_file)}")
        except ToolError as exc:
            parts.append(f"--- {rel} --- ({exc})")
    return "\n\n".join(parts) or "(none)"


def _trim(messages: list[dict], limit: int) -> list[dict]:
    if sum(len(m["content"]) for m in messages) <= limit or len(messages) <= 8:
        return messages
    note = {"role": "user", "content": "[earlier steps omitted to save tokens; rely on the files and latest results]"}
    return messages[:2] + [note] + messages[-6:]


def run_subagent(router: Router, ws: Workspace, role: str, role_prompt: str, goal: str,
                 files: list[str], max_steps: int, max_context_chars: int, on_event=None) -> AgentResult:
    messages = [
        {"role": "system", "content": role_prompt + "\n\n" + PROTOCOL},
        {"role": "user", "content": f"GOAL:\n{goal}\n\nPROJECT FILES:\n{ws.tree()}\n\nFILES:\n{_context(ws, files)}"},
    ]
    written: set[str] = set()
    strikes = 0
    for step in range(max_steps):
        text = router.chat(role, _trim(messages, max_context_chars))
        messages.append({"role": "assistant", "content": text})
        actions = parse_actions(text)
        if not actions:
            strikes += 1
            if strikes >= 2:
                return AgentResult("stuck", "model did not produce valid action blocks", written)
            messages.append({"role": "user", "content": "No valid action blocks found. Use @@write, @@run, @@read or @@finish."})
            continue
        results: list[str] = []
        for act in actions:
            if act["a"] == "finish":
                return AgentResult("done", act["summary"], written)
            try:
                if act["a"] == "write":
                    ws.write(act["path"], act["content"])
                    written.add(act["path"])
                    results.append(f"wrote {act['path']} ({len(act['content'])} chars)")
                elif act["a"] == "read":
                    results.append(f"--- {act['path']} ---\n{ws.read(act['path'])}")
                elif act["a"] == "run":
                    code, out = ws.run(act["cmd"])
                    results.append(f"$ {act['cmd']}\n[exit {code}]\n{out}")
            except ToolError as exc:
                results.append(f"ERROR: {exc}")
        if on_event:
            on_event(step + 1, [a["a"] for a in actions])
        messages.append({"role": "user", "content": truncate("RESULTS:\n" + "\n\n".join(results), 7000)})
    return AgentResult("max_steps", "step limit reached without @@finish", written)
