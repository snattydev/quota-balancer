# Contributing

English for issues, pull requests, commit messages, code, and comments.

This is a small [Oh My Pi](https://omp.sh) plugin. Keep a change to one behavior. Provider ids stay data from `omp usage` and the model catalog, not new branches in the balancer.

## Features

1. Open a feature request and describe the behavior you want. A one-line fix can skip the issue.
2. Branch from `main`: `feat/<short-name>` or `fix/<short-name>`.
3. Open one pull request to `main`. The template is [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).

Do not open a pull request that mixes a feature with unrelated cleanup.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) 1.0.0:

```
<type>(<scope>): <subject>

<why, when the subject is not enough>
```

- Subject is imperative, lowercase, and has no trailing period. Stay within 72 characters.
- Types used here: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.
- Scope is optional: `pools`, `skill`, `readme`.
- A breaking assignment change uses `feat!:` or a `BREAKING CHANGE:` footer.

```
feat(pools): skip a model that is missing from the catalog

fix: keep role keys this plugin does not set
```

One concern per commit. Do not commit `config.yml`, `agent.db`, auth files, or `.env`.

## Develop

OMP and Python 3. From a checkout of this repository:

```sh
omp plugin link .
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 scripts/balance.py
```

`omp plugin link .` is the same link `omp install .` creates. `package.json` `omp.extensions` loads `quota-balancer.ts`. The skill is `skills/quota-balancer/SKILL.md`. Restart `omp` after linking.

`--apply` rewrites the active agent config and drops comments in that file. Run it only on a config you intend to replace. The open session does not reload the new chains.

## Plugin shape

Follow the [OMP plugin layout](https://github.com/can1357/oh-my-pi/blob/main/docs/skills/authoring-marketplaces.md): extension entry in `package.json`, skill under `skills/<name>/SKILL.md`. The extension resolves `scripts/balance.py` from its own real path, so a linked checkout has to be this tree.
