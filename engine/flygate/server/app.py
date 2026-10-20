"""Dashboard server — Python stdlib only (no FastAPI dependency).

Routes:
  GET  /            static single-page dashboard
  GET  /api/state   full JSON snapshot
  GET  /api/token   control token for the POST endpoints (same-origin only)
  GET  /api/stream  SSE stream of snapshots (pushed whenever the engine ticks)
  POST /api/kill    queue a kill switch command (engine thread halts + flattens)
  POST /api/resume  queue a resume command (engine thread clears the halt)

Security model (the dashboard is bound to a loopback address by default, but
`FLYGATE_HOST=0.0.0.0` makes it LAN reachable, so it defends itself):
  * POST requires header ``X-Flygate-Token`` equal to the server token. The
    token is read from ``FLYGATE_TOKEN`` or generated once and persisted to
    ``runs/dashboard.token`` (owner-only).
  * The ``Host`` header must be in the allowlist (loopback names plus the
    configured bind host unless that is a wildcard address) — anti DNS rebinding.
  * A present ``Origin`` header must be in the same allowlist — anti CSRF: a
    foreign page can send a CORS "simple" request, but it cannot set the token
    header (that needs a preflight) and it cannot read ``/api/token`` because
    no CORS headers are ever emitted.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..storage import default_runs_dir

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# State files the ZCode automation drops here (x_watch_<account>.json); read-only.
X_WATCH_DIR = os.path.join(_ROOT, "data", "cache")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
TOKEN_HEADER = "X-Flygate-Token"
TOKEN_FILE = "dashboard.token"

POLL_INTERVAL = 0.5     # seconds between engine-version polls
WRITE_INTERVAL = 0.2    # seconds a stream handler waits between socket writes
PING_INTERVAL = 10.0    # seconds between SSE keep-alive comments
QUEUE_DEPTH = 8         # snapshots buffered per slow client


def _hostname(value: str | None) -> str:
    """Host part of a ``Host`` header or an origin URL, lower-cased."""
    v = (value or "").strip().lower()
    if v.startswith("["):                       # [::1]:8787
        end = v.find("]")
        return v[1:end] if end > 0 else v
    if "://" in v:                              # https://host:port
        v = v.split("://", 1)[1]
        v = v.split("/", 1)[0]
    if ":" in v:
        return v.rsplit(":", 1)[0]
    return v


class Dashboard:
    def __init__(self, engine, token: str | None = None, runs_dir: str | None = None):
        self.engine = engine
        self.clients: list = []          # deque of pending snapshot strings
        self.lock = threading.Lock()
        self.runs_dir = (runs_dir or getattr(engine, "runs", None)
                         or default_runs_dir())
        self.token = token or self._load_or_create_token()
        self.allowed_hosts = set(LOOPBACK_HOSTS)
        self._allow_host(getattr(getattr(engine, "cfg", None), "host", None))
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

        # Multi-Chain Smart Money Radar
        try:
            from ..radar.tracker import RadarTracker
            self.radar = RadarTracker(self.runs_dir)
            self.radar.start()
        except Exception as e:
            print(f"[dash ] radar initialization notice: {e}")
            self.radar = None

    # ---------------------------------------------------------------- token
    def _allow_host(self, host: str | None):
        h = _hostname(host)
        if h and h not in WILDCARD_HOSTS:
            self.allowed_hosts.add(h)

    def _load_or_create_token(self) -> str:
        env = (os.environ.get("FLYGATE_TOKEN") or "").strip()
        if env:
            return env
        path = os.path.join(self.runs_dir, TOKEN_FILE)
        try:
            with open(path) as f:
                saved = f.read().strip()
            if saved:
                print(f"[dash ] control token loaded from {path}")
                return saved
        except OSError:
            pass
        tok = secrets.token_urlsafe(32)
        try:
            os.makedirs(self.runs_dir, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(tok)
            try:
                os.chmod(path, 0o600)        # no-op-ish on Windows, enforced on POSIX
            except OSError:
                pass
        except OSError as e:
            print(f"[dash ] WARNING: could not persist token to {path}: {e}")
        print(f"[dash ] control token (also in {path}): {tok}")
        return tok

    # ------------------------------------------------------------ broadcast
    def broadcast(self):
        """Fan the current snapshot out to every connected SSE client."""
        with self.lock:
            if not self.clients:
                return
        snap = json.dumps(self.engine.state.snapshot())
        with self.lock:
            for q in self.clients:
                q.append(snap)

    # --------------------------------------------------------------- poller
    def start_poller(self):
        """Watch the engine state version and broadcast on every change."""
        with self.lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return self._poll_thread
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._watch_state, daemon=True, name="dash-poller")
            t = self._poll_thread
        t.start()
        return t

    def stop_poller(self):
        self._poll_stop.set()
        t = self._poll_thread
        self._poll_thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _state_version(self):
        return getattr(self.engine.state, "version", None)

    def _watch_state(self):
        last = self._state_version()
        while not self._poll_stop.wait(POLL_INTERVAL):
            try:
                ver = self._state_version()
            except Exception:
                continue
            if ver is None or ver == last:
                continue
            last = ver
            try:
                self.broadcast()
            except Exception:
                pass

    # -------------------------------------------------------------- handler
    def make_handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):   # silence
                pass

            def _send(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            # -- guards: never send CORS headers, so no foreign page can read us
            def _host_ok(self) -> bool:
                return _hostname(self.headers.get("Host")) in dash.allowed_hosts

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return _hostname(origin) in dash.allowed_hosts

            def _token_ok(self) -> bool:
                return secrets.compare_digest(
                    self.headers.get(TOKEN_HEADER) or "", dash.token)

            def do_GET(self):
                if not (self._host_ok() and self._origin_ok()):
                    self._send(403, b'{"error":"forbidden"}')
                    return
                if self.path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif self.path in ("/radar", "/radar.html"):
                    with open(os.path.join(STATIC, "radar.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif self.path == "/api/state":
                    self._send(200, json.dumps(dash.engine.state.snapshot()).encode())
                elif self.path == "/api/token":
                    self._send(200, json.dumps({"token": dash.token}).encode())
                elif self.path == "/api/daily":
                    from ..analytics import get_daily_summary
                    self._send(200, json.dumps(get_daily_summary(dash.runs_dir)).encode())
                elif self.path.startswith("/api/radar/feed"):
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    chain = params.get("chain", [None])[0]
                    new_only = params.get("new_only", ["0"])[0] in ("1", "true", "True")
                    evs = dash.radar.get_feed(chain, new_only=new_only) if dash.radar else []
                    self._send(200, json.dumps(evs, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/new_tokens"):
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    chain = params.get("chain", [None])[0]
                    payload = {
                        "tokens": dash.radar.get_new_tokens_summary(chain) if dash.radar else [],
                        "gate": dash.radar.get_gate_stats() if dash.radar else None,
                        "narratives": dash.radar.get_narrative_stats() if dash.radar else [],
                    }
                    self._send(200, json.dumps(payload, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/wallets"):
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    chain = params.get("chain", [None])[0]
                    wallets = dash.radar.wallet_db.list_wallets(chain) if dash.radar else []
                    self._send(200, json.dumps(wallets, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/query"):
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    chain = params.get("chain", ["SOL"])[0]
                    q = params.get("q", [""])[0]
                    res = dash.radar.query(chain, q) if dash.radar else {}
                    self._send(200, json.dumps(res, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/watchlist"):
                    items = dash.radar.get_watchlist() if dash.radar else []
                    self._send(200, json.dumps(items, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/picks"):
                    summary = (dash.radar.get_picks_summary() if dash.radar
                               else {"total": 0, "open": 0, "since_ts": None,
                                     "all": {}, "groups": [], "picks": []})
                    self._send(200, json.dumps(summary, ensure_ascii=False).encode())
                elif self.path.startswith("/api/radar/x_watch"):
                    items = []
                    try:
                        import glob as _glob
                        for p in sorted(_glob.glob(
                                os.path.join(X_WATCH_DIR, "x_watch_*.json"))):
                            try:
                                with open(p, encoding="utf-8") as f:
                                    items.append(json.load(f))
                            except (OSError, ValueError):
                                continue
                    except Exception:
                        pass
                    self._send(200, json.dumps(items, ensure_ascii=False).encode())
                elif self.path == "/api/stream":
                    self._stream()
                else:
                    self._send(404, b'{"error":"not found"}')

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                queue: deque = deque(maxlen=QUEUE_DEPTH)
                with dash.lock:
                    dash.clients.append(queue)
                try:
                    last = ""
                    next_ping = time.time() + PING_INTERVAL
                    while True:
                        with dash.lock:
                            data = queue.popleft() if queue else None
                        if data is not None:
                            if data != last:
                                self.wfile.write(f"data: {data}\n\n".encode())
                                self.wfile.flush()
                                last = data
                        elif time.time() >= next_ping:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            next_ping = time.time() + PING_INTERVAL
                        time.sleep(WRITE_INTERVAL)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with dash.lock:
                        if queue in dash.clients:
                            dash.clients.remove(queue)

            def _read_json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                for enc in ("utf-8", "gbk", "latin-1"):
                    try:
                        return json.loads(raw.decode(enc) or "{}")
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                try:
                    return json.loads(raw.decode("utf-8", errors="replace") or "{}")
                except Exception:
                    return {}

            def do_POST(self):
                if not self._host_ok():
                    self._send(403, b'{"error":"forbidden","reason":"host"}')
                    return
                if not self._origin_ok():
                    self._send(403, b'{"error":"forbidden","reason":"origin"}')
                    return
                if not self._token_ok():
                    self._send(403, b'{"error":"forbidden","reason":"token"}')
                    return
                if self.path == "/api/kill":
                    dash.engine.request_kill("dashboard kill switch")
                    self._send(200, b'{"ok":true,"killed":true,"queued":true}')
                elif self.path == "/api/resume":
                    dash.engine.request_resume()
                    self._send(200, b'{"ok":true,"resumed":true,"queued":true}')
                elif self.path == "/api/radar/wallets":
                    body = self._read_json_body()
                    wr = body.get("win_rate")
                    pr = body.get("pnl_usd")
                    w = dash.radar.wallet_db.add_wallet(
                        body.get("chain", "SOL"),
                        body.get("address", ""),
                        alias=body.get("alias", ""),
                        win_rate=float(wr) if wr not in (None, "") else None,
                        pnl_usd=float(pr) if pr not in (None, "") else None,
                        style=body.get("style", "自定义关注")
                    ) if dash.radar else {}
                    self._send(200, json.dumps({"ok": True, "wallet": w}, ensure_ascii=False).encode())
                elif self.path == "/api/radar/wallets/toggle":
                    body = self._read_json_body()
                    ok = dash.radar.wallet_db.toggle_wallet(
                        body.get("address", ""), bool(body.get("enabled", True))
                    ) if dash.radar else False
                    self._send(200, json.dumps({"ok": ok}).encode())
                elif self.path == "/api/radar/wallets/delete":
                    body = self._read_json_body()
                    ok = dash.radar.wallet_db.remove_wallet(body.get("address", "")) if dash.radar else False
                    self._send(200, json.dumps({"ok": ok}).encode())
                elif self.path == "/api/radar/watchlist":
                    body = self._read_json_body()
                    item = dash.radar.add_to_watchlist(
                        body.get("chain", "SOL"),
                        body.get("target", ""),
                        entry_price=float(body.get("entry_price", 0.0) or 0.0),
                        note=body.get("note", "")
                    ) if dash.radar else {}
                    self._send(200, json.dumps({"ok": True, "item": item}, ensure_ascii=False).encode())
                elif self.path == "/api/radar/watchlist/delete":
                    body = self._read_json_body()
                    ok = dash.radar.remove_from_watchlist(body.get("address", "")) if dash.radar else False
                    self._send(200, json.dumps({"ok": ok}).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

        return Handler

    def serve(self, host: str, port: int):
        self._allow_host(host)
        dash = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def shutdown(self):
                dash.stop_poller()
                super().shutdown()

            def server_close(self):
                dash.stop_poller()
                super().server_close()

        httpd = Server((host, port), self.make_handler())
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="dashboard").start()
        self.start_poller()
        return httpd
