---
name: quota-balancer
description: "Read live Oh My Pi provider quotas and rebalance model roles, subagents, and fallback chains onto the pools that still have room."
---

# Quota & Model Balancer

This skill reads `omp usage --json`, matches catalog models to each provider's own pools, and updates `modelRoles`, `task.agentModelOverrides`, and `retry.fallbackChains` in the active OMP config (`omp config path`, or `PI_CODING_AGENT_DIR`).

Provider ids are whatever the usage report and the model catalog returned. Do not hardcode them. Do not write a selector that is not in the loaded catalog.

## How a reading is used

1. Every usage report becomes one provider. Each limit is a pool: a named family window, an included pool, a metered pool, or a general pool. Percentage windows and USD caps both count.
2. A model is covered by pools on its own provider. Named labels win. Families that any provider meters by name use the metered pool everywhere else, so a subscription model is not copied onto another provider's paid bucket.
3. Roles follow remaining room:
   - Included frontier pool under 80%: `grok-power` (newest frontier model on that pool).
   - Otherwise, if a Claude pool is open: `claude-first`.
   - Otherwise, included pool under 90%: `balanced`.
   - Otherwise: `conserve-cursor` (reserve the included pool). `conserve-included` is the same profile.
4. Runtime fallbacks live in `retry.fallbackChains`. A named-pool model does not fall through to the same id on a metered pool. If the frontier model and the roomiest Claude share a provider, frontier fallbacks try another provider's Claude first.
5. Dashboard remainder is not admission. A 429 can still arrive while a window looks open. The chain is what absorbs it.
6. `--apply` and `/rebalance` write config only. The open session does not reload those chains. Start a new OMP session after applying.

Do not put an opt-in custom gateway into these profiles. Models that are not in the catalog snapshot are skipped.

## Usage

The extension paints a footer badge (`● ⚖ quota: PROFILE`) on startup. The detail panel stays off until `/quota` or `/quota on`. `/quota off` hides the panel and leaves the badge. `/quota status` repeats the summary. `/rebalance` reads quotas again and rewrites config.

Run from the repository root (`scripts/balance.py` lives next to `quota-balancer.ts`):

```bash
python3 scripts/balance.py
python3 scripts/balance.py --apply
python3 scripts/balance.py --profile claude-first --apply
python3 scripts/balance.py --profile grok-power --apply
python3 scripts/balance.py --profile balanced --apply
python3 scripts/balance.py --profile conserve-cursor --apply
```
