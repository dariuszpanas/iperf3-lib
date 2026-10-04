# Contributing

Thanks for improving `iperf3-lib`. Changes should preserve the supported
Python 3.12-3.14 and libiperf 3.19.1/3.22 matrix.

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

### Reuse one local Docker context

Local Docker targets stage the current checkout into a fixed directory shared
by all worktrees. Docker receives that stable context path. The default is
`iperf3-lib/docker-validation` under the platform's user cache directory.

To select a persistent location for this repository and its worktrees:

```bash
git config --local iperf3-lib.dockerStagingRoot /absolute/path/to/docker-validation
```

The directory must be dedicated to this helper and initially empty.
A location within a checkout must be ignored
by Git. `--staging-root` overrides `IPERF3_DOCKER_STAGING_ROOT`, which overrides
the shared repository setting and then the user-cache default.

The helper selects tracked and nonignored untracked files, including dirty
changes and deletions. It excludes Git internals, vaults, caches, build output
and common credential files even when tracked. Symlinks, Windows reparse points,
and submodules are rejected. Git selection or file changes during staging abort
the build. Review `source-manifest.json` alongside the fixed `context` directory
for the selected revision, dirty state and file hashes.

An ownership marker protects existing unrelated directories. Only the owned
`context` child is replaced; other files in the staging root are preserved. An
OS file lock covers staging, build, and tests. Concurrent attempts fail with an
in-use error; retry after the current run finishes. Process exit releases the
lock automatically.

The underlying commands are:

```bash
uv run --frozen python scripts/docker_validate.py stage
uv run --frozen python scripts/docker_validate.py test --python-base python:3.14-slim --iperf-version 3.22
uv run --frozen python scripts/docker_validate.py build --dockerfile examples/observability/Dockerfile --image iperf3-lib-observability:dev
```

These commands build and run without host bind mounts. For manual `docker cp`
validation, first copy inputs and collect outputs under the same chosen staging
root so the container runtime sees stable host paths. Hosted Linux workflows continue
using their checked-out build contexts. YAGA uses named Docker workspace volumes
and streamed copies; its configuration needs no change.

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

`make check` runs Ruff, ty, the documentation build/link check, and the YAGA repository plan. That plan checks
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

Start from an existing [issue](https://github.com/dariuszpanas/iperf3-lib/issues)
or open one using the bug, feature, or task form. Use `bug`, `enhancement`, or
`documentation` for the work type and one `area:*` label for the affected
component. `planning` means a design or scope decision is still needed.
Keep acceptance criteria in the issue and link dependencies explicitly.
Keep planning and acceptance evidence in GitHub issues; public guides should
explain how to use the library.

Keep changes focused and explain:

- the user-visible behavior and compatibility impact;
- tests added or updated;
- documentation or changelog changes; and
- the exact commands used for validation.

Do not commit local environments, coverage output, downloaded iperf sources,
or the `.vault/` project knowledge base.

## Documentation

The site uses Zensical, following YAGA's documentation conventions. Edit
`docs/`, navigation in `zensical.toml`, and theme overrides in `overrides/`.
Keep examples aligned with the source and distinguish unreleased APIs from
the latest published version.

```bash
make docs
make docs-serve
```

`make docs` runs a strict clean build and validates local site links, assets,
fragments, and rendered README links. It does not check remote URLs. CI builds
documentation on every PR; `.github/workflows/docs.yml` deploys it to GitHub
Pages after a push to `main`. The detailed
[documentation maintenance guide](development/documentation.md)
covers Pages setup, preview, and publication checks.

## Maintenance automation

- The complete native compatibility matrix runs on the first day of each month
  at 09:17 America/Los_Angeles, as well as on pushes and pull requests.
- Dependabot checks Python, GitHub Actions, and Docker dependencies every
  Monday. Version and security updates are grouped by ecosystem. CI, releases, and Docker
  builds use the current stable uv release so contributors need not match an
  exact local tool version.

## Releases

Follow the [maintainer release procedure](development/releasing.md) for candidate
preparation, build-only qualification, and publication.
