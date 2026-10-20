"""Jev (TypeSafe System One) client for flygate.

硬约束（docs/UNIFIED.md §4）:
- stdlib only —— flygate 零新运行时依赖
- 确定性回放: 答案按 sha256(model + state + questions) 落盘缓存;
  模型钉死版本 id（jev-1.13.0），绝不用别名（jev-latest 随发版漂移）
- 永不阻塞交易循环: ask_or_none() 失败返回 None，调用方降级回 tilt 决策
- 重试只针对瞬态错误（429/5xx/网络）；4xx 立即失败
- 计费只按输入 token（输出免费），state 3k token ≈ $0.000126/次
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-1.13.0"  # pinned versioned id — never jev-latest (aliases drift)
DEFAULT_TIMEOUT_S = 8.0
RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504, 529})


class JevError(RuntimeError):
    """Transport/protocol failure. Callers degrade (ask_or_none), never crash the loop."""


class JevRetryable(JevError):
    """Transient failure worth retrying: 429/5xx/network."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class JevClient:
    """state + questions -> answers，带 append-only 磁盘缓存。

    cache key = sha256(canonical({model, state, questions}))。
    key 含 model —— 换版本自动 miss，绝不串答案；
    key 不含时间戳 —— 同一历史 state 的回放必然命中缓存（回测可复现）。
    """

    def __init__(self, transport=None, cache_path=None, model=DEFAULT_MODEL,
                 base_url=DEFAULT_BASE_URL, api_key=None, api_key_env="TYPESAFE_API_KEY",
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_retries: int = 2,
                 backoff_s: float = 0.5):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = float(backoff_s)
        self.cache_path = cache_path
        self._api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self._transport = transport if transport is not None else self._http_transport
        self._cache = None  # lazy load: {key: answers}

    # ---------------------------------------------------------------- public
    def ask(self, state, questions, use_cache: bool = True) -> dict:
        """返回 answers dict。网络/协议错误抛 JevError（调用方决定降级）。"""
        key = self.cache_key(state, questions)
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        resp = self._send({"model": self.model, "state": state, "questions": questions})
        answers = resp.get("answers") if isinstance(resp, dict) else None
        if not isinstance(answers, dict):
            raise JevError("response missing answers object")
        self._cache_put(key, answers)
        return answers

    def ask_or_none(self, state, questions, use_cache: bool = True,
                    log=None, tag: str = "JEV"):
        """永不抛错的包装：任何失败返回 None（引擎降级回 tilt 决策）。"""
        try:
            return self.ask(state, questions, use_cache=use_cache)
        except JevError as e:
            if log is not None:
                try:
                    log.info(tag, "jev_degrade", error=str(e))
                except Exception:
                    pass  # 遥测绝不打断交易循环（同 flygate 引擎原则）
            return None

    def cache_key(self, state, questions) -> str:
        blob = _canonical({"model": self.model, "state": state, "questions": questions})
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------- transport
    def _send(self, payload) -> dict:
        last: JevError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._transport(payload)
            except JevRetryable as e:  # 瞬态：退避重试
                last = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2 ** attempt))
        raise JevError(f"transport failed after {self.max_retries + 1} attempts: {last}")

    def _http_transport(self, payload) -> dict:
        req = urllib.request.Request(
            self.base_url + "/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_CODES:
                raise JevRetryable(f"http {e.code}") from None
            raise JevError(f"http {e.code}") from None  # 401/422 等：重试无意义
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise JevRetryable(f"network: {e}") from None

    # ----------------------------------------------------------------- cache
    def _load_cache(self) -> dict:
        if self._cache is None:
            self._cache = {}
            if self.cache_path and os.path.exists(self.cache_path):
                try:
                    with open(self.cache_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:  # 容错读：坏行跳过（同 flygate storage 传统）
                                row = json.loads(line)
                                self._cache[row["key"]] = row["answers"]
                            except (KeyError, TypeError, ValueError):
                                continue
                except OSError:
                    self._cache = {}
        return self._cache

    def _cache_get(self, key):
        return self._load_cache().get(key)

    def _cache_put(self, key, answers) -> None:
        self._load_cache()[key] = answers
        if not self.cache_path:
            return
        d = os.path.dirname(self.cache_path)
        if d:
            os.makedirs(d, exist_ok=True)
        # append-only jsonl：加载时坏行跳过，行级追加无需整体重写
        with open(self.cache_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "answers": answers},
                               ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
