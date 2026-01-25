import importlib

from py_iperf3.result import Result


class DummyFFI:
    def __init__(self):
        self.NULL = 0

    def string(self, s):
        return s

    def new(self, spec, val):
        return val


class RecorderLib:
    def __init__(self, *, make_json=True, run_client_ret=0):
        self.i_errno = 0
        self._record = {}
        self._make_json = make_json
        self._run_client_ret = run_client_ret

    def iperf_new_test(self):
        return 1

    def iperf_defaults(self, t):
        return 0

    def iperf_set_test_role(self, t, c):
        self._record["role"] = c

    def iperf_set_test_server_hostname(self, t, s):
        self._record["hostname"] = s

    def iperf_set_test_server_port(self, t, p):
        self._record["port"] = p

    def iperf_set_test_duration(self, t, d):
        self._record["duration"] = d

    def iperf_set_test_omit(self, t, o):
        self._record["omit"] = o

    def iperf_set_test_num_streams(self, t, n):
        self._record["parallel"] = int(n)

    def iperf_set_test_blksize(self, t, b):
        self._record["blksize"] = int(b)

    def iperf_set_test_tos(self, t, tos):
        self._record["tos"] = int(tos)

    def iperf_set_test_reverse(self, t, v):
        self._record["reverse"] = int(v)

    def iperf_set_test_bidirectional(self, t, v):
        self._record["bidir"] = int(v)

    def iperf_set_test_mptcp(self, t, v):
        self._record["mptcp"] = int(v)

    def iperf_set_test_json_output(self, t, v):
        self._record["json"] = int(v)

    def iperf_run_client(self, t):
        return int(self._run_client_ret)

    def iperf_get_test_json_output_string(self, t):
        if not self._make_json:
            return self._make_json
        raw = b'{"end": {"sum_sent": {"bits_per_second": 1234.0}}}'
        return raw

    def iperf_strerror(self, errno):
        return b"err"

    def iperf_free_test(self, t):
        return None


def _setup_and_run(monkeypatch, recorder: RecorderLib, cfg_kwargs: dict):
    import py_iperf3.ffi.api as api_mod

    dummy_ffi = DummyFFI()
    monkeypatch.setattr(api_mod, "ffi", dummy_ffi)
    monkeypatch.setattr(api_mod, "lib", recorder)

    # reload client module so it picks updated api objects
    import py_iperf3.libiperf_client as client_mod

    importlib.reload(client_mod)

    from py_iperf3.config import ClientConfig
    from py_iperf3.libiperf_client import Client

    cfg = ClientConfig(server="127.0.0.1", **cfg_kwargs)
    res = Client(cfg).run()
    return res, recorder._record


def test_client_bidirectional(monkeypatch):
    r = RecorderLib()
    res, record = _setup_and_run(monkeypatch, r, {"bidirectional": True})
    assert isinstance(res, Result)
    # json output present => ok True
    assert res.ok is True
    assert record.get("bidir") == 1


def test_client_mptcp(monkeypatch):
    r = RecorderLib()
    res, record = _setup_and_run(monkeypatch, r, {"mptcp": True})
    assert isinstance(res, Result)
    assert res.ok is True
    assert record.get("mptcp") == 1


def test_client_setters(monkeypatch):
    r = RecorderLib()
    res, record = _setup_and_run(
        monkeypatch,
        r,
        {"parallel": 4, "blksize": 1500, "tos": 2, "omit": 1},
    )
    assert isinstance(res, Result)
    assert res.ok is True
    assert record.get("parallel") == 4
    assert record.get("blksize") == 1500
    assert record.get("tos") == 2
    assert record.get("omit") == 1


def test_client_run_error(monkeypatch):
    # Simulate iperf_run_client returning negative -> client returns Result(ok=False)
    r = RecorderLib(run_client_ret=-1)
    res, record = _setup_and_run(monkeypatch, r, {})
    assert isinstance(res, Result)
    assert res.ok is False
