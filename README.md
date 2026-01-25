# iperf3-lib

Modern Python wrapper for **iperf3** using **cffi** against **libiperf**.  
- Typed configs/results (Pydantic)
- Sync + Async APIs
- Feature detection for SCTP / bidirectional / MPTCP (when lib supports it)
- Ready for `uv` workflows, ruff/ty/pytest

## Quickstart

```bash
# create env & install
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

You need a system libiperf (install iperf3 via your package manager).
To point to a non-standard library path:

```bash
export IPERF3_LIB=/usr/local/lib/libiperf.so
```

```python
from iperf3_lib.config import ClientConfig, Protocol
from iperf3_lib.iperf_client import Client

cfg = ClientConfig(server="127.0.0.1", duration=2, protocol=Protocol.TCP, parallel=2)
res = Client(cfg).run()
print("OK:", res.ok, "Mbps:", f"{res.summary_mbps:.2f}", "err:", res.error)
```
Notes
* Full features require a recent libiperf (iperf3 ≥ 3.19.x recommended).
* Some symbols may not exist on older builds; we detect and degrade gracefully.
* Integration tests are marked with @pytest.mark.integration.

---

## Running tests

- Run tests locally with Docker (recommended for Windows):

```bash
# build the test image (pick python base and iperf version if needed)
docker build --build-arg PYTHON_BASE=python:3.12-slim --build-arg IPERF3_VERSION=3.20 -t py-iperf3-test .

# run the full test suite inside the container (mounted volume allows editing without rebuilding)
docker run --rm -v "$(pwd):/app" py-iperf3-test bash -c "pytest -vv --cov=src --cov-report=term-missing"
```

- Quick single-test run (example):

```bash
docker run --rm -v "$(pwd):/app" py-iperf3-test bash -c "pytest -q tests/test_client_integration_configs.py::test_client_various_configs\[udp_rate\] -vv"
```

Notes about optional features
- MPTCP and some other features are optional and require the libiperf to be built with kernel/header support. Tests that exercise optional features will be skipped or xfailed depending on whether the built shared object exports the required symbol.

CI
- This repository includes a GitHub Actions workflow that builds and runs the tests inside the Docker image for a matrix of Python base images and iperf versions (see `.github/workflows/ci.yml`).

---

```markdown
# Changelog

## 0.1.0
- Initial release: cffi (ABI) wrapper of libiperf (client & server),
  Pydantic models, async support, ruff/ty/pytest, CI with uv.

```
