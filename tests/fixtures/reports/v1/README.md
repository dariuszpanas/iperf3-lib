# Frozen assessment report v1 fixtures

These files were created once when `median-summary-v1` was introduced. Tests
load and roundtrip the committed JSON without invoking current analysis. Do not
regenerate them merely because analysis behavior changes.

The source setting receipts come from `native/3.21/tcp-forward-client.json`.
Byte counts, duration, reported bitrate, wrapper clocks and exceptions are
explicit deterministic synthetic variants for arithmetic and failure tests;
these reports are not claimed to be fresh native captures.

- `baseline-pass.json`: one valid candidate and baseline, inclusive 25% tolerance.
- `partial-failure.json`: one completed trial, one exception and one unstarted
  trial; inconclusive performance and an execution-failure CI status.
