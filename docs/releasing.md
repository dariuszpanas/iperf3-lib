# Releasing

This is the maintainer procedure. Documentation work or closing a roadmap issue
does not publish a package. Decide the next release scope through the
[roadmap](roadmap.md) before selecting a candidate version.

## Qualify the candidate

1. Fetch `origin`, compare the candidate with current `origin/main`, and review
   unresolved correctness issues and accepted release criteria.
2. Update `project.version` in `pyproject.toml`, refresh `uv.lock`, and move the
   appropriate changes out of `Unreleased` in the [changelog](changelog.md).
   Update the documentation banner, README, and installation examples together.
3. Run `make ci` and `make build`. Require the hosted Python 3.12–3.14 ×
   libiperf 3.19.1/3.21 matrix for the exact candidate commit. Preserve both
   endpoint observations and native lifecycle behavior in regression coverage.
4. Inspect the rendered README, built docs, and public documentation URLs.
   Verify the Pages deployment reflects the intended source, and check links
   anonymously. Twine and the offline link checker do not prove remote availability.
5. For exporter changes, require a real benchmark to reach Prometheus and the
   intended Grafana queries/panels. A string comparison of metrics is insufficient.

Validate the chosen tag against metadata (replace the example for the next release):

```bash
uv run --no-project python scripts/validate_release.py --tag v0.2.0
```

## Publication workflow

The [release workflow](https://github.com/dariuszpanas/iperf3-lib/blob/main/.github/workflows/release.yml)
has two entry points:

- A `v*` tag validates that its version matches metadata, builds wheel and sdist,
  checks Twine metadata and wheel contents, and smoke-installs the wheel on all
  supported Python versions. The `pypi` environment publishes the retained
  distributions, followed by a GitHub Release using those same artifacts.
- Manual dispatch requires an exact version input and publishes only to TestPyPI
  through the `testpypi` environment after the same build and smoke checks.

The workflow itself does not run the full native suite or assert that a tag is
on `main`; maintainers must establish those conditions before pushing a tag.
Production and TestPyPI publication require an explicit release decision.

## Verify publication

Check the new PyPI metadata, wheel/sdist files, and GitHub Release assets. Install
the published wheel in a clean environment, check its version and lazy import,
and run a real native smoke test with a supported library. Inspect the PyPI
description and its documentation links without maintainer authentication.

If publication is interrupted, inspect which artifacts reached the registry
before retrying. Published files cannot be overwritten. If only the GitHub
Release failed, repair that step using the retained artifacts.
