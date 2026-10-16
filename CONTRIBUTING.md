# Contributing to FAST Channels on AWS

Thank you for helping. This guide covers the development setup, how changes flow into a release, the quality bar,
and how design decisions are recorded. By participating you agree to the [code of conduct](CODE_OF_CONDUCT.md).
Report security issues privately as described in [SECURITY.md](SECURITY.md), not as issues.

## Development setup

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.14), [Node.js 24](https://nodejs.org) and, for
anything that touches AWS, the AWS CLI v2 with credentials. Docker is optional.

```bash
git clone https://github.com/<you>/AWS-Fast-Channels.git && cd AWS-Fast-Channels
make install        # uv sync --all-packages; npm ci for the CDK CLI and the console
make help           # every target
```

| Target | What it runs |
|---|---|
| `make lint` | `ruff format --check`, `ruff check`, console ESLint |
| `make typecheck` | `mypy` (strict) on `core/src functions/src agent/src infra/src`, console `tsc -b` |
| `make test` | `pytest` (core, functions, agent, infra), console Vitest |
| `make synth` | Console build, then `cdk synth` with cdk-nag; fails on unacknowledged findings |
| `make check` | All of the above, what CI runs |
| `make diff`, `make deploy`, `make destroy` | Against your account; `PREFIX=<name>` for a separate copy |
| `make seed`, `make trace ID=…`, `make eval MODEL=…`, `make model-access` | Operator scripts |

The console also runs without AWS: `cd console && npm run dev:mock` (see [console/README.md](console/README.md)).

## Repository tour

| Package | Rules |
|---|---|
| `core/` (`fast_channels.core`) | Pure domain logic: pydantic and the standard library only, no boto3. 100 % line coverage |
| `functions/` (`fast_channels.functions`) | Lambda handlers; tests use botocore `Stubber` and real sample media |
| `agent/` (`fast_channels.agent`) | The AgentCore Runtime app; it must not import `fast_channels.core` |
| `infra/` (`fast_channels.infra`) | CDK constructs and stacks; assertion and snapshot tests, cdk-nag tests |
| `console/` | TypeScript strict, ESLint with zero warnings, Vitest and Playwright smoke tests |

`fast_channels` is a PEP 420 namespace across the four packages; there is one `uv.lock` for all of them.

## Branches and releases

```
feature/<topic>  ──PR──▶  develop  ──PR──▶  release/<major>.<minor>  ──PR──▶  main  ──tag──▶  vX.Y.Z
```

- Branch from `develop` as `feature/<topic>` (or `fix/<topic>`) and open a pull request to `develop`.
- A release is cut as `release/<major>.<minor>` from `develop`. Only fixes land there; they are merged back to
  `develop`.
- `release/*` is merged to `main` and tagged `vX.Y.Z` (semantic versioning). The GitHub release notes come from
  [CHANGELOG.md](CHANGELOG.md).
- Legacy lines `release/1.x` and `release/2.x` (SAM) are kept for reference and receive no changes.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org):

```
feat(schedule): re-anchor blocks after the preceding live program
fix(ingest): accept 59.94 fps sources on 60 Hz ladders
docs(agent): regenerate the tool reference table
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `perf`. Scopes are package or area names
(`core`, `ingest`, `schedule`, `channels`, `agent`, `console`, `infra`, `delivery`, …). Mark breaking changes with
`!` (`feat(config)!: …`) and a `BREAKING CHANGE:` footer. Keep commits focused.

## Quality bar

- Python 3.14 idioms: `match`, `X | Y` unions, `type` aliases, `StrEnum`, f-strings, `pathlib`. Small, pure functions
  where possible; precise docstrings; comments only where they explain *why*.
- `uv run ruff format --check`, `uv run ruff check` and `uv run mypy core/src functions/src agent/src infra/src` pass.
- New behaviour has tests. Infrastructure changes have CDK assertions, and cdk-nag findings are fixed or acknowledged
  on the construct that causes them, with a reason.
- Changing a CloudFormation snapshot is deliberate: `uv run pytest infra/tests/test_snapshots.py --snapshot-update`
  and review the diff.
- One source of truth per concept: when you change one of these, regenerate what derives from it in the same PR.

| Source | Derived | Regenerate |
|---|---|---|
| `fast_channels.core.catalog`, `.ingest`, `.events` | `contracts/*` | `uv run python scripts/generate_contracts.py` |
| `delivery.ROUTES` | `docs/path-contract.json` | `uv run pytest infra/tests/test_delivery.py --snapshot-update` |
| Tool registry | Tool table in `docs/agent.md` | The snippet in [docs/agent.md](docs/agent.md#tool-reference) |
| `FastChannelsConfig` | Configuration reference in `docs/operations.md` | From the field docstrings |
| `ladders/*.toml` | Ladder tables in `docs/encoding.md`, job template snapshot | `uv run pytest core/tests --snapshot-update` |
| `scripts/render_architecture.py` | `docs/images/architecture.png` | `uv run --group docs python scripts/render_architecture.py` (needs Graphviz) |
| `scripts/render_brand_assets.py` | `assets/brand/*.png` | `uv run python scripts/render_brand_assets.py` (changes every kit fingerprint) |

## Tests

- **Unit and infrastructure tests**: `uv run pytest` (all four packages) and `cd console && npm test`.
- **Console smoke tests**: `cd console && npx playwright install chromium && npm run test:e2e` (mock mode, strict
  CSP).
- **Model conformance**: `make eval MODEL=<bedrock model id>` before proposing a model for `agent.allowed_models`.
- **End-to-end tests** (`tests/e2e`, marker `e2e`) run against a deployed stage: the stack-output contract; an
  upload with a sidecar through MediaConvert to a VOD source; the default ladder's kit as MediaTailor reads it;
  programs published through the tools, then SSAI, SGAI and DASH sessions watching the breaks air with ads from the
  second break on; the agent over AG-UI with the autopilot's token; and stopping the channel (also done at teardown,
  whatever failed). One run id is the correlation id of everything, so `scripts/trace.py <run id>` shows the run.
  Run them with `uv run pytest tests/e2e -m e2e --prefix <prefix>`, or through the manual **End-to-end** workflow
  (`.github/workflows/e2e.yml`, AWS access through an OIDC role), which deploys a throwaway stage, runs the tests and
  destroys it. A run costs a few US dollars.
- **Release gate**: a release is tagged only after a green end-to-end run on the release branch.

When you experiment in AWS, use a prefix of your own (`make deploy PREFIX=<you>-exp`) so that your stacks never collide
with someone else's, and destroy them when you are done.

## Architecture decision records

Significant decisions are recorded in [docs/adr/](docs/adr/README.md), one file per decision:
`NNNN-short-title.md` with **Status**, **Context**, **Decision** and **Consequences**, and evidence where there is any
(measurements, live tests). To change a decision, add a new record that supersedes the old one, mark the old one
`Superseded by NNNN`, and update the index. Propose an ADR in the same pull request as the change it explains.

## Pull requests

Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md): what and why, how it was verified (at least
`make check`; deployed if it changes AWS behaviour), and which docs changed. Keep the README and `docs/` accurate to
the code: a pull request that changes behaviour updates them.
