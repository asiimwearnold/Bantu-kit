project: demo-node
budget_tokens: 30000
---
# Node demo: proves a non-Python project works end to end
Dry run with zero tokens: `BANTU_SANDBOX=local python -m bantu run tasks/backlog/010-demo-node.md --mock`
Real run: build the `bantu-node` image first (see README), then run without `--mock`.

```subtasks
[
  {"id": "s1", "role": "coder-node",
   "goal": "Create src/greet.js, a CommonJS module exporting greet(name) that returns 'Hello, <name>!'.",
   "read": ["SPEC.md", "tests/greet.test.js"],
   "acceptance": "node --test"}
]
```
