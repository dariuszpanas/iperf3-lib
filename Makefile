.DEFAULT_GOAL := help

UV ?= uv
HOST_PYTHON ?= python
PYTHON_BASE ?= python:3.12-slim
IPERF3_VERSION ?= 3.21
DOCKER_IMAGE ?= iperf3-lib-test:local
TOOL_VERSIONS_FILE := .tool-versions
TOOL_VERSIONS := $(strip $(file <$(TOOL_VERSIONS_FILE)))

ifneq ($(words $(TOOL_VERSIONS)),2)
$(error $(TOOL_VERSIONS_FILE) must contain exactly "uv <version>")
endif
ifneq ($(word 1,$(TOOL_VERSIONS)),uv)
$(error $(TOOL_VERSIONS_FILE) must contain exactly "uv <version>")
endif
override UV_VERSION := $(word 2,$(TOOL_VERSIONS))

.PHONY: help check-uv-pin check-toolchain install test test-cov test-integration lint format format-check type-check check ci build clean docker-build docker-test docker-shell all

help:
	@echo "Available targets:"
	@echo "  check-uv-pin      - Verify every uv consumer derives from .tool-versions"
	@echo "  check-toolchain   - Verify installed uv matches .tool-versions"
	@echo "  install          - Sync the frozen development environment"
	@echo "  test             - Run tests that are not marked integration"
	@echo "  test-cov         - Run non-integration tests with coverage"
	@echo "  test-integration - Run integration tests (requires libiperf and iperf3)"
	@echo "  lint             - Run Ruff lint checks"
	@echo "  format           - Apply Ruff formatting and safe lint fixes"
	@echo "  format-check     - Check formatting without changing files"
	@echo "  type-check       - Run ty against the package"
	@echo "  check            - Run all non-mutating static checks"
	@echo "  ci               - Run static checks and the full Docker test suite"
	@echo "  build             - Build and validate wheel/sdist artifacts"
	@echo "  docker-build     - Build the Docker test image"
	@echo "  docker-test      - Build the image and run the full suite"
	@echo "  docker-shell     - Open a shell in the Docker test image"
	@echo "  clean            - Remove local build and test artifacts"

check-uv-pin:
	$(HOST_PYTHON) scripts/check_uv_pin.py --validate-only

check-toolchain:
	$(HOST_PYTHON) scripts/check_uv_pin.py --uv-command "$(UV)"

install test test-cov test-integration lint format format-check type-check build: check-toolchain

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

check: lint format-check type-check

ci: check docker-test

build:
	$(UV) build --no-sources
	$(UV) run --frozen --no-dev --group release twine check dist/*
	$(UV) run --frozen --no-dev --group release check-wheel-contents dist/*.whl

docker-build: check-uv-pin
	docker build \
		--build-arg UV_VERSION=$(UV_VERSION) \
		--build-arg PYTHON_BASE=$(PYTHON_BASE) \
		--build-arg IPERF3_VERSION=$(IPERF3_VERSION) \
		-t $(DOCKER_IMAGE) .

docker-test: docker-build
	docker run --rm -v "$(CURDIR):/app" $(DOCKER_IMAGE) \
		pytest -vv --cov=iperf3_lib --cov-report=term-missing

docker-shell: docker-build
	docker run --rm -it -v "$(CURDIR):/app" $(DOCKER_IMAGE) bash

clean:
	rm -rf build dist *.egg-info .pytest_cache .coverage htmlcov .ruff_cache coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

all: ci
