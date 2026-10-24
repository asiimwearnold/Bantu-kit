project: sente
budget_tokens: 250000
---
# Sente HTTP API and one-page demo UI
There are no oracle tests for this task, so review the result yourself before demoing it. Keep the existing tests green.

```subtasks
[
  {"id": "s1", "role": "coder",
   "goal": "Create src/sente/api.py: a standard-library http.server JSON API wrapping the Sente service. Endpoints: POST /register {name}, GET /balance?name=, POST /mint {buyer, amount}, POST /transfer {src, dst, amount, idem_key?}, POST /exit/request {holder, agent, amount}, POST /exit/confirm {id, by}, POST /chat {user, text} which calls sente.chat.parse_intent and returns the intent WITHOUT executing it, GET /audit. Return JSON errors with proper status codes. Add a --selftest flag that starts the server on a free port in a thread, exercises every endpoint with urllib, prints OK, and exits 0.",
   "read": ["SPEC.md", "src/sente/service.py", "src/sente/chat.py"],
   "acceptance": "python -m sente.api --selftest"},
  {"id": "s2", "role": "coder",
   "goal": "Create web/index.html: one self-contained page (no external assets) that talks to the API: register users, buy Sente, send, request and confirm a cash-out, a chat box that shows the parsed intent with a Confirm button, and a live audit panel. Plain HTML, CSS and JavaScript only.",
   "read": ["src/sente/api.py"],
   "acceptance": "python -m pytest -q"}
]
```
