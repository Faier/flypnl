"""防前视测试：白名单导出 + 泄漏自检。带日期/币名的 state 必须被拦下。"""
import unittest

from flygate.decide.state import (build_state, default_questions,
                                  validate_no_identifiers)


class Tel(dict):
    """模拟 flybrain.observe() 的遥测 dict（含该被滤掉的字段）。"""


SAMPLE_TEL = Tel({
    "ts": 1728345600,            # 绝对时间 → 必须丢弃
    "pair": "BTC_USDT",          # 币名 → 必须丢弃
    "price": 67890.5,            # 绝对价格 → 必须丢弃
    "equity": 340.0,             # 账户上下文 → 必须丢弃
    "diff_hz": 3.3, "r2_wide": 0.91, "kc_active": 137,
    "hs_up": 0.33, "hs_dn": 0.07, "mbon_margin": 0.006,
    "note": "just broke out!",   # 未知字符串字段 → 白名单外，丢弃
})


class TestBuildState(unittest.TestCase):
    def test_whitelist_only(self):
        s = build_state(SAMPLE_TEL)
        self.assertNotIn("ts", s)
        self.assertNotIn("pair", s)
        self.assertNotIn("price", s)
        self.assertNotIn("equity", s)
        self.assertNotIn("note", s)
        self.assertEqual(s["diff_hz"], 3.3)
        self.assertEqual(s["kc_active"], 137)

    def test_holding_and_unrealized(self):
        s = build_state(SAMPLE_TEL, holding="long", unrealized_pct=1.23)
        self.assertEqual(s["holding"], "long")
        self.assertEqual(s["unrealized_pct"], 1.23)

    def test_bad_holding_rejected(self):
        self.assertNotIn("holding", build_state(SAMPLE_TEL, holding="yolo"))

    def test_nonfinite_dropped(self):
        s = build_state({"diff_hz": float("nan"), "r2_wide": float("inf"),
                         "kc_active": 5})
        self.assertNotIn("diff_hz", s)
        self.assertNotIn("r2_wide", s)
        self.assertIn("kc_active", s)

    def test_extra_only_via_whitelist(self):
        s = build_state(SAMPLE_TEL, extra={"window_ret_pct": 0.8, "evil": 1.0})
        self.assertIn("window_ret_pct", s)
        self.assertNotIn("evil", s)


class TestLeakGate(unittest.TestCase):
    def test_clean_state_passes(self):
        self.assertEqual(validate_no_identifiers(build_state(SAMPLE_TEL)), [])

    def test_date_like_string_flagged(self):
        bad = {"note": "as of 2026-10-08 the trend was up"}
        v = validate_no_identifiers(bad)
        self.assertTrue(any("date-like" in x for x in v))

    def test_symbol_like_string_flagged(self):
        bad = {"note": "BTC_USDT broke out"}
        v = validate_no_identifiers(bad)
        self.assertTrue(any("symbol-like" in x for x in v))

    def test_denied_key_flagged(self):
        v = validate_no_identifiers({"Price": 123.0})  # 大小写不敏感
        self.assertTrue(v)

    def test_nested_walk(self):
        v = validate_no_identifiers({"a": {"b": ["2026-01-01"]}})
        self.assertTrue(v)


class TestQuestionsTemplate(unittest.TestCase):
    def test_three_question_types(self):
        q = default_questions()
        self.assertEqual(q["direction"]["type"], "choice")
        self.assertEqual(q["strength"]["type"], "score")
        self.assertEqual(q["valid_setup"]["type"], "noul")

    def test_english_only(self):
        """硬约束：提问模板不得含 CJK（官方明说 CJK 弱于英文）。"""
        def strings(o):
            if isinstance(o, str):
                yield o
            elif isinstance(o, dict):
                for v in o.values():
                    yield from strings(v)
            elif isinstance(o, (list, tuple)):
                for v in o:
                    yield from strings(v)
        for s in strings(default_questions()):
            self.assertTrue(all(ord(ch) < 128 for ch in s), f"CJK found: {s}")


if __name__ == "__main__":
    unittest.main()
