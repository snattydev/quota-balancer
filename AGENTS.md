# AGENTS.md

Instructions for an agent installing or changing quota-balancer, an Oh My Pi extension. Human setup and the reason this exists are in `README.md`.

## Install

Do not hardcode a home directory. The agent directory is `omp config path`. `PI_CODING_AGENT_DIR` overrides it.

From the repository root:

```sh
omp install .
```

That is `omp plugin link`. It symlinks this tree into the plugin set, loads `quota-balancer.ts` from `package.json` `omp.extensions`, and discovers `skills/quota-balancer/SKILL.md`. The extension resolves `scripts/balance.py` from its own real path, so the link must point at this tree, not at a copied `.ts` file with no `scripts/` beside it.

Restart OMP after linking. Confirm the footer badge `quota:` on the next session.

PyYAML is required only when writing config:

```sh
pip install pyyaml
```

## Operate

```sh
python3 scripts/balance.py
python3 scripts/balance.py --json
python3 scripts/balance.py --profile grok-power|claude-first|balanced|conserve-cursor --apply
```

`conserve-included` is an alias of `conserve-cursor`.

`--apply` and `/rebalance` write the active `config.yml` (or `config.yaml` if that is the only file). They do not reload the open session. Tell the user to start a new session.

The extension passes the in-process model catalog on stdin. A CLI run with no stdin uses `omp models --json`. Never write a selector that is not in that snapshot. `provider/id:effort` is valid only when the model lists that effort.

## Rules when changing the balancer

- Provider ids come from `omp usage --json` (`reports[].provider`) and from the catalog. Do not add a branch for a specific provider name.
- Balance each provider on its own pools. A limit covers a model only on that same provider.
- Named family pools win over included and metered pools. Families named by any report are metered on providers that do not name them.
- Do not map a named-pool model onto the same id under another provider's metered pool.
- If the frontier model and the best Claude share a provider, frontier fallbacks should try another provider's Claude first.
- Drop empty selectors. An unknown id makes OMP warn at startup.
- Keep the config path on `agent_dir()` in `scripts/balance.py`.

## Check

```sh
python3 -m unittest discover -s tests -v
python3 scripts/balance.py
```

The dry run must list every provider in the usage report, and it must not write config. Use `--apply` only when the user asked to update their assignment.

## Changes

English. Conventional Commits: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. Imperative subject, no trailing period, at most 72 characters. One concern per commit. Feature work uses a branch and a pull request to `main`, as in `CONTRIBUTING.md`. Push `main` only when the user asked.

## Do not commit

`config.yml`, `agent.db`, auth files, `.env`, or anything under the OMP agent directory. This repository is the plugin only.
