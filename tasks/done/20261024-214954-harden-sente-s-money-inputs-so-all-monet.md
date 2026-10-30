project: sente
repository: https://github.com/asiimwearnold/Sente.git
push: true
budget_tokens: 30000
---
Harden Sente's money inputs so all monetary amounts are integer UGX values, never floats or booleans. Validate public money-taking rules and ledger methods before any state mutation. Preserve existing behavior for valid integers and existing documented ValueError behavior; reject non-integer amounts clearly. Change implementation files only, not tests/spec/docs. Acceptance: python -m pytest -q.

```subtasks
[{"id":"s1","role":"coder","goal":"Inspect rules.py and service.py. Enforce integer-only (reject bool as well as float) monetary inputs on all public monetary functions/methods, before they mutate ledger state. Keep valid integer behavior unchanged and raise clear ValueError for invalid numeric types. Do not edit tests, SPEC.md, or other files.","read":["SPEC.md","src/sente/rules.py","src/sente/service.py","tests/test_rules.py","tests/test_service.py"],"acceptance":"python -m pytest -q"}]
```
