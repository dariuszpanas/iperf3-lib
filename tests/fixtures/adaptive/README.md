# Adaptive UDP report fixtures

`v1-synthetic.json` freezes the `iperf3-lib.adaptive-udp` schema-v1 envelope and
`conservative-tested-rates-v1` interpretation. It is deterministic synthetic
evidence from `tests/test_adaptive_reports.py`, not a native qualification run.

The initial 8,000 and 24,000 bit/s grid contains an acceptable lower observation
and an excessive-loss upper observation. The planner confirms the lower rate,
refines their integer midpoint, and confirms the 16,000 bit/s observation.
Every admitted trial, batch reason, cooldown, allocation, receiver packet count,
failed acceptance condition, and original artifact producer remains present.
Native-reported loss deliberately differs from packet-derived loss to preserve
the separate evidence fields.

Nested result producer versions use `fixture-development`; the envelope producer
is `example.fixture` version `1`. Changing this fixture requires reviewing the
versioned archive contract. It must not be regenerated merely to hide a changed
decision or measurement interpretation.
