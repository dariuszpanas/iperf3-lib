# Releasing

Release scope and migration decisions are tracked in the [roadmap](roadmap.md).
A merged PR prepares code; publishing a package requires an explicit release
decision.

The published 0.3.0 scope included finite sequential trials, explicit sweeps,
expanded native controls, callback events and isolated Python/CFFI workers.
The planned 0.4.0 scope adds owned async and bounded concurrent plans, adaptive
UDP experiments, and typed live-event consumption. Their current qualification
maps are linked from the [advanced execution design](design/advanced-execution.md).
Historical release receipts do not qualify these additions. Release notes and
capability claims must follow evidence for the combined candidate.

## Rehearse without publishing

The default manual mode of the
[release workflow](https://github.com/dariuszpanas/iperf3-lib/actions/workflows/release.yml)
is **qualify**. It builds and tests a candidate and retains evidence without
uploading to a package registry or creating a GitHub Release.

For the revision selected through the workflow ref, run:

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

## Installed lifecycle checks in CI

Current CI also qualifies operation cancellation, Linux parent-death cleanup,
the private worker transport, plans, adaptive UDP and typed live events
from installed wheels and sdists. It builds one distribution pair from the
candidate revision, then uses fresh environments outside the source checkout
in all six Python/libiperf combinations. Import paths and installed package
bytes must match the retained distributions. Missing or skipped lifecycle
cases fail qualification.

These checks retain separate lifecycle receipts with the source revision,
distribution and harness hashes, interpreter/native versions, and per-case
evidence. The 49 selected cases cover cancellation and parent death, malformed
and saturated transport, sequential and concurrent plan ownership, resource
recovery, adaptive decisions under measured impairment, and the complete
protocol/direction live-event matrix. Each distribution must pass all selected
setup/call/teardown phases; a selected skip fails qualification. See the
[live-event qualification map](design/live-event-qualification.md) and the
[isolated execution map](design/isolated-execution-qualification.md) for the
retained measurements and cleanup requirements.

These CI distributions are separate from the release workflow's retained
bundle and smoke receipts below. Before publishing 0.4.0, the expanded
qualification must also cover the exact wheel and sdist selected for release;
green CI on independently built artifacts is not that evidence.

## Inspect installed receipt v2

The 0.3.0 qualification contract requires **12 successful receipt-v2 files**:
one installed wheel and one installed sdist for each Python 3.12–3.14 and
libiperf 3.19.1/3.21 combination. Each receipt must retain:

| Evidence | Required checks |
| --- | --- |
| Distribution identity | Source revision, package version, distribution filename and SHA-256, Python/native version, and installed import location outside the checkout. |
| Native profiles | Forward, reverse and bidirectional TCP, UDP and SCTP; observed requested/effective settings and canonical artifact roundtrips. |
| Rate intent and analysis | Aggregate allocation with a remainder, measured bytes/time results, summary/interval quality and stream provenance, and available TCP/CPU evidence with explicit missing values. |
| Capabilities and saved results | Offline capability/codec checks with native loading forbidden, a separate native probe, supplied execution evidence, saved-native timing estimates, measured zero, missing data and failure preservation. |
| Repeated assessment | One warm-up and two measured native runs, retained baseline evidence, the complete selected population and median arithmetic, and JSON/text/JUnit/CI agreement. |
| Finite sweep | Two native stream-count cells with the same aggregate target, verified allocation, eligible receiver measurements, comparison evidence and a sweep-report roundtrip. |

`scripts/smoke_release.py` validates receipt contents with
`validate_smoke_receipt()`. This checks retained semantics without requiring the
verifier's Python, package version, platform or native library to match the
recorded environment. Independently bind every receipt to the retained release
manifest, expected matrix cell, exact source revision, and wheel/sdist hash.
Internal consistency alone does not establish that external identity.

Historical receipt-v1 files remain evidence for their original revisions. They
do not satisfy the expanded 0.3.0 contract. A successful earlier rehearsal also
does not qualify a later source revision. Keep the final candidate's manifest,
all 12 receipts and aggregate workflow result together before publication.

## Prepare the release candidate

1. Fetch `origin`, compare the candidate with current `origin/main`, and
   resolve the accepted release criteria. Record dispositions for proposals
   deferred to a later version.
2. Update `project.version` in `pyproject.toml` and the local project identity
   in `uv.lock`. Keep historical artifact producer identities unchanged. Record
   selected changes under a versioned **unreleased candidate** heading in the
   [changelog](../docs/changelog.md); a metadata version does not establish publication.
   Update installation examples, migration instructions, and the banner
   together. Keep the README's documentation links current; release history
   and version-specific instructions belong in the linked pages.
3. Run local quality/unit gates before the Docker/native matrix. Merge the
   candidate through the normal required checks, then run a build-only hosted
   rehearsal for the resulting exact commit. Inspect the retained distributions,
   hashes, and every installed-native receipt.
4. Inspect the rendered README and deployed documentation anonymously. Confirm
   the Pages revision, actual page content, and public links. The automated
   HTTP checks alone do not establish that the intended revision is displayed.
5. For measurement/exporter changes, also require the real
   [Prometheus/Grafana qualification](../docs/guides/grafana.md). An installed import
   or a string comparison of metrics is insufficient.
6. After qualification, obtain the explicit publication decision. Only an
   actual publication establishes a release date and permits changing the
   changelog's candidate status. Do not rebuild qualified distribution bytes
   merely to remove a temporary publication-status sentence from their README.
7. Verify the registry's trusted-publisher configuration and intended publishing
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
