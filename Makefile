# ==============================================================================
# Makefile - developer entrypoint
#
# WHAT THIS IS, AND WHY IT IS NOT THE ONLY ENTRYPOINT
#
# Every target here is a thin wrapper that delegates to the project's own
# developer CLI (`python -m knowledge_assistant.interfaces.cli`, installed as
# `ka` after `make install`). There is no command logic in this file.
#
# That is deliberate. The Phase 1 brief asks for a `make up` experience, but
# `make` is absent from most Windows shells without a Unix toolchain, and a
# developer workflow that cannot be followed on the reviewer's machine is not a
# reproducible environment - it is a local convention. So the CLI carries the
# implementation and this file carries the discoverability. Both call the same
# functions in src/knowledge_assistant/interfaces/cli.py.
#
# Two command-line spellings, one implementation:
#
#     make test                                  # this file
#     python -m knowledge_assistant.interfaces.cli test   # no `make` needed
#
# VIRTUALENV HANDLING
#
# These targets work whether or not you have activated the virtualenv, because a
# Makefile that only works inside an activated shell is a Makefile that quietly
# runs against the wrong interpreter. PYTHON resolves to .venv when it exists,
# and the venv's bin directory is prepended to PATH for every recipe so that
# tools resolved by name - including the `which` checks inside the CLI - find the
# virtualenv's copies rather than anything global.
#
# Override PYTHON to run against a different interpreter:
#
#     make test PYTHON=python3.13
#
# NO CLEAN TARGET
#
# Deliberately absent. Deleting directories is not a step a new contributor should
# reach for while learning a repository, and every artefact it would remove is
# already gitignored and regenerable. Remove one by hand when you actually mean
# to.
# ==============================================================================

SHELL := /usr/bin/env bash
# -e aborts a recipe at the first failing command; pipefail stops a piped exit
# code from being hidden by the last stage. A target that half-succeeds and
# prints a green summary is worse than one that stops.
.SHELLFLAGS := -e -o pipefail -c
.DEFAULT_GOAL := help

VENV := .venv

ifeq ($(OS),Windows_NT)
  VENV_BIN := $(VENV)/Scripts
else
  VENV_BIN := $(VENV)/bin
endif

# Prefer the virtualenv interpreter if one exists, otherwise fall back to the
# platform's default. Resolved with $(wildcard) rather than an existence test
# shell-out so `make -n` stays side-effect free.
PYTHON ?= $(if $(wildcard $(VENV_BIN)/python.exe),$(CURDIR)/$(VENV_BIN)/python.exe,$(if $(wildcard $(VENV_BIN)/python),$(CURDIR)/$(VENV_BIN)/python,python3))

# Prepended so `ruff` and `mypy` resolve inside the virtualenv even from an
# unactivated shell. The project package itself is made importable by
# PYTHONPATH, because `make test` must work before anyone has run `make install`.
export PYTHONPATH := $(CURDIR)/src
export PATH := $(CURDIR)/$(VENV_BIN):$(PATH)

# The project's own CLI. This is the supported interface; the targets below are
# aliases, not reimplementations.
KA := $(PYTHON) -m knowledge_assistant.interfaces.cli

COMPOSE := docker compose


# ==============================================================================
# HELP
# ==============================================================================

.PHONY: help
help: ## Show this help
	@printf 'Knowledge Assistant - developer targets\n\n'
	@printf 'Equivalent to any target:  $(PYTHON) -m knowledge_assistant.interfaces.cli <command>\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@printf '\nFull documentation: docs/13-deployment/LOCAL_DEVELOPMENT.md\n'
	@printf 'Note: `make up` requires a running Docker daemon. Without it, the database\n'
	@printf 'cannot start; see `make help` targets that do not need one.\n'


# ==============================================================================
# ENVIRONMENT SETUP
# ==============================================================================

.PHONY: install
install: ## Create the virtualenv and install the project with dev dependencies
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"
	@printf '\nInstalled. The `ka` command is now on the virtualenv PATH.\n'
	@printf 'Next: make up   (needs a running Docker daemon)\n'

.PHONY: install-lock
install-lock: ## Install the exact pinned versions from requirements.lock
	$(PYTHON) -m pip install --requirement requirements.lock
	@printf '\nInstalled pinned dependency set.\n'
	@printf 'This does NOT install the project itself; run `make install` for the `ka` command.\n'


# ==============================================================================
# LOCAL STACK  (requires a running Docker daemon)
# ==============================================================================

.PHONY: up
up: ## Start PostgreSQL + pgvector and wait for it to report healthy
	$(KA) up

.PHONY: down
down: ## Stop the stack and delete its volumes
	$(KA) down

.PHONY: logs
logs: ## Follow stack logs
	$(KA) logs

.PHONY: serve
serve: ## Run the API in the foreground (host process)
	$(KA) serve

.PHONY: worker
worker: ## Run the worker in the foreground (host process)
	$(KA) worker


# ==============================================================================
# DATABASE MIGRATIONS
# ==============================================================================

.PHONY: migrate
migrate: ## Apply migrations to KA_DATABASE_URL
	$(KA) migrate

.PHONY: migration-status
migration-status: ## Show which revision the database is currently at
	$(PYTHON) -m alembic current

.PHONY: migration-sql
migration-sql: ## Render migrations to SQL without connecting (reviewable diff)
	$(PYTHON) -m alembic upgrade head --sql


# ==============================================================================
# QUALITY GATES
#
# `check` is the gate CI must agree with. It delegates to `ka check`, which runs
# lint -> typecheck -> architecture -> tests in cheapest-first order so a
# formatting error surfaces in seconds instead of after the full suite.
# ==============================================================================

.PHONY: test
test: ## Run the test suite
	$(KA) test

.PHONY: lint
lint: ## Run ruff lint and ruff format check
	$(KA) lint

.PHONY: format-check
format-check: ## Verify formatting without writing changes
	$(PYTHON) -m ruff format --check .

.PHONY: format
format: ## Apply ruff formatting
	$(PYTHON) -m ruff format .

.PHONY: typecheck
typecheck: ## Run mypy in strict mode
	$(KA) typecheck

.PHONY: arch
arch: ## Enforce the module dependency rules
	$(KA) arch

.PHONY: check
check: ## Run every non-container gate: lint, types, architecture, tests
	$(KA) check

.PHONY: openapi
openapi: ## Verify the committed OpenAPI document matches the application
	$(PYTHON) scripts/openapi_artifact.py

.PHONY: openapi-write
openapi-write: ## Rewrite the OpenAPI document (an intentional API contract change)
	$(PYTHON) scripts/openapi_artifact.py --write

# NOTE: this target currently exits non-zero. It delegates to `scripts/
# definition_of_done.py`, which is the Phase 1 "machine-checkable definition of
# done" deliverable and has not been written yet. It is wired up now, while the
# calling convention is fresh, so that the script lands with a target already
# waiting for it rather than with an entry point still to be invented. Do not
# treat its failure as a regression.
.PHONY: dod
dod: ## Run the machine-checkable Phase 1 definition-of-done verification
	$(KA) dod