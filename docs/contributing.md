# Contributing

Install a current stable [uv](https://docs.astral.sh/uv/) release and clone the
repository. The committed lockfile selects dependency versions; contributors
do not need to match an exact uv version.

```bash
git clone https://github.com/dariuszpanas/iperf3-lib.git
cd iperf3-lib
make install
make check
make test
make workflow-lint
make docker-test
```

`make check` runs Ruff, ty, the strict documentation build/link check, and YAGA
repository policies. `make test` runs non-native tests. Docker is required for
workflow lint and the full native suite; see [compatibility](reference/compatibility.md)
for supported versions. Run non-native gates before native tests.

## Track the work

Use [GitHub issues](https://github.com/dariuszpanas/iperf3-lib/issues) for bugs,
feature proposals, and concrete tasks. The [roadmap](roadmap.md) links the
pre-release design review. Each issue should explain the problem, the expected
outcome, acceptance checks, and dependencies on other issues.

| Label | Meaning |
| --- | --- |
| `bug`, `enhancement`, `documentation` | Type of work |
| `planning` | A scope or design decision is still needed |
| `area:results` | Native parsing and normalized contracts |
| `area:metrics` | Prometheus output and Grafana integration |
| `area:configuration` | Configuration intent and capability detection |
| `area:benchmarking` | Analysis, trials, comparisons, and sweeps |
| `area:execution` | Native lifecycle, events, and process isolation |
| `area:tooling` | Development tools, CI, documentation, and releases |

Keep existing dependency automation labels. Add an area label only when useful;
avoid assigning a target release to a proposal before its scope is accepted.

## YAGA policies

YAGA is a development dependency of this repository. Consumer configuration
lives in `pyproject.toml` and `.yaga/`; no YAGA source checkout is required.
Tree policies read committed revisions, so validate the committed candidate:

```bash
git fetch origin
make policy-check REVISION=HEAD
uv run --frozen yaga commit check --range origin/main...HEAD
make change-check RANGE=origin/main...HEAD
```

Use Conventional Commit subjects and an explanatory body of at least eight words.
CI checks the PR title and every commit, and source changes require test changes.

The repository [contributor guide](https://github.com/dariuszpanas/iperf3-lib/blob/main/CONTRIBUTING.md)
is the full review checklist. See also [documentation maintenance](documentation.md),
[release procedure](releasing.md), and [security reporting](security.md).
