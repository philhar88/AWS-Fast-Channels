# 0001 — Python CDK app in a uv workspace

**Status:** Accepted, amended by [0020](0020-demand-through-the-prebid-server-guidance.md) (a JVM fleet for the demand module)

## Context
v1 and v2 were SAM templates with Python Lambda functions. v3 adds an agent (Python, Strands Agents), a pure domain
library shared by infrastructure and runtime code, and many Lambda functions. CDK supports Python as a first-class
language; most AWS samples use TypeScript CDK with Python functions, which means two toolchains for one team.

## Decision
- The CDK app is Python (`aws-cdk-lib` 2.273, constructs pinned in `uv.lock`). Runtime code is Python 3.14 on arm64.
- One uv workspace with four PEP 420 namespace packages: `fast_channels.core` (pure domain), `.functions` (Lambda
  handlers), `.agent` (AgentCore Runtime app), `.infra` (constructs and stacks). One lock file for everything.
- Lambda code is bundled without Docker (`uv export` + `uv pip install --python-platform aarch64-manylinux2014`), with
  the asset hash computed from the bundle output. Docker is a fallback, not a requirement.
- The browser console is the only TypeScript.

## Consequences
- One language for infrastructure, functions, agent and tests; the ladder model that compiles MediaConvert job templates
  at synth time is the same code that validates agent ladder proposals at runtime.
- The constructs are consumable from Python CDK apps (copy or install the packages). They are not published through
  jsii to other CDK languages; a TypeScript app would need to wrap or port them.
- CDK still needs Node.js for the CLI and jsii runtime; the CLI is pinned in the root `package.json`.
- **Amended by [0020](0020-demand-through-the-prebid-server-guidance.md):** the optional demand module adds a JVM
  fleet, Prebid Server Java on x86 Fargate (the guidance's image and its jlinked runtime are x86), and one Java
  module of ours, `pbs/fast-ctv`. CI builds it with Amazon Corretto 21 and Maven 3.9.11 on an x86 runner, against
  the pinned Prebid Server Java release, and runs its JUnit and contract tests (`pbs-module`). Everything else stays
  Python.
