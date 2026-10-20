"""HangGuard / Beat regressions (2026-09-17 freeze-series fix).

Fully offline: heartbeat files, staleness classification, boot grace and
the runner/engine wiring. The SIGKILL path itself is never executed here —
it only ever runs live, verified on the pm2 deployment.
"""
import os
import tempfile
import time
import unittest

from flygate.trader.engine import AppState
from flygate.trader.futures_runner import FuturesRunner
from flygate.trader.watchdog import Beat, HangGuard

from test_audit_fixes import FakeFutClient, mk_cfg


class TestBeat(unittest.TestCase):
    def test_beat_writes_fresh_parseable_file(self):
        d = tempfile.mkdtemp()
        hb = Beat(os.path.join(d, "hb.futures"))
        hb.beat()
        self.assertTrue(os.path.exists(hb.path))
        val = float(open(hb.path).read())
        self.assertLess(time.time() - val, 5)
        self.assertLess(time.time() - os.path.getmtime(hb.path), 5)

    def test_beat_survives_unwritable_path(self):
        hb = Beat("/definitely/not/a/dir/hb.x")
        hb.beat()                     # must raise nothing


class TestHangGuardStale(unittest.TestCase):
    def _guard(self, **kw):
        d = tempfile.mkdtemp()
        return HangGuard(d, **kw), d

    def test_fresh_heartbeat_is_not_stale(self):
        g, _ = self._guard(stall_s=180)
        g.register("futures").beat()
        self.assertEqual(g.stale(), {})

    def test_stale_heartbeat_is_reported_with_age(self):
        g, _ = self._guard(stall_s=180)
        hb = g.register("futures")
        hb.beat()
        old = time.time() - 200
        os.utime(hb.path, (old, old))
        stale = g.stale()
        self.assertIn("futures", stale)
        self.assertGreaterEqual(stale["futures"], 200)

    def test_boot_grace_covers_a_never_beaten_thread(self):
        g, _ = self._guard(stall_s=180, boot_grace_s=300)
        g.register("futures")                 # constructed, loop not yet run
        g._t0 = time.time() - 100             # 100s into boot: still grace
        self.assertEqual(g.stale(), {})
        g._t0 = time.time() - 400             # past grace, never beaten
        self.assertIn("futures", g.stale())

    def test_one_stalled_thread_flags_even_if_other_loops(self):
        # only the FUT thread hangs while the engine loop keeps beating:
        # a single shared file would mask it — per-thread files must not
        g, _ = self._guard(stall_s=180, boot_grace_s=300)
        fut = g.register("futures")
        g.register("engine").beat()
        fut.beat()
        old = time.time() - 999
        os.utime(fut.path, (old, old))
        g.register("engine").beat()
        self.assertEqual(set(g.stale()), {"futures"})


class TestRunnerWiring(unittest.TestCase):
    def test_runner_accepts_and_stores_heartbeat(self):
        d = tempfile.mkdtemp()
        hb = Beat(os.path.join(d, "hb.futures"))
        r = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(), d,
                          live=False, heartbeat=hb)
        self.assertIs(r.heartbeat, hb)

    def test_runner_default_has_no_heartbeat(self):
        r = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(),
                          tempfile.mkdtemp(), live=False)
        self.assertIsNone(r.heartbeat)

    def test_state_save_leaves_no_tmp_and_round_trips(self):
        # the atomic-rename save: no .tmp lingers, content still loads
        d = tempfile.mkdtemp()
        r = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(), d,
                          live=False)
        r.broker.size, r.extreme, r.n_today = 2, 123.4, 5
        r._save()
        self.assertFalse(os.path.exists(r._state_path + ".tmp"))
        r2 = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(), d,
                           live=False)
        self.assertEqual(r2.extreme, 123.4)
        self.assertEqual(r2.n_today, 5)


if __name__ == "__main__":
    unittest.main()
