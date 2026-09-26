# Releasing

Release scope and migration decisions are tracked in the [roadmap](roadmap.md).
A merged PR prepares code; publishing a package requires an explicit release
decision.

For 0.3.0, the planned experiment scope is finite sequential trials and
explicit sweeps. Adaptive UDP, live events, and isolated workers have a
[documented follow-on disposition](design/advanced-execution.md); their issues
remain open. The small native event probe does not qualify stress delivery,
impairment, cancellation, or process cleanup. Release notes and capability
claims must preserve those boundaries.

## Rehearse without publishing

The default manual mode of the
[release workflow](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/release.yml)
is **qualify**. It builds and tests a candidate and retains evidence without
uploading to a package registry or creating a GitHub Release.

After the workflow change is on `main`, run:

```bash
gh workflow run release.yml --ref main -f target=qualify
```

The optional `version` input asserts that the selected revision's
`project.version` matches the supplied value. Omitting it uses the metadata.
A branch can be rehearsed in qualification mode; publication requires its
exact revision to belong to the remote default-branch history.

A successful rehearsal contains:

- A strict documentation build, rendered README and local-link checks, static
  and unit gates, YAGA policy checks, and Docker-backed workflow lint.
- One wheel/sdist pair checked with Twine and wheel-content validation.
  Both embedded descriptions must match the candidate README and render with
  valid documentation links. Every absolute documentation URL in the README,
  including its deployed HTML fragments, is checked alongside the public
  landing and changelog pages.
- A `release-bundle` artifact with those distributions and
  `release-evidence/manifest.json`. The manifest records source revision,
  version, build Python, filenames, sizes, and SHA-256 hashes.
- The full Python 3.12–3.14 × libiperf 3.19.1/3.21 native matrix, including
  lifecycle regressions, from the exact source revision.
- Installed wheel **and** sdist qualification in every matrix cell. These use
  fresh environments and isolated interpreters outside the checkout;
  editable/source imports are rejected.
- Retained `native-evidence-py*-iperf*` receipts showing native TCP, reverse,
  bidirectional, UDP, and SCTP runs, reporting roles, directional observations,
  configuration checks, and missing/zero/error-result semantics.
- A successful aggregate **Release qualification** job. Artifacts and evidence
  are retained for 30 days; download them for a longer-lived release record.

The installed smoke runs bounded, rate-limited loopback benchmarks. Each
format's smoke has a 120-second process deadline in the qualification
container. This test harness does not change the library's documented
cancellation or concurrency behavior.

## Prepare the release candidate

1. Fetch `origin`, compare the candidate with current `origin/main`, and
   resolve the accepted release criteria. Record dispositions for proposals
   deferred to a later version.
2. Update `project.version` in `pyproject.toml`, refresh `uv.lock`, and move
   the selected entries from `Unreleased` into a dated
   [changelog](changelog.md) section. Update the README, installation examples,
   migration instructions, and documentation banner together.
3. Run local quality/unit gates before the Docker/native matrix. Merge the
   candidate through the normal required checks, then run a build-only hosted
   rehearsal for the resulting exact commit. Inspect the retained distributions,
   hashes, and every installed-native receipt.
4. Inspect the rendered README and deployed documentation anonymously. Confirm
   the Pages revision, actual page content, and public links. The automated
   HTTP checks alone do not establish that the intended revision is displayed.
5. For measurement/exporter changes, also require the real
   [Prometheus/Grafana qualification](guides/grafana.md). An installed import
   or a string comparison of metrics is insufficient.
6. Verify the registry's trusted-publisher configuration and intended publishing
   environment restrictions before selecting a publication target. The workflow
   does not create or administratively configure those settings for a rehearsal.

Accepted version syntax is `MAJOR.MINOR.PATCH`, optionally followed by
`aN`, `bN`, or `rcN`, and optionally `.devN`. Local versions, epochs,
and post releases are not part of this publication policy.

Validate a planned tag against the current metadata:

```bash
uv run --no-project python scripts/validate_release.py --tag v0.3.0
```

That local metadata check alone does not qualify the candidate. The workflow
also binds checkout HEAD to the event SHA, and publication requires
default-branch ancestry.

## Publish only after the release decision

There are two publishing entry points:

- An explicitly selected manual `target=testpypi` runs the complete
  qualification and publishes only to TestPyPI through the `testpypi`
  environment.
- A `v*` tag runs the complete qualification, verifies exact tag/version
  equality and default-branch ancestry, and publishes to PyPI through the
  `pypi` environment. It then creates the GitHub Release.

Both paths build once. Publishing jobs download the retained bundle and
verify its manifest against the hash emitted by the build job, then verify
the exact artifact filenames, sizes, hashes, version and source identity.
They publish those same files without checking out or rebuilding the package.
The GitHub Release includes the same wheel, sdist and manifest.

Current stable uv is supported throughout; qualification records the tools
used and synchronizes the committed dependency lockfile.

## Verify publication

Check the published registry metadata, wheel/sdist files, GitHub Release
assets and their hashes against the retained manifest. Install the published
package in a clean environment, check version and lazy import, and run a
native smoke with a supported library. Inspect the public PyPI description
and its documentation links without maintainer authentication.

If publication is interrupted, inspect which files reached the registry
before retrying. Published files cannot be overwritten. If only GitHub Release
creation failed, repair that step using the retained artifacts.
