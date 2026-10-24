# Project: Sente prototype

Prepaid digital cash for Uganda. One premium at purchase covers unlimited free transfers and the eventual cash-out. Full background is in the partner briefing; the working spec is `SPEC.md`.

## Decisions so far
- Prototype uses test money only and the Python standard library only.
- Rules and tests were written from the partner briefing numbers (5,000 UGX purchase, 200 UGX premium).
- The cap applies to the service margin, not the exit credit, so large purchases still fund their own cash-outs.
- Parsing of chat text is deterministic; a language model may only produce text that goes through `parse_intent` and a confirmation step.

## Placeholders to revisit
Premium rate, margin cap, minimum mint, retention checkpoint (365 days), rebate percentage.

## Notes from BANTU runs
(BANTU may append short dated notes here.)
