"""Shared HTTP JSON helper for the radar.

One place for timeouts, retry/backoff, the User-Agent and failure logging so
the radar modules never hand-roll urllib again (and never fail silently).
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request

# 进程级兜底：radar 线程池里任何没带显式 timeout 的 socket 操作也 ≤15s。
# （getaddrinfo 不受此约束——残余挂起由引擎 HangGuard SIGKILL 兜底。）
socket.setdefaulttimeout(15)

log = logging.getLogger("flygate.radar")

USER_AGENT = "Mozilla/5.0 (FlyGate-Radar/1.0; contact: local)"


class HttpError(RuntimeError):
    """A JSON GET could not be completed (network error or bad payload)."""


def get_json(url: str, timeout: float = 4.0, retries: int = 1):
    """GET a URL and parse JSON. Raises HttpError after exhausting retries."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:            # network, timeout, HTTP, JSON decode
            last = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    log.warning("GET %s failed: %s", url, last)
    raise HttpError(str(last))


def post_json(url: str, payload: dict, timeout: float = 6.0, retries: int = 1):
    """POST a JSON body (JSON-RPC style endpoints) and parse the response."""
    last: Exception | None = None
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    log.warning("POST %s failed: %s", url, last)
    raise HttpError(str(last))
