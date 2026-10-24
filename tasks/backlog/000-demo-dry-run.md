project: demo
budget_tokens: 20000
---
# Dry run of the BANTU plumbing
Run with `python -m bantu run tasks/backlog/000-demo-dry-run.md --mock` to test everything for zero tokens.

```subtasks
[
  {"id": "s1", "role": "coder",
   "goal": "Create src/demo/__init__.py (empty) and src/demo/greet.py with greet(name) returning 'Hello, <name>!'.",
   "read": ["tests/test_greet.py"],
   "acceptance": "python -m pytest -q"}
]
```
