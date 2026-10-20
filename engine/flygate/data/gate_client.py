"""Thin Gate.io APIv4 REST client.

Public endpoints (klines, tickers) need no keys.
Private endpoints (balances, orders) use the APIv4 HMAC-SHA512 signature:

    sign_string = METHOD \\n PATH \\n QUERY_STRING \\n hex(sha512(BODY)) \\n TIMESTAMP
    header SIGN = hex(hmac_sha512(secret, sign_string))

Live trading requires keys with SPOT TRADE permission only — never withdrawal.
Paper mode (default) never touches private endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

BASE = "https://api.gateio.ws/api/v4"

# NEVER route engine traffic through the system proxy by default: a
# half-open local proxy would kill the engine. But when the machine's
# DIRECT path dies (proxy app hijacking DNS, router outage), fall back to
# whatever route the OS provides. Only DNS-class errors are retried on the
# alternate opener — those are provably pre-send, so an order POST can never
# be duplicated by the retry.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_FALLBACK_OPENER = urllib.request.build_opener()      # honors env proxies
_DNS_ERRORS = ("nodename nor servname", "Name or service not known",
               "Network is unreachable", "Temporary failure in name resolution")


class GateError(RuntimeError):
    pass


def _fmt_amount(amount: float, precision: int | None = None) -> str:
    """Order amount as a PLAIN decimal string — never scientific notation.

    Gate's API takes amounts as decimal strings; `f"{amount:.10g}"` switches to
    e-notation below 1e-4, so a dust sell left the machine as
    `"amount": "2e-05"` and the exchange rejected it. The callers of this are
    the risk-REDUCING exits and `_close_all`, which only logs failures — the
    position then stayed open through a halt (2026-09-15 audit).

    `precision` is the pair's amount_precision when it is known (same
    semantics as GateBroker._fmt: fixed decimals, integer string at 0); when
    it is unknown we fall back to 8 decimals with the trailing zeros trimmed,
    which is still a plain decimal and never wider than Gate's widest spot
    amount precision."""
    if precision is not None:
        p = max(0, int(precision))
        txt = f"{amount:.{p}f}" if p > 0 else str(int(amount))
    else:
        txt = f"{amount:.8f}".rstrip("0").rstrip(".")
        txt = txt or "0"
    if "e" in txt or "E" in txt:            # belt and braces: never e-notation
        txt = f"{amount:.8f}".rstrip("0").rstrip(".") or "0"
    return txt


class GateClient:
    def __init__(self, key: str = "", secret: str = "", base: str = BASE):
        self.key = key.strip()
        self.secret = secret.strip()
        self.base = base
        self._all_pairs = None          # cached /spot/currency_pairs map
        # paper-mode tripwire: when True, any private WRITE (order, cancel,
        # leverage...) is refused before it can leave the machine. Set by the
        # engine whenever it runs in paper mode (2026-09-15 audit: the paper
        # CLI still reached the live futures order path)
        self.read_only = False

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, query: dict | None = None,
                 body: dict | None = None, auth: bool = False, timeout: float = 10.0):
        # 有界性说明：urlopen(timeout) 覆盖 connect/recv，但 getaddrinfo(DNS)
        # 不受任何 urllib 超时约束——这是 10 月冻结风暴的病根。工程处置：
        # 进程级 socket.setdefaulttimeout(15)（__main__.main）+ HangGuard 心跳
        # 看门狗对挂起线程 SIGKILL 重启（runs/hb.*，已在 trader/watchdog.py）。
        if self.read_only and auth and method.upper() != "GET":
            raise GateError(f"read-only client: private write blocked "
                            f"({method} {path}) — paper mode never orders")
        q = urllib.parse.urlencode(query) if query else ""
        url = self.base + path + (("?" + q) if q else "")
        data = None
        headers = {"Accept": "application/json",
                   "User-Agent": "flygate/0.1"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            if not (self.key and self.secret):
                raise GateError("API key/secret required for private endpoints")
            ts = str(int(time.time()))
            payload_hash = hashlib.sha512(data or b"").hexdigest()
            # Gate signs the FULL path incl. the /api/v4 prefix (per docs example)
            prefix = urllib.parse.urlparse(self.base).path.rstrip("/")
            sign_str = "\n".join([method.upper(), prefix + path, q, payload_hash, ts])
            sign = hmac.new(self.secret.encode(), sign_str.encode(),
                            hashlib.sha512).hexdigest()
            headers.update({"Timestamp": ts, "KEY": self.key, "SIGN": sign})
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise GateError(f"HTTP {e.code} {method} {path}: {detail}") from None
        except Exception as e:
            if not any(s in str(e) for s in _DNS_ERRORS):
                raise
            # direct path can't even resolve — request never sent, safe to
            # retry via the OS route (proxied if the user runs one)
            with _FALLBACK_OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "null")

    # ---------------- public ----------------
    def ticker(self, pair: str) -> dict:
        return self._request("GET", "/spot/tickers", query={"currency_pair": pair})[0]

    def pair_rules(self, pair: str) -> dict:
        """Trading rules for one pair: precision, min amounts, trade status.
        NOTE: /spot/currency_pairs ignores the currency_pair filter and always
        returns the whole list, so we fetch once and filter client-side.
        The cache is only accepted when the list looks complete — a network
        glitch returning an empty/partial list must not poison it forever."""
        if self._all_pairs is None or len(self._all_pairs) < 100:
            for attempt in range(3):
                try:
                    m = {r["id"]: r for r in
                         self._request("GET", "/spot/currency_pairs")}
                    if len(m) >= 100:
                        self._all_pairs = m
                        break
                except GateError:
                    raise
                except Exception:
                    pass
                time.sleep(1.0)
        if not self._all_pairs:
            raise GateError("currency pair list unavailable (network?)")
        r = self._all_pairs.get(pair)
        if r is None:
            # don't trust a possibly-partial cache for THIS pair: refetch once
            self._all_pairs = None
            raise GateError(f"pair {pair} not in rules cache — refetching next call")
        return {"amount_precision": int(r.get("amount_precision", 8)),
                "precision": int(r.get("precision", 8)),
                "min_base_amount": float(r.get("min_base_amount", 0) or 0),
                "min_quote_amount": float(r.get("min_quote_amount", 0) or 0),
                "trade_status": r.get("trade_status", "tradable"),
                "fee": float(r.get("fee", "0.2") or 0.2)}

    def klines(self, pair: str, interval: str = "1m", _from: int | None = None,
               _to: int | None = None, limit: int = 1000) -> list:
        """CLOSED candles as [[ts, open, high, low, close, base_volume], ...]
        ascending. Raw Gate rows are [ts, quote_vol, close, high, low, open,
        base_vol, window_closed]; forming candles are dropped."""
        q = {"currency_pair": pair, "interval": interval, "limit": limit}
        if _from is not None:
            q["from"] = int(_from)
        if _to is not None:
            q["to"] = int(_to)
        raw = self._request("GET", "/spot/candlesticks", query=q) or []
        out = []
        for r in raw:
            if len(r) >= 8 and str(r[7]).lower() == "false":
                continue                            # still forming
            out.append([r[0], r[5], r[3], r[4], r[2], r[6] if len(r) > 6 else 0.0])
        return out

    # ---------------- private ----------------
    def balances(self) -> list:
        return self._request("GET", "/spot/accounts", auth=True)

    def create_market_order(self, pair: str, side: str, amount: float,
                            quote_order: bool | None = None) -> dict:
        """Spot market order. For buys amount is quote units (auto-buy by cost),
        for sells amount is base units (auto-sell by size). Callers round to the
        pair's precision first (see GateBroker).

        The amount is written as a PLAIN decimal at the pair's precision (see
        _fmt_amount) — an e-notation amount ("2e-05") is rejected by Gate and
        used to strand a position through a halt. An amount that rounds to zero
        is refused here instead of being sent as "0" (which Gate rejects, but
        only after a wasted round-trip on the exit path)."""
        txt = _fmt_amount(amount, self._amount_precision(pair))
        if float(txt) <= 0:
            raise GateError(
                f"refusing {side} market order on {pair}: amount {amount!r} "
                f"rounds to {txt!r} — nothing to send")
        body = {"currency_pair": pair, "type": "market", "side": side,
                "time_in_force": "ioc", "amount": txt}
        return self._request("POST", "/spot/orders", body=body, auth=True)

    def _amount_precision(self, pair: str) -> int | None:
        """amount_precision from the ALREADY-CACHED pair rules, or None.

        Deliberately never fetches: the order path (an exit during a halt) must
        not gain a new network dependency, and GateBroker warms this cache with
        its own `rules()` call before it orders."""
        r = (self._all_pairs or {}).get(pair)
        if not isinstance(r, dict):
            return None
        try:
            p = r.get("amount_precision")
            return int(p) if p is not None else None
        except (TypeError, ValueError):
            return None

    def order(self, pair: str, order_id: str) -> dict:
        return self._request("GET", f"/spot/orders/{order_id}",
                             query={"currency_pair": pair}, auth=True)

    def my_trades(self, pair: str, limit: int = 100) -> list:
        """Real fill history from the exchange (authoritative, survives any
        local restart/wipe). Returns newest-first."""
        return self._request("GET", "/spot/my_trades",
                             query={"currency_pair": pair, "limit": limit},
                             auth=True) or []

    def open_orders(self, pair: str | None = None) -> list:
        q = {"status": "open"}
        if pair:
            q["currency_pair"] = pair
        return self._request("GET", "/spot/orders", query=q, auth=True)

    def cancel_order(self, pair: str, order_id: str) -> dict:
        return self._request("DELETE", f"/spot/orders/{order_id}",
                             query={"currency_pair": pair}, auth=True)


INTERVALS = {"10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
             "1h": 3600, "4h": 14400, "1d": 86400}
