# Diagnostic helper — not used by CI; see tools/README.md

#!/usr/bin/env python3
import socket
import threading
import time
from queue import Empty, Queue

from py_iperf3.config import ClientConfig, Protocol
from py_iperf3.ffi.api import ffi, lib
from py_iperf3.libiperf_client import Client
from py_iperf3.libiperf_server import Server


def _server_thread(host, port, exc_q: Queue):
    srv = Server(port=port, bind_host=host)
    try:
        srv.serve_forever()
    except Exception as e:
        exc_q.put(e)
        import traceback
        print('Server thread exception:', e)
        traceback.print_exc()


def wait_port(host, port, timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except Exception:
            time.sleep(0.05)
    return False


def main():
    host = '127.0.0.1'
    port = 5201
    exc_q: Queue = Queue()
    thr = threading.Thread(target=_server_thread, args=(host, port, exc_q), daemon=True)
    thr.start()
    if not wait_port(host, port, timeout=5.0):
        print('Server did not start')
        # no process to terminate; just exit
        return

    print('lib symbols:')
    for s in ['iperf_set_test_bitrate','iperf_set_test_rate','iperf_set_test_mptcp','iperf_set_test_bidirectional','iperf_set_test_json_output']:
        print(s, hasattr(lib, s))

    cfg = ClientConfig(server=host, port=port, duration=1, protocol=Protocol.UDP, rate=100000)
    try:
        res = Client(cfg).run()
        print('Result ok:', res.ok)
        print('Result raw:', res.raw)
    except Exception as e:
        import traceback
        print('Client raised', repr(e))
        traceback.print_exc()

    # Now replicate lower-level lib calls to inspect raw JSON output
    print('\n--- low-level lib test ---')
    t = lib.iperf_new_test()
    if t == ffi.NULL:
        print('iperf_new_test failed')
    else:
        try:
            # defaults
            if lib.iperf_defaults(t) < 0:
                print('iperf_defaults failed')
            else:
                lib.iperf_set_test_role(t, b'c')
                _set = None
                if hasattr(lib, 'iperf_set_test_server_hostname'):
                    lib.iperf_set_test_server_hostname(t, ffi.new('char[]', host.encode()))
                if hasattr(lib, 'iperf_set_test_server_port'):
                    lib.iperf_set_test_server_port(t, port)
                if hasattr(lib, 'iperf_set_test_duration'):
                    lib.iperf_set_test_duration(t, 1)
                # set UDP-specific bitrate if possible
                if hasattr(lib, 'iperf_set_test_rate'):
                    lib.iperf_set_test_rate(t, 100000)
                    print('used iperf_set_test_rate')
                elif hasattr(lib, 'iperf_set_test_bitrate'):
                    lib.iperf_set_test_bitrate(t, 100000)
                    print('used iperf_set_test_bitrate')
                else:
                    print('no bitrate setter available')
                # ensure JSON output if possible
                if hasattr(lib, 'iperf_set_test_json_output'):
                    lib.iperf_set_test_json_output(t, 1)
                # run client
                ret = lib.iperf_run_client(t)
                print('iperf_run_client ret =', ret)
                if ret < 0:
                    try:
                        err = lib.iperf_strerror(lib.i_errno)
                        print('lib error:', ffi.string(err).decode())
                    except Exception:
                        print('lib returned error, but strerror failed')
                cjson = lib.iperf_get_test_json_output_string(t)
                print('cjson ptr:', cjson)
                if cjson != ffi.NULL:
                    try:
                        s = ffi.string(cjson).decode()
                        print('cjson raw:', s)
                    except Exception as exc:
                        print('failed to decode cjson:', exc)
        finally:
            lib.iperf_free_test(t)

    # check for any exception raised by server thread
    try:
        err = exc_q.get_nowait()
    except Empty:
        err = None
    if err:
        print('Server thread raised during execution:', err)
    # attempt cooperative shutdown
    try:
        # import srv from thread? we can't access srv here; rely on process exit or thread ends
        pass
    except Exception:
        pass

if __name__ == '__main__':
    main()
