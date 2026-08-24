# Contributing

Thanks for improving `iperf3-lib`. Changes should preserve the supported
Python 3.12-3.14 and libiperf 3.19.1/3.21 matrix.

## Development setup

Install the [uv](https://docs.astral.sh/uv/) release pinned in
[`.tool-versions`](.tool-versions), clone the repository, and run:

```bash
make install
```

Native tests require both the iperf3 executable and its shared `libiperf`.
Linux is the tested host platform. Docker is the recommended route when the
host does not provide a compatible library:

```bash
make docker-test
```

The Docker compatibility dimensions can be selected explicitly:

```bash
make docker-test PYTHON_BASE=python:3.14-slim IPERF3_VERSION=3.19.1
```

## Checks

Run non-mutating static checks before submitting a change:

```bash
make check
```

With a compatible host library, run the non-integration tests directly:

```bash
make test
```

Build and validate distribution artifacts after packaging or documentation
changes:

```bash
make build
```

Run `make format` only when you intend to modify source formatting.

## Native API changes

- Keep CFFI declarations compatible with both supported libiperf releases.
- Test behavior, not just symbol presence. Protocol tests must verify that
  libiperf applied the requested TCP, UDP, or SCTP protocol.
- Optional native features must fail explicitly with
  `UnsupportedFeatureError`; do not silently ignore configuration.
- Keep native loading lazy so package metadata and non-native configuration can
  be used without immediately loading a shared library.
- Document thread, cancellation, and shutdown limitations when changing
  asynchronous or server behavior.

## Pull requests

Keep changes focused and explain:

- the user-visible behavior and compatibility impact;
- tests added or updated;
- documentation or changelog changes; and
- the exact commands used for validation.

Do not commit local environments, coverage output, downloaded iperf sources,
or the `.vault/` project knowledge base.

## Maintenance automation

- The complete native compatibility matrix runs on the first day of each month
  at 09:17 America/Los_Angeles, as well as on pushes and pull requests.
- Dependabot checks Python, GitHub Actions, and Docker dependencies every
  Monday. Version and security updates are grouped by ecosystem; the beta `ty`
  checker remains isolated for deliberate review. The uv executable is pinned
  once in `.tool-versions` and upgraded as a coordinated toolchain change.

## Releases

Maintainers should update `project.version` in `pyproject.toml`, refresh
`uv.lock`, and verify the intended tag before pushing it:

```bash
uv run --no-project python scripts/validate_release.py --tag v0.2.0
```

Production publication is tag-driven. Manual workflow dispatch publishes only
to TestPyPI and requires a version that exactly matches project metadata.
