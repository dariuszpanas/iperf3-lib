#!/usr/bin/env python3
# Diagnostic helper — not used by CI; see tools/README.md
import json
import subprocess

symbols = [
    'iperf_set_test_rate',  # canonical numeric rate setter
    'iperf_set_test_bitrate',  # alternate historical name
    'iperf_set_test_mptcp',
    'iperf_set_test_bidirectional',
    'iperf_set_test_json_output',
]

out = {'iperf_version': None, 'symbols': {}}
try:
    p = subprocess.run(['iperf3', '--version'], capture_output=True, text=True, check=False)
    out['iperf_version'] = p.stdout.strip() or p.stderr.strip()
except Exception as e:
    out['iperf_version_error'] = str(e)

try:
    from py_iperf3.ffi.api import lib
    for s in symbols:
        out['symbols'][s] = hasattr(lib, s)
except Exception as e:
    out['ffi_error'] = str(e)

print(json.dumps(out, indent=2))
