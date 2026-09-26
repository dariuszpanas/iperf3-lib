# Contributing

Thanks for improving `iperf3-lib`. Changes should preserve the supported
Python 3.12-3.14 and libiperf 3.19.1/3.21 matrix.

## Development setup

Install a current stable release of [uv](https://docs.astral.sh/uv/), clone
the repository, and run:

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

## YAGA checks and policies

`make install` includes the published `yaga-cli` package in the development
environment. No YAGA source checkout or global YAGA installation is needed.
The project's policy lives in `[tool.yaga]` in `pyproject.toml` and `.yaga/`.
Dependabot's existing uv and GitHub Actions updates cover the CLI and Action.

`make check` runs Ruff, ty, and the YAGA repository plan. That plan checks
immutable Action references, workflow permissions and checkout settings,
Windows-compatible file names, regular file modes, file size limits, required
project files, and accidental commits of local vaults or generated files.
File mode, name, size, and tree checks read the committed `HEAD` snapshot;
workflow checks read the working files. Commit changes before validating the
complete candidate, or select another committed snapshot with `REVISION`.

```bash
make policy-check REVISION=HEAD
make commit-check REVISION=HEAD
make change-check RANGE=origin/main...HEAD
make workflow-lint
```

`workflow-lint` uses YAGA's Docker-backed actionlint runner. `make ci` runs
the non-native quality and unit gates, workflow lint, then native Docker tests.
Fetch the base branch before using a commit or change range: YAGA does not
fetch missing Git history automatically.

Before opening or updating a PR, also check its complete commit range and
source/test changes. These explicit range checks are additional to `make ci`:

```bash
uv run --frozen yaga commit check --range origin/main...HEAD
make change-check RANGE=origin/main...HEAD
```

Commit subjects use Conventional Commits with an optional scope and a maximum
of 100 characters. Include an explanatory body of at least eight words.
For feedback before committing, run:

```bash
uv run --frozen yaga commit check --file .git/COMMIT_EDITMSG
```

PR CI checks the title and every PR commit using complete history, and requires
test changes when Python source changes. The event-aware commit Action skips
message rules for verified Dependabot PRs; repository policies and tests still
run. The aggregate `ci` check requires the commit-policy job for PRs.

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
  checker remains isolated for deliberate review. CI, releases, and Docker
  builds use the current stable uv release so contributors need not match an
  exact local tool version.

## Releases

Maintainers should update `project.version` in `pyproject.toml`, refresh
`uv.lock`, and verify the intended tag before pushing it:

```bash
uv run --no-project python scripts/validate_release.py --tag v0.2.0
```

Production publication is tag-driven. Manual workflow dispatch publishes only
to TestPyPI and requires a version that exactly matches project metadata.
