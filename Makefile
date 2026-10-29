# FAST Channels on AWS — developer and operator tasks. Run `make help`.
SHELL := bash
.DEFAULT_GOAL := help
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION ?= 1

CDK := npx cdk

# STAGE=<name> applies stages/<name>.json (a partial fast-channels block: prefix, modules, ...) as CDK context and
# targets the operator scripts at its prefix; PREFIX=<name> alone deploys cdk.json under another prefix, and with
# STAGE replaces the stage's prefix. A stage that cannot be read stops make before any command runs.
ifdef STAGE
STAGE_OPTS := $(STAGE) $(if $(PREFIX),--prefix $(PREFIX)) $(if $(MODULES),--modules $(MODULES))
STAGE_CONTEXT := $(shell uv run python scripts/stage_args.py context $(STAGE_OPTS) || echo STAGE-ERROR)
ifneq ($(findstring STAGE-ERROR,$(STAGE_CONTEXT)),)
$(error STAGE=$(STAGE): see the error above)
endif
STAGE_PREFIX := $(shell uv run python scripts/stage_args.py prefix $(STAGE_OPTS) || echo STAGE-ERROR)
ifneq ($(findstring STAGE-ERROR,$(STAGE_PREFIX)),)
$(error STAGE=$(STAGE): see the error above)
endif
CONTEXT_ARGS := $(STAGE_CONTEXT)
PREFIX_OPT := --prefix $(STAGE_PREFIX)
else
CONTEXT_ARGS := $(if $(PREFIX),-c fast-channels.prefix=$(PREFIX))
PREFIX_OPT := $(if $(PREFIX),--prefix $(PREFIX))
endif

.PHONY: help install console check lint typecheck test synth diff deploy destroy as-run-router account-baseline seed \
	model-access eval trace pbs-test pbs-contract deploy-demand destroy-demand

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo "  STAGE=<name> (stages/<name>.json) or PREFIX=<name> selects the deployment of synth, diff, deploy,"
	@echo "  destroy, as-run-router, account-baseline, seed and trace."

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
	MYPYPATH=scripts:tests/e2e uv run mypy scripts tests/e2e
	MYPYPATH=tests/pbs uv run mypy tests/pbs
	cd console && npm run typecheck

test: ## Unit and infrastructure tests (Python + console)
	uv run pytest
	cd console && npm test

check: lint typecheck test synth ## Everything CI runs

synth: console ## Synthesize all stacks (runs cdk-nag)
	$(CDK) synth '**' --quiet $(CONTEXT_ARGS)

diff: console ## Show pending changes against the deployed stacks
	$(CDK) diff '**' $(CONTEXT_ARGS)

deploy: console ## Deploy every stack (about 20 minutes the first time)
	$(CDK) deploy '**' --require-approval never --concurrency 2 $(CONTEXT_ARGS)

destroy: ## Delete every stack and the runtime resources the pipeline and agent created
	$(CDK) destroy '**' --force $(CONTEXT_ARGS)

as-run-router: console ## Deploy the account's as-run router for stages with observability.as_run shared (ADR-0032)
	$(CDK) deploy '*/AsRun' --exclusively --require-approval never -c as-run-router=true $(CONTEXT_ARGS)

account-baseline: console ## Turn CloudWatch Transaction Search on for the account (transaction_search: enable)
	$(CDK) deploy '*/AccountBaseline' --exclusively --require-approval never -c account-baseline=true $(CONTEXT_ARGS)

model-access: ## Check (and optionally request) Amazon Bedrock model access for the agent
	uv run python scripts/check_model_access.py

seed: ## Ingest the open-licensed sample titles and load the reference catalog (SEED_ARGS="--wait --yes")
	uv run python scripts/seed_sample_content.py $(PREFIX_OPT) $(SEED_ARGS)

eval: ## Run the agent model-conformance suite (MODEL=<bedrock model id>; TASK=scheduler.plan writes its eval record)
	uv run python scripts/eval_model.py --model-id $(or $(MODEL),us.anthropic.claude-haiku-5-5) $(if $(TASK),--task $(TASK))

trace: ## Find every log line for a correlation id (ID=<asset id, session id or execution name>)
	uv run python scripts/trace.py $(ID) $(PREFIX_OPT)

DEMAND_ARGS := $(if $(STAGE),--stage $(STAGE)) $(if $(PREFIX),--prefix $(PREFIX)) $(if $(MODULES),--modules $(MODULES))

deploy-demand: console ## Deploy the demand module of STAGE: its stacks, the guidance's bootstrap, image and stacks; smoke
	uv run python scripts/prebid_guidance.py deploy $(DEMAND_ARGS) $(DEMAND_OPTS)

destroy-demand: ## Destroy the demand module of STAGE, with what the guidance retains; then list what is left
	uv run python scripts/prebid_guidance.py destroy $(DEMAND_ARGS) $(DEMAND_OPTS)

pbs-test: ## Build Prebid Server Java with the fast-ctv module, as the guidance's image does, and run its JUnit tests
	uv run python scripts/pbs_module.py build
	uv run python scripts/pbs_module.py test

pbs-contract: ## Contract tests against the jar from pbs-test (no-hook matrix, header isolation, wire fields)
	uv run pytest tests/pbs -m pbs
