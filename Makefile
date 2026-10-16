# FAST Channels on AWS — developer and operator tasks. Run `make help`.
SHELL := bash
.DEFAULT_GOAL := help
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION ?= 1

CDK := npx cdk
PREFIX_ARG := $(if $(PREFIX),-c fast-channels.prefix=$(PREFIX))

.PHONY: help install console check lint typecheck test synth diff deploy destroy seed model-access eval trace

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install Python (uv), CDK CLI and console dependencies
	uv sync --all-packages
	npm ci
	cd console && npm ci

console: ## Build the operator console (console/dist), required before synth/deploy
	cd console && npm run build

lint: ## Format check and lint (Python + console)
	uv run ruff format --check
	uv run ruff check
	cd console && npm run lint

typecheck: ## Static type checks (mypy strict + tsc)
	uv run mypy core/src functions/src agent/src infra/src
	cd console && npm run typecheck

test: ## Unit and infrastructure tests (Python + console)
	uv run pytest
	cd console && npm test

check: lint typecheck test synth ## Everything CI runs

synth: console ## Synthesize all stacks (runs cdk-nag); PREFIX=name to override the deployment prefix
	$(CDK) synth --quiet $(PREFIX_ARG)

diff: console ## Show pending changes against the deployed stacks
	$(CDK) diff '**' $(PREFIX_ARG)

deploy: console ## Deploy every stack (about 25 minutes the first time)
	$(CDK) deploy '**' --require-approval never --concurrency 2 $(PREFIX_ARG)

destroy: ## Delete every stack and the runtime resources the pipeline and agent created
	$(CDK) destroy '**' --force $(PREFIX_ARG)

model-access: ## Check (and optionally request) Amazon Bedrock model access for the agent
	uv run python scripts/check_model_access.py

seed: ## Ingest the open-licensed sample titles and load the reference catalog
	uv run python scripts/seed_sample_content.py $(if $(PREFIX),--prefix $(PREFIX))

eval: ## Run the agent model-conformance suite (MODEL=<bedrock model id>)
	uv run python scripts/eval_model.py --model-id $(or $(MODEL),us.anthropic.claude-haiku-5-5)

trace: ## Find every log line for a correlation id (ID=<asset id, session id or execution name>)
	uv run python scripts/trace.py $(ID) $(if $(PREFIX),--prefix $(PREFIX))
