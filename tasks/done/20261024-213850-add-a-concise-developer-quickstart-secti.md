project: sente
repository: https://github.com/asiimwearnold/Sente.git
push: true
budget_tokens: 25000
---
Add a concise developer Quickstart section to README.md: explicitly say this is a test-money prototype, state Python 3.10+ and standard-library requirements, and show the exact command to run the full suite. Change README.md only. Acceptance: python -m pytest -q.

```subtasks
[{"id":"s1","role":"coder","goal":"Edit only README.md to add a concise Quickstart section. State Sente is a test-money prototype, not production currency; require Python 3.10+ and the standard library; show python -m pytest -q as the full test command. Do not alter implementation, tests, or spec.","read":["README.md","SPEC.md"],"acceptance":"python -m pytest -q"}]
```
