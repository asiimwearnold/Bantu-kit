project: sente
budget_tokens: 120000
---
# Sente chat intent parser
Deterministic parser that sits between free text (or a language model) and the ledger. It never moves money.

```subtasks
[
  {"id": "s1", "role": "coder",
   "goal": "Implement src/sente/chat.py with parse_intent(text) as described in SPEC.md. Use regular expressions only. Money-moving intents must always return needs_confirmation True, and a message containing more than one money action must return action 'unknown' with reason 'multiple_actions'.",
   "read": ["SPEC.md", "tests/test_chat.py"],
   "acceptance": "python -m pytest -q"}
]
```
