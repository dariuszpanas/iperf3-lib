.DEFAULT_GOAL := help

UV ?= uv
PYTHON_BASE ?= python:3.12-slim
IPERF3_VERSION ?= 3.21
DOCKER_IMAGE ?= iperf3-lib-test:local
REVISION ?= HEAD
RANGE ?= origin/main...HEAD

.PHONY: help install test test-cov test-integration lint format format-check type-check policy-check commit-check change-check workflow-lint check ci build clean docker-build docker-test docker-shell all

help:
	@echo "Available targets:"
	@echo "  install          - Sync the frozen development environment"
	@echo "  test             - Run tests that are not marked integration"
	@echo "  test-cov         - Run non-integration tests with coverage"
	@echo "  test-integration - Run integration tests (requires libiperf and iperf3)"
	@echo "  lint             - Run Ruff lint checks"
	@echo "  format           - Apply Ruff formatting and safe lint fixes"
	@echo "  format-check     - Check formatting without changing files"
	@echo "  type-check       - Run ty against the package"
	@echo "  policy-check     - Run YAGA workflow and repository policies (REVISION=HEAD)"
	@echo "  commit-check     - Check one commit message (REVISION=HEAD)"
	@echo "  change-check     - Require tests with source changes (RANGE=origin/main...HEAD)"
	@echo "  workflow-lint    - Run YAGA actionlint checks (requires Docker)"
	@echo "  check            - Run all non-mutating static checks"
	@echo "  ci               - Run quality/unit checks, workflow lint, and Docker tests"
	@echo "  build             - Build and validate wheel/sdist artifacts"
	@echo "  docker-build     - Build the Docker test image"
	@echo "  docker-test      - Build the image and run the full suite"
	@echo "  docker-shell     - Open a shell in the Docker test image"
	@echo "  clean            - Remove local build and test artifacts"

install:
	$(UV) sync --frozen --dev

test:
	$(UV) run --frozen pytest -m "not integration"

test-cov:
	$(UV) run --frozen pytest -m "not integration" --cov=iperf3_lib --cov-report=term-missing --cov-report=xml

test-integration:
	$(UV) run --frozen pytest -m integration

lint:
	$(UV) run --frozen ruff check src tests scripts

format:
	$(UV) run --frozen ruff format src tests scripts
	$(UV) run --frozen ruff check --fix src tests scripts

format-check:
	$(UV) run --frozen ruff format --check src tests scripts

type-check:
	$(UV) run --frozen ty check src scripts

policy-check:
	$(UV) run --frozen yaga repo check --plan .yaga/checks/repository.toml --revision "$(REVISION)"

commit-check:
	$(UV) run --frozen yaga commit check --commit "$(REVISION)"

change-check:
	$(UV) run --frozen yaga change check --policy .yaga/change-policy.toml --range "$(RANGE)"

workflow-lint:
	$(UV) run --frozen yaga workflow lint .github/workflows

check: lint format-check type-check policy-check

ci: check test workflow-lint docker-test

build:
	$(UV) build --no-sources
	$(UV) run --frozen --no-dev --group release twine check dist/*
	$(UV) run --frozen --no-dev --group release check-wheel-contents dist/*.whl

docker-build:
	docker build \
		--build-arg PYTHON_BASE=$(PYTHON_BASE) \
		--build-arg IPERF3_VERSION=$(IPERF3_VERSION) \
		-t $(DOCKER_IMAGE) .

docker-test: docker-build
	docker run --rm $(DOCKER_IMAGE) \
		pytest -vv --cov=iperf3_lib --cov-report=term-missing

docker-shell: docker-build
	docker run --rm -it $(DOCKER_IMAGE) bash

clean:
	rm -rf build dist *.egg-info .pytest_cache .coverage htmlcov .ruff_cache coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

all: ci
