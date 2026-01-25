# Developer tools

This folder contains small helper scripts for debugging libiperf or running a manual local server/client session.

- `inspect_lib_symbols.py` - print which optional symbols the loaded lib exports.
- `debug_udp_run.py` - small script to run a single UDP client run against a local server started via the library.

These are developer helpers and not used by CI. Use them manually when debugging integration problems.
