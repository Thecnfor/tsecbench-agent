# Multi-Stage Workflow

## State ledger
1. After every stage, write down: assets, credentials, accepted endpoints, failed vectors.
2. Never restart from zero: feed the ledger into the next stage's first command.

## Pivot discipline
1. A flag is a pivot, not the end. Re-run service discovery from the current foothold.
2. Test the newest credential against every previously discovered endpoint before new recon.
3. If two stages fail on the same surface, change surface: another host, port, or role.

## Time budget
1. Short slice: one high-information command at a time; skip broad directory sweeps.
2. Long slice: after a foothold, spend time on lateral reachability, not on re-fingerprinting.
