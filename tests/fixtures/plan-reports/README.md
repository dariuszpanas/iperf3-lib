# Frozen plan execution report fixtures

`v2-cancelled-synthetic.json` records one completed synthetic trial, one
cancelled trial with a retained event and one retention drop, and one unstarted
trial. Byte counts, timestamps, settings and event delivery are deterministic
codec examples; this file is not a native execution receipt.

The fixture freezes the standalone v2 field set and sequential pause semantics.
Do not regenerate it merely because the current dataclasses or codec change.
Both original report producer and retained artifact producer are preserved.
