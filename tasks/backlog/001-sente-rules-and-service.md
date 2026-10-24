project: sente
budget_tokens: 300000
---
# Sente core: rules and ledger service
Build the two modules the partner briefing describes. SPEC.md is the source of truth and the tests are the definition of done.

```subtasks
[
  {"id": "s1", "role": "coder",
   "goal": "Implement src/sente/rules.py exactly as specified in SPEC.md (constants, PremiumSplit, Settlement, split_premium, commission_cum, settle_lot). Use integer maths only.",
   "read": ["SPEC.md", "tests/test_rules.py"],
   "acceptance": "python -m pytest -q tests/test_rules.py"},
  {"id": "s2", "role": "coder",
   "goal": "Implement src/sente/service.py (Sente class, Lot dataclass, InsufficientFunds, EscrowError) as specified in SPEC.md, using sente.rules. In-memory state is fine for now; sqlite3 is optional.",
   "read": ["SPEC.md", "src/sente/rules.py", "tests/test_service.py"],
   "acceptance": "python -m pytest -q tests/test_service.py"}
]
```
