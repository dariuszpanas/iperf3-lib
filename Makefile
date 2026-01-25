.PHONY: help install test test-cov test-integration lint format type-check check clean docker-build docker-test docker-shell all

# Default target
help:
	@echo "Available targets:"
	@echo "  install          - Install project with dev dependencies (using uv)"
	@echo "  all              - Format, type-check, and run the full test suite (including integration)"
	@echo "  test             - Run tests (excluding integration)"
	@echo "  test-cov         - Run tests with coverage report"
	@echo "  test-integration - Run integration tests (requires iperf3 server)"
	@echo "  lint             - Run ruff linter (via uv)"
	@echo "  format           - Format code with ruff (via uv)"
	@echo "  type-check       - Run ty type checker (via uv)"
	@echo "  check            - Run all checks (lint, format check, type-check)"
	@echo "  clean            - Remove build artifacts and caches"
	@echo "  docker-build     - Build Docker test image"
	@echo "  docker-test      - Run tests in Docker container"
	@echo "  docker-shell     - Open shell in Docker container"

# Installation
install:
	# Sync project virtualenv and install dev dependencies via uv
	uv sync --dev

# Testing
test:
	uv run pytest -v -m "not integration"

test-cov:
	uv run pytest -v -m "not integration" --cov=src --cov-report=term-missing --cov-report=html

test-integration:
	uv run pytest -v -m integration

# Code quality
lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

type-check:
	uv run ty check src

check: lint type-check
	uv run ruff format --check src tests

# Cleanup
clean:
	rm -rf build dist *.egg-info .pytest_cache .coverage htmlcov .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Docker targets
docker-build:
	docker build -t py-iperf3-test .

docker-test: docker-build
	docker run --rm -v "$(CURDIR):/app" py-iperf3-test bash -c "pytest -vvv --cov=src --cov-report=term-missing --cov-report=xml"

# Aggregate target: format, type-check, and docker-test (full run)
all: format type-check docker-test


docker-shell: docker-build
	docker run --rm -it -v "$(CURDIR):/app" py-iperf3-test bash
