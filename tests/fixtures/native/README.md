# Native result fixtures

These are unmodified native JSON objects captured on 2026-09-26 from real
loopback runs, using `capture.py`. Client JSON is returned by the CFFI-backed
`Client.run()` API; server JSON is emitted by a separate native
`iperf3 --server --one-off --json` process. JSON formatting is normalized for
review, but fields and values are retained, including actual timestamps,
container hostnames, connection ports, zero values, and socket identifiers.

The non-native gate ran first on source revision
`a6d74e9a6dd69e9a761fe556c63e7fba51bf6d42`:
`uv run --frozen make check test` passed all six YAGA checks, strict documentation
build/link checks, Ruff, ty, and 143 unit tests (11 native tests deselected).

## Captured environments

| Directory | libiperf | Python | Platform | Local image |
| --- | --- | --- | --- | --- |
| `3.19.1` | 3.19.1 | 3.12.14 | Linux x86_64 | `iperf3-lib-test:docs-min` |
| `3.21` | 3.21 | 3.14.7 | Linux x86_64 | `iperf3-lib-test:docs-latest` |

The image identities used were:

- Minimum: `sha256:2df5fcebbb3ea70178ec3556e65f05b194a2baf11c42cd905a5e02a0f1fe936e`
- Latest: `sha256:1296b4f830178644e7bb7b9e664d330211e9bb0e9d623558d5dc7cb235b8df54`

Both images build libiperf from the official versioned release archive using
the repository Dockerfile, including its upstream SHA-256 checksum check.

Each version has client and server JSON for TCP forward, TCP reverse, TCP
bidirectional, and UDP. Every run requests one second, two parallel streams,
and 4,000,000 bits/s per stream. UDP uses 1,200-byte datagrams. Bidirectional
mode has two streams in each direction. Capture containers have one CPU and
256 MiB limits and `--network none`; all traffic stays on container loopback.
The script terminates and waits for its server on every exit path.

## Reproduce

Build the two native images from the source revision being tested:

```bash
uv run make check test
uv run make docker-build PYTHON_BASE=python:3.12-slim IPERF3_VERSION=3.19.1 DOCKER_IMAGE=iperf3-lib-test:docs-min
uv run make docker-build PYTHON_BASE=python:3.14-slim IPERF3_VERSION=3.21 DOCKER_IMAGE=iperf3-lib-test:docs-latest
```

The capture used `docker cp` so Docker Desktop did not require changing drive
sharing. From PowerShell at the repository root, run this once for each version
and image pairing in the table above:

```powershell
$fixtureVersion = '3.19.1'
$fixtureImage = 'iperf3-lib-test:docs-min'
$fixtureContainer = docker create --network none --cpus 1 --memory 256m --entrypoint python $fixtureImage /tmp/capture.py --version $fixtureVersion --output /tmp/fixtures
if ($LASTEXITCODE -ne 0 -or $fixtureContainer -notmatch '^[a-f0-9]{64}$') { throw 'Failed to create capture container' }
try {
    docker cp tests/fixtures/native/capture.py "${fixtureContainer}:/tmp/capture.py"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to copy capture script' }
    docker start --attach $fixtureContainer
    if ($LASTEXITCODE -ne 0) { throw 'Native capture failed' }
    New-Item -ItemType Directory -Path "tests/fixtures/native/$fixtureVersion" -Force | Out-Null
    docker cp "${fixtureContainer}:/tmp/fixtures/." "tests/fixtures/native/$fixtureVersion"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to retain fixtures' }
}
finally {
    docker rm --force $fixtureContainer | Out-Null
}
```

Fixture changes should be reviewed alongside their capture revision and native
version. Do not replace native values with convenient synthetic measurements.

## Verified role and stream evidence

Both versions include `start.connecting_to` in client output and
`start.accepted_connection` in server output. These are objects containing the
control connection's host and port. `start.connected` lists each local stream
socket and its connection endpoints.

For bidirectional interval streams, the native `sender` flag describes the
reporting endpoint's role for that stream. A client-reported sender stream runs
client to server; a server-reported sender stream runs server to client. The
opposite local role gives the opposite direction. Stream array positions are
not the source of direction information.

TCP `end.streams` contains `sender` and `receiver` observations sharing the same
local socket identifier. Both nested objects carry the same local-role `sender`
flag, so that flag does not change the observation named by the enclosing key.
UDP uses an `udp` object per end stream. Server-origin JSON includes native zero
values for some remote end summaries; these fixtures retain those values.

`tests/test_result_native_fixtures.py` checks both endpoint roles against their
actual native summaries and intervals and reverses real stream arrays to guard
against assumptions about their positions. Native integration tests repeat the
direction checks against newly returned JSON in the minimum/latest CI matrix.
