"""Security + liveness tests for the dashboard server (flygate/server/app.py).

Covers the CSRF/RCE-class hole (unauthenticated POST could flatten the live
portfolio) and the dead SSE broadcast (the dashboard used to freeze on the
initial /api/state snapshot and then paint "data stalled").
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from flygate.server.app import Dashboard

TOKEN = "unit-test-token-not-secret"
FOREIGN_ORIGIN = "https://evil.example"
NEW_VERSION = 42


class FakeRisk:
    def __init__(self):
        self.halted = False
        self.reason = ""

    def halt(self, reason: str):
        self.halted = True
        self.reason = reason

    def resume(self, total_equity: float):
        self.halted = False
        self.reason = ""
        self.anchored_at = total_equity


class FakeBroker:
    def __init__(self):
        self.balance = 100.0
        self.position = 0.0


class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.mode = "paper"

    def snapshot(self) -> dict:
        with self.lock:
            return {"mode": self.mode, "version": self.version}


class FakeEngine:
    """Records control commands; the HTTP thread must only queue them."""

    def __init__(self, host: str = "127.0.0.1"):
        self.cfg = SimpleNamespace(host=host, port=0)
        self.state = FakeState()
        self.risk = FakeRisk()
        self.brokers = {"BTC_USDT": FakeBroker()}
        self.closed: list = []
        self.commands: list = []
        self._halt_announced = True

    def request_kill(self, reason: str = "dashboard"):
        self.commands.append(("kill", reason))

    def request_resume(self):
        self.commands.append(("resume", ""))

    def _close_all(self, reason: str):
        self.closed.append(reason)

    def _total_equity(self) -> float:
        return 1234.5


@pytest.fixture
def dash(tmp_path):
    engine = FakeEngine()
    d = Dashboard(engine, token=TOKEN, runs_dir=str(tmp_path))
    httpd = d.serve("127.0.0.1", 0)
    d.port = httpd.server_address[1]
    try:
        yield engine, d
    finally:
        httpd.shutdown()
        httpd.server_close()
        d.stop_poller()


def _post(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        return r.status, dict(r.getheaders()), body
    finally:
        conn.close()


def _post_host(port: int, path: str, host: str, headers: dict):
    """POST with a forged Host header (http.client would append its own)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def _get(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def test_unauthenticated_post_rejected(dash):
    """(a) no token -> 403 and nothing is queued."""
    engine, d = dash
    for path in ("/api/kill", "/api/resume"):
        status, _, body = _post(d.port, path)
        assert status == 403, (path, status, body)
        assert json.loads(body)["error"] == "forbidden"
    assert engine.commands == []
    assert engine.risk.halted is False
    assert engine.closed == []


def test_wrong_token_and_foreign_host_rejected(dash):
    """Token mismatch and a forged Host (DNS rebinding) -> 403, no side effect."""
    engine, d = dash
    status, _, _ = _post(d.port, "/api/kill", {"X-Flygate-Token": "guess"})
    assert status == 403
    status, body = _post_host(d.port, "/api/kill", "evil.example",
                              {"X-Flygate-Token": TOKEN})
    assert status == 403 and json.loads(body)["reason"] == "host"
    assert engine.commands == [] and engine.closed == []


def test_bad_origin_post_rejected(dash):
    """(b) a CORS-simple cross-origin POST (valid token is impossible in a real
    browser, but a spoofed one must not help) is rejected."""
    engine, d = dash
    headers = {"X-Flygate-Token": TOKEN, "Origin": FOREIGN_ORIGIN}
    status, _, body = _post(d.port, "/api/kill", headers)
    assert status == 403 and json.loads(body)["reason"] == "origin"
    assert engine.commands == [] and engine.closed == []

    status, _, _ = _post(d.port, "/api/resume", {"Origin": FOREIGN_ORIGIN})
    assert status == 403

    # the dashboard's own origin (same host:port) is accepted
    good = {"X-Flygate-Token": TOKEN, "Origin": f"http://127.0.0.1:{d.port}"}
    status, _, _ = _post(d.port, "/api/resume", good)
    assert status == 200


def test_authenticated_post_accepted(dash):
    """(c) with the token the control endpoints work (by queuing the command)."""
    engine, d = dash
    status, _, body = _post(d.port, "/api/kill", {"X-Flygate-Token": TOKEN})
    assert status == 200
    assert json.loads(body) == {"ok": True, "killed": True, "queued": True}
    assert engine.commands == [("kill", "dashboard kill switch")]

    status, _, body = _post(d.port, "/api/resume", {"X-Flygate-Token": TOKEN})
    assert status == 200
    assert json.loads(body) == {"ok": True, "resumed": True, "queued": True}
    assert engine.commands == [("kill", "dashboard kill switch"), ("resume", "")]


def test_control_posts_never_mutate_engine_state_synchronously(dash):
    """The handler runs on the HTTP thread: it must only queue, never halt /
    close / touch broker state (that races the engine's own tick)."""
    engine, d = dash
    broker = engine.brokers["BTC_USDT"]
    before = dict(vars(broker))

    assert _post(d.port, "/api/kill", {"X-Flygate-Token": TOKEN})[0] == 200
    assert _post(d.port, "/api/resume", {"X-Flygate-Token": TOKEN})[0] == 200

    assert engine.commands == [("kill", "dashboard kill switch"), ("resume", "")]
    assert engine.risk.halted is False and engine.risk.reason == ""
    assert engine.closed == []
    assert engine._halt_announced is True
    assert engine.brokers["BTC_USDT"] is broker
    assert vars(broker) == before


def test_token_endpoint_is_not_cross_origin(dash):
    """(d) /api/token serves the token without any CORS headers, so a foreign
    page can neither read it nor preflight a request that sends it."""
    _, d = dash
    status, headers, body = _get(d.port, "/api/token")
    assert status == 200
    assert json.loads(body) == {"token": TOKEN}
    lowered = {k.lower() for k in headers}
    assert not [h for h in lowered if h.startswith("access-control-")]
    # and the server refuses the foreign-origin read outright
    status, _, _ = _get(d.port, "/api/token", {"Origin": FOREIGN_ORIGIN})
    assert status == 403


def test_stream_pushes_frames_after_state_change(dash):
    """(e) regression: /api/stream must deliver a data frame when the engine
    state version moves (the poller used to be missing entirely)."""
    engine, d = dash
    conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"

    frames: list = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                line = resp.fp.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    frames.append(line)
        except (OSError, TimeoutError):   # socket closed / idle timeout at teardown
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        time.sleep(0.6)                     # let the poller seed its baseline
        before = len(frames)
        engine.state.version = NEW_VERSION   # simulated engine tick
        deadline = time.time() + 3.0
        while time.time() < deadline and len(frames) <= before:
            time.sleep(0.05)
        assert len(frames) > before, "no SSE data frame within 3s of a state change"
        payload = json.loads(frames[before][len(b"data: "):])
        assert payload["version"] == NEW_VERSION
    finally:
        stop.set()
        conn.close()


def test_token_generated_and_persisted(tmp_path, monkeypatch):
    """Unset FLYGATE_TOKEN -> a fresh token is stored owner-only and reused."""
    monkeypatch.delenv("FLYGATE_TOKEN", raising=False)
    engine = FakeEngine()
    first = Dashboard(engine, runs_dir=str(tmp_path))
    path = tmp_path / "dashboard.token"
    assert path.exists()
    assert path.read_text().strip() == first.token
    assert len(first.token) >= 32
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Dashboard(engine, runs_dir=str(tmp_path)).token == first.token


def test_token_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FLYGATE_TOKEN", "from-env")
    d = Dashboard(FakeEngine(), runs_dir=str(tmp_path))
    assert d.token == "from-env"
    assert not (tmp_path / "dashboard.token").exists()
