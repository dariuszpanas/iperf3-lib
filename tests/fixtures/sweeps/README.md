# Sweep report fixture

`v1-synthetic.json` is a deterministic synthetic codec fixture, not a network
benchmark. It is generated from `tests.sweep_helpers.measured` with two parallel
stream cells, one measured repetition per cell, randomized order seed 4, and a
fixed test clock. Receiver bytes (100), seconds (1), and native-shaped receipts
are deliberately synthetic. The native-reported rate differs to prove summary
arithmetic uses bytes/time. Nested producer version is normalized to
`fixture-development`; the envelope producer is `example.fixture` version `1`.

The golden freezes the unreleased sweep-v1 development contract. Production
native evidence is qualified separately in `test_sweeps_integration.py` on both
supported libiperf endpoints.
