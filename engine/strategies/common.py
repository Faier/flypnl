"""雷达实验公共库（纯 stdlib，无三方依赖）。

设计纪律（与 flygate 主引擎一致）：
  - 只读观察优先：所有雷达只发公开 GET，绝不下单、绝不带密钥
  - 网络要抗造：超时 + 退避重试，扫描失败记 scan_error 而不是崩循环
  - 证据落盘：每次扫描追加 jsonl（时间序列），每日重写一份 md 摘要
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

RUNS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "runs", "radar")
UA = {"User-Agent": "flygate-radar/0.1"}


def http_json(url: str, timeout: float = 25, retries: int = 3, headers: dict | None = None):
    """GET -> dict/list；失败重试（指数退避），全败抛最后一个异常。

    每次调用放在守护线程里 join(timeout+5)：urlopen 的 timeout 管不住
    getaddrinfo（DNS 解析可以无限挂起——果蝇引擎 09-14..17 四次冻结的同款
    病），雷达没有看门狗进程可依，必须在调用层兜底。挂死的线程被遗弃
    （daemon，随进程死亡），循环继续转。
    """
    import threading
    last: Exception = TimeoutError("unreachable")
    for i in range(retries):
        result: dict = {}

        def _work():
            try:
                req = urllib.request.Request(url, headers=headers or UA)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    result["data"] = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:      # noqa: BLE001 — 记录后在线程外重试
                result["err"] = e

        th = threading.Thread(target=_work, daemon=True, name="http_json")
        th.start()
        th.join(timeout + 5.0)
        if "data" in result:
            return result["data"]
        last = result.get("err") or TimeoutError(f"no response in {timeout+5:.0f}s (DNS hang?)")
        time.sleep(2.0 * (i + 1))
    raise last


def append_jsonl(name: str, rec: dict):
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, name), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl_tail(name: str, n: int = 2000) -> list:
    p = os.path.join(RUNS, name)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, errors="replace") as f:
        for line in f.readlines()[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def write_report(name: str, text: str):
    rep = os.path.join(RUNS, "reports")
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(rep, name), "w") as f:
        f.write(text)


def read_state(name: str, default: dict) -> dict:
    p = os.path.join(RUNS, name)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return default


def write_state(name: str, d: dict):
    p = os.path.join(RUNS, name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, p)


def fmt_pct(x) -> str:
    return f"{x * 100:+.2f}%" if isinstance(x, (int, float)) else "—"


def fmt_ts(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
