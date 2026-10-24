"""Scripted replies so the whole pipeline can be tested with zero tokens (--mock)."""
from __future__ import annotations

import json


def mock_reply(role: str, messages: list[dict]) -> str:
    turn = sum(1 for m in messages if m["role"] == "assistant")
    if role == "planner":
        return json.dumps({"subtasks": [{
            "id": "s1", "role": "coder", "goal": "Implement greet() so the tests pass.",
            "read": ["tests/test_greet.py"], "acceptance": "python -m pytest -q"}]})
    if role == "reviewer":
        if turn == 0:
            return "@@write REVIEW.md\n# Review (mock)\nNo issues found.\n@@end\n@@finish\nReview written.\n@@end"
        return "@@finish\nDone.\n@@end"
    # coder
    if turn == 0 and "greet.js" in messages[1]["content"]:
        return ("@@write src/greet.js\nexports.greet = (name) => `Hello, ${name}!`;\n@@end\n"
                "@@run\nnode --test\n@@end")
    if turn == 0:
        return ("@@write src/demo/__init__.py\n@@end\n"
                "@@write src/demo/greet.py\n"
                "def greet(name: str) -> str:\n    return f\"Hello, {name}!\"\n@@end\n"
                "@@run\npython -m pytest -q\n@@end")
    return "@@finish\nImplemented greet().\n@@end"
