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

## Artifact v1 capture expansion

An additional 24 endpoint documents were captured on 2026-09-26 from source
`b366c2c88f4edb1e415d243592227d83b235234d` with the artifact implementation working
tree overlaid. The original 16 documents above were retained. The added cases
are UDP reverse and bidirectional, SCTP forward/reverse/bidirectional, and TCP
with one second of omitted warm-up. Every case succeeded on both versions;
none was synthesized or skipped. The same duration, stream count, rate, loopback,
resource limits, and server cleanup contract apply.

The expansion used these already-built Linux images, with the current
`src/iperf3_lib/result.py`, `src/iperf3_lib/iperf_client.py`, and capture script
copied into each isolated container:

| Native version | Python | Image | Image identity |
| --- | --- | --- | --- |
| 3.19.1 | 3.12.14 | `iperf3-lib-test:semantics-min` | `sha256:2db3903f1ee26bbfb61a54b28897f5b0530b1888fa2bec9f0c885bb011eb6131` |
| 3.21 | 3.14.7 | `iperf3-lib-test:semantics-latest` | `sha256:daf709b6c657a3824e8f352e19db3ce8cd493d54711234dd8fa70ecde4d91911` |

Before capture, focused Ruff and ty checks passed, and 122 normalization/native
fixture tests passed. The capture script accepts repeated `--scenario` flags to
select the six new cases; omitting that option captures all ten cases. Its
`preserve_raw` adapter replaces only the result normalization callback, so an
unsupported native value or parser defect cannot filter out the evidence being
captured. Native allocation, configuration setters, protocol getter checks,
blocking CFFI execution, JSON retrieval, and native cleanup still execute. The
new raw fixtures are then parsed independently by the normal regression tests.

Warm-up fixtures preserve both `omitted: true` and `omitted: false` intervals.
Native interval boundaries restart near zero after warm-up; canonical duration
comes from the native `seconds` field rather than subtraction or requested time.

### Official producer semantics

The versioned upstream source explains two intentionally conservative mappings:

- The UDP stream end object mixes sender bytes/rate with receiver loss/jitter,
  and chooses its packet count according to available endpoint information.
  Preserve the complete object as an unattributed stream observation. See
  [3.19.1 UDP summary emitter](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L4114-L4135)
  and [3.21 UDP summary emitter](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L4294-L4315).
- Retransmission support and collection are restricted to TCP, while SCTP
  emitters still output retransmission fields. Exchange can supply `-1`, default
  summaries contain zero, and SCTP interval storage is not populated with a
  retransmission measurement. These captures include the uninitialized integer
  `3684054920433006592` in 3.19.1 server intervals. See
  [3.19.1 capability check](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L630-L636),
  [exchange sentinel](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L2599),
  [collector](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L3562-L3639),
  [summary emitter](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L4081-L4084),
  and [interval emitter](https://github.com/esnet/iperf/blob/3.19.1/src/iperf_api.c#L4533-L4536).
  The corresponding 3.21 paths are its
  [capability check](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L634-L640),
  [exchange sentinel](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L2764),
  [collector](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L3738-L3815),
  [summary emitter](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L4261-L4264),
  and [interval emitter](https://github.com/esnet/iperf/blob/3.21/src/iperf_api.c#L4720-L4723).

For these two qualified producer versions, every emitted SCTP retransmission
integer is normalized to `None` with `unsupported` availability. Other or
missing producer versions receive `unknown` availability because their support
has not been established. Raw values and protocol/version pointers remain as
evidence. Wrong types are rejected; unrelated negative measurements are rejected.
Synthetic tests cover additional signed values without claiming they appeared
in these captures.

## libiperf 3.22 capture expansion

Twenty additional endpoint documents were captured on 2026-10-04 from clean
source `91879463bcf7be888e1c6c068d9f2f730510812a`, using the unchanged
`capture.py` above with libiperf 3.22 and CPython 3.14.8 on Linux x86_64. The
official versioned release archive was verified by the Dockerfile's upstream
SHA-256 check. The loaded native version getter returned `3.22`; every captured
document identifies `iperf 3.22`, and no run returned a native error.

The capture image was `iperf3-lib-test:libiperf-322-base`, with Docker-inspected
image identity
`sha256:6b63c9d3a54ba7d35c4c3f20176614945397062586eb369804f482fc0b7214f7`.
The container used `--network none`, one CPU, 256 MiB of memory, and a
120-second outer deadline. All ten scenarios ran with the same duration,
parallelism, per-stream rate, UDP block size, and warm-up settings described
above. Client and server documents are retained separately in `3.22/`.
The existing forty documents in `3.19.1/` and `3.21/` remain unchanged.

The tagged [3.22 TCP getters](https://github.com/esnet/iperf/blob/3.22/src/tcp_info.c)
and [SCTP implementation](https://github.com/esnet/iperf/blob/3.22/src/iperf_sctp.c)
are byte-identical to 3.21. The relevant
[JSON summary, interval, exchange, and collection functions](https://github.com/esnet/iperf/blob/3.22/src/iperf_api.c)
and [CPU calculation](https://github.com/esnet/iperf/blob/3.22/src/iperf_util.c)
are also unchanged. Consequently the qualified Linux TCP/CPU units and SCTP
retransmission-unavailability rules above also apply to 3.22. Regression tests
compare the new native values and evidence pointers directly, while retaining
coverage for both earlier producers and unknown-version/platform handling.
