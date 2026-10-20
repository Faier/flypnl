"""Jev 客户端离线测试：缓存确定性、版本钉死、重试/降级。全 mock transport，无网络。

（从 flygate-extensions 移植，导入路径适配 flygate 包。）
"""
import json
import os
import tempfile
import unittest

from flygate.decide.jev import JevClient, JevError, JevRetryable

ANSWERS = {"direction": {"choice": "flat",
                         "probabilities": {"long": 0.2, "flat": 0.6, "short": 0.2},
                         "confidence": 0.9}}
STATE = {"diff_hz": 3.3, "r2_wide": 0.91}
QUESTIONS = {"direction": {"type": "choice"}}


class FakeTransport:
    """可编程假传输：前 fail_times 次抛可重试错误，之后返回固定答案。"""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times
        self.payloads = []

    def __call__(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if self.calls <= self.fail_times:
            raise JevRetryable("http 500")
        return {"model": payload["model"], "answers": ANSWERS,
                "usage": {"input_tokens": 10, "output_tokens": 0}}


class DeadTransport:
    """永远失败（不可重试）。"""
    calls = 0

    def __call__(self, payload):
        type(self).calls += 1
        raise JevError("http 401")


class TestCacheDeterminism(unittest.TestCase):
    def test_same_state_asks_once(self):
        with tempfile.TemporaryDirectory() as d:
            t = FakeTransport()
            c = JevClient(transport=t, cache_path=os.path.join(d, "cache.jsonl"),
                          backoff_s=0.0)
            a1 = c.ask(STATE, QUESTIONS)
            a2 = c.ask(STATE, QUESTIONS)
            self.assertEqual(a1, ANSWERS)
            self.assertEqual(a1, a2)
            self.assertEqual(t.calls, 1)  # 第二次命中缓存

    def test_cache_survives_new_client(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.jsonl")
            t1, t2 = FakeTransport(), FakeTransport()
            JevClient(transport=t1, cache_path=path, backoff_s=0.0).ask(STATE, QUESTIONS)
            JevClient(transport=t2, cache_path=path, backoff_s=0.0).ask(STATE, QUESTIONS)
            self.assertEqual(t2.calls, 0)  # 新客户端从磁盘命中 → 回放零成本

    def test_cache_file_is_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.jsonl")
            c = JevClient(transport=FakeTransport(), cache_path=path, backoff_s=0.0)
            c.ask(STATE, QUESTIONS)
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f if x.strip()]
            self.assertEqual(len(rows), 1)
            self.assertIn("key", rows[0])
            self.assertIn("answers", rows[0])


class TestPinnedModel(unittest.TestCase):
    def test_pinned_version_sent(self):
        t = FakeTransport()
        JevClient(transport=t, cache_path=None).ask(STATE, QUESTIONS)
        self.assertEqual(t.payloads[0]["model"], "jev-1.13.0")

    def test_model_is_part_of_cache_key(self):
        with tempfile.TemporaryDirectory() as d:
            t = FakeTransport()
            c1 = JevClient(transport=t, cache_path=os.path.join(d, "c.jsonl"),
                           model="jev-1.13.0", backoff_s=0.0)
            c2 = JevClient(transport=t, cache_path=os.path.join(d, "c.jsonl"),
                           model="jev-9.9.9", backoff_s=0.0)
            c1.ask(STATE, QUESTIONS)
            c2.ask(STATE, QUESTIONS)
            self.assertEqual(t.calls, 2)  # 换版本必须重新问，绝不串答案


class TestDegrade(unittest.TestCase):
    def test_ask_or_none_none_on_persistent_failure(self):
        t = FakeTransport(fail_times=99)
        c = JevClient(transport=t, cache_path=None, max_retries=2, backoff_s=0.0)
        self.assertIsNone(c.ask_or_none(STATE, QUESTIONS))
        self.assertEqual(t.calls, 3)  # 1 次原始 + 2 次重试

    def test_non_retryable_fails_fast(self):
        DeadTransport.calls = 0
        c = JevClient(transport=DeadTransport(), cache_path=None,
                      max_retries=2, backoff_s=0.0)
        with self.assertRaises(JevError):
            c.ask(STATE, QUESTIONS)
        self.assertEqual(DeadTransport.calls, 1)  # 4xx 不重试

    def test_retry_then_success(self):
        t = FakeTransport(fail_times=1)
        c = JevClient(transport=t, cache_path=None, max_retries=2, backoff_s=0.0)
        self.assertIsNotNone(c.ask(STATE, QUESTIONS))
        self.assertEqual(t.calls, 2)

    def test_ask_raises_when_transport_dead(self):
        c = JevClient(transport=FakeTransport(fail_times=99), cache_path=None,
                      max_retries=1, backoff_s=0.0)
        with self.assertRaises(JevError):
            c.ask(STATE, QUESTIONS)


class TestBadResponse(unittest.TestCase):
    def test_missing_answers_raises(self):
        c = JevClient(transport=lambda p: {"unexpected": True}, cache_path=None)
        with self.assertRaises(JevError):
            c.ask(STATE, QUESTIONS)


if __name__ == "__main__":
    unittest.main()
