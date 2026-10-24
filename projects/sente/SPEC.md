# Sente prototype: specification

Goal: a runnable prototype of the Sente prepaid digital cash model, for partner demos. Test money only.

## Hard constraints
- Python 3.10+, **standard library only** (sqlite3 allowed). No network access, no third-party packages.
- All money is an integer number of UGX. Never use floats for money.
- Source code lives in `src/sente/`. Tests live in `tests/` and are **read-only**: never edit them. Make the code pass them.
- Keep code small and readable. Type hints on public functions. No unused code.

## Modules
### `sente/rules.py` (pure functions, no state)
Constants (placeholders, to be tuned later):
`PREMIUM_RATE_BP=300`, `PREMIUM_MIN=200`, `EXIT_RATE_BP=120`, `EXIT_MIN=100`, `MARGIN_CAP=5_000`, `REBATE_PCT=20`, `MIN_MINT=2_000`, `CHECKPOINT_DAYS=365`.

- `split_premium(amount) -> PremiumSplit(exit_credit, margin)` (a `NamedTuple` with a `.total` property).
  - `total_uncapped = max(PREMIUM_MIN, round_half_up(amount * 3%))`
  - `exit_credit = max(EXIT_MIN, round_half_up(amount * 1.2%))`
  - `margin = min(MARGIN_CAP, total_uncapped - exit_credit)`
  - The cap applies to the service margin only. The exit credit always stays proportional.
  - Raise `ValueError` if `amount < MIN_MINT`.
  - Use integer maths: `round_half_up(amount * bp / 10000) = (amount * bp + 5000) // 10000`.
- `commission_cum(amount, exit_credit, exited) -> int` = `(exit_credit * exited + amount // 2) // amount`.
  Cumulative rounding: a lot's total commission over all exits always equals its exit credit.
- `settle_lot(amount, exit_credit, margin, exited) -> Settlement(agent_paid, rebate, margin_recognized, exit_credit_reserved)`
  - `premium = exit_credit + margin`, `retained = amount - exited`
  - `agent_paid = commission_cum(amount, exit_credit, exited)`
  - `rebate = (premium * retained * REBATE_PCT + amount * 50) // (amount * 100)`, then `min(rebate, margin)`
  - `margin_recognized = margin - rebate`; `exit_credit_reserved = exit_credit - agent_paid`
  - Raise `ValueError` if `exited` is outside `0..amount`.

### `sente/service.py` (the prototype ledger)
`class Sente(db_path=":memory:", clock=None)`; `clock` is a callable returning a timezone-aware `datetime` (default: now, UTC).
Exceptions: `InsufficientFunds`, `EscrowError`.
Money is conserved. `audit()` must always satisfy `bank == wallets + escrow + exit_pool + deferred_margin + revenue`.

| Method | Behaviour |
|---|---|
| `register(name)` | Create a wallet (idempotent). |
| `balance(name) -> int` | Raises `KeyError` for unknown names. |
| `mint(buyer, amount) -> Lot` | Uses `split_premium`. Bank receives `amount + premium`. Buyer wallet gets `amount`. `exit_credit` goes to the exit pool, `margin` to deferred margin. Creates a `Lot`. |
| `transfer(src, dst, amount, idem_key=None)` | Free and instant. `ValueError` if amount <= 0, `InsufficientFunds`, `KeyError`. Same `idem_key` twice does nothing the second time. |
| `request_exit(holder, agent, amount) -> escrow_id` | Locks the holder's Sente in escrow. Same validation as transfer. Status `"locked"`. |
| `confirm_exit(escrow_id, by) -> status` | `by` is `"agent"` or `"holder"` (else `ValueError`). When both have confirmed, release. Confirming twice is harmless. Confirming a cancelled escrow raises `EscrowError`. Returns `"locked"` or `"released"`. |
| `cancel_exit(escrow_id) -> "cancelled"` | Refunds the holder. Idempotent. `EscrowError` if already released. |
| `escrow_status(escrow_id) -> str` | `"locked"`, `"released"` or `"cancelled"`. |
| `agent_redeem(agent, amount)` | Agent turns Sente into cash: wallet and bank both decrease. |
| `run_checkpoint() -> list[dict]` | For each unsettled lot older than `CHECKPOINT_DAYS`: use `settle_lot`, pay `rebate` into the buyer's wallet from deferred margin, move the rest of the lot's margin to revenue, mark settled. Returns dicts with `lot_id`, `buyer`, `rebate`, `margin_recognized`. Running it twice does nothing the second time. |
| `lots() -> list[Lot]` | Ordered by creation time then id. |
| `audit() -> dict` | Keys: `bank`, `wallets`, `escrow`, `exit_pool`, `deferred_margin`, `revenue`. |

`Lot` is a dataclass: `id, buyer, amount, premium, exit_credit, margin, created_at, exited=0, settled=False`.

Release of an escrow (both confirmed):
1. Consume the escrow amount from lots **oldest first** (global FIFO), increasing each lot's `exited`. If the lots have less remaining than the amount, ignore the surplus (those coins were already prepaid, for example rebates or commissions).
2. Agent commission = sum over touched lots of `commission_cum(after) - commission_cum(before)`. Pay it from the exit pool to the agent wallet, in addition to the escrowed amount.
3. A lot that becomes fully exited and is not yet settled has its whole margin moved from deferred margin to revenue now (no rebate), and is marked settled.

### `sente/chat.py`
`parse_intent(text) -> dict`: deterministic, regex-based, never moves money. Money-moving intents always carry `needs_confirmation: True`; text can never change that. See `tests/test_chat.py` for the exact shapes. Amounts accept `1000`, `1,000`, `5k`, `1.5k` (no space before `k`).

### Later (not part of the first tasks)
`sente/api.py` (stdlib `http.server`, JSON endpoints wrapping `Sente`) and a one-page HTML demo UI in `web/index.html`.

## Done means
`python -m pytest -q` passes with no edits to `tests/`.
