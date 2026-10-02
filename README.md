# quota-balancer

[Oh My Pi](https://omp.sh) plugin. Reads live `omp usage` and writes `modelRoles`, `task.agentModelOverrides`, and `retry.fallbackChains` onto the provider pools that still have room.

OMP keeps the role you pinned and walks `retry.fallbackChains` only after a quota error ([routing](https://github.com/can1357/oh-my-pi/blob/main/README.md), [retry policy](https://github.com/can1357/oh-my-pi/blob/main/docs/non-compaction-retry-policy.md)). This plugin picks the assignment first. Credential round-robin and `omp usage` do not move a role between providers.

## Install

OMP and Python 3. PyYAML only when writing config:

```sh
pip install pyyaml
```

```sh
git clone https://github.com/snattydev/quota-balancer.git
cd quota-balancer
omp install .
```

`omp install .` is [`omp plugin link`](https://github.com/can1357/oh-my-pi/blob/main/docs/plugin-manager-installer-plumbing.md): this checkout is symlinked into the plugin set. `package.json` field `omp.extensions` loads `quota-balancer.ts`. The skill loads from `skills/quota-balancer/SKILL.md` ([plugin layout](https://github.com/can1357/oh-my-pi/blob/main/docs/skills/authoring-marketplaces.md)). Restart `omp`. The footer badge is `quota:`.

The link has to point at this tree. `scripts/balance.py` is resolved from the extension's real path.

## Use

```sh
python3 scripts/balance.py
python3 scripts/balance.py --apply
python3 scripts/balance.py --profile claude-first --apply
python3 scripts/balance.py --json
```

| Profile | When | Spends |
| --- | --- | --- |
| `grok-power` | Included frontier pool under 80% | Newest frontier model on that pool |
| `claude-first` | Included pool at least 80%, a Claude pool still open | Claude for slow, plan, and review |
| `balanced` | Included pool 80–90%, Claude exhausted | Named fast pool for routine work, frontier for slow work |
| `conserve-cursor` | Included pool at or above 90% | Routine roles leave the included pool |

`conserve-included` is the same profile. Percentage windows count as full at 90%. USD caps at 95%.

In a session: `/rebalance`, `/quota`, `/quota on`, `/quota off`, `/quota status`, `/quota refresh`. `/quota` and `/quota on` open the detail panel. `/quota off` hides the panel and leaves the badge.

`--apply` and `/rebalance` rewrite the active config (`omp config path`, or `PI_CODING_AGENT_DIR`). Comments in that file are dropped. Role keys this plugin does not set are kept. `retry.fallbackChains` is replaced. The open session keeps the chains it loaded at startup. Start a new session after applying.

## Pools

Provider ids come from the usage report. A model is matched only to pools on its own provider. A family named by any report is metered on providers that do not name it, so a subscription model is not copied onto another provider's paid bucket. Selectors are `provider/id` or `provider/id:effort`, and only when that id is in the catalog snapshot. A missing model is omitted.

A reported remainder is not admission. A provider can still return 429 while its window looks open. The fallback chain absorbs that.

## Contributing

English. Commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/). Feature work lands through a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Not part of [Oh My Pi](https://github.com/can1357/oh-my-pi).
