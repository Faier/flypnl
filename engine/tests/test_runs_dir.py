"""FLYGATE_RUNS: a second deployment must never share the paper show's state
directory.

Rationale (2026-10-18): the planned "50 USDT real-money satellite" has to run
BESIDE the paper main show. Every state file is keyed by filename only —
`engine.lock` (single-instance interlock), `{pair}.broker.json` (cash/position),
`{pair}.brain.npz`, `risk.json`, `KILL` — so two processes in one directory
either refuse to start (the lock) or silently clobber each other's equity.
`default_runs_dir()` (used by Engine, the CLI and the dashboard) reads
FLYGATE_RUNS to keep them apart.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

from flygate.config import Config
from flygate.storage import default_runs_dir
from flygate.trader.engine import Engine


class TestDefaultRunsDir(unittest.TestCase):
    def test_default_points_at_the_repo_runs_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLYGATE_RUNS", None)
            p = default_runs_dir()
        self.assertEqual(os.path.basename(p), "runs")
        # sibling of the package: <repo>/runs, not <repo>/flygate/runs
        self.assertTrue(os.path.isdir(p))
        self.assertEqual(os.path.dirname(p), os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))

    def test_env_override_wins_and_expands(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FLYGATE_RUNS"] = d
            try:
                self.assertEqual(default_runs_dir(), os.path.abspath(d))
            finally:
                os.environ.pop("FLYGATE_RUNS", None)

    def test_engine_honours_the_env_dir(self):
        cfg = Config()
        cfg.pairs = []
        cfg.futures = {}
        client = types.SimpleNamespace(key="")
        with tempfile.TemporaryDirectory() as d:
            os.environ["FLYGATE_RUNS"] = d
            try:
                e = Engine(cfg, live=False, client=client)
                try:
                    self.assertEqual(e.runs, os.path.abspath(d))
                    self.assertTrue(
                        os.path.exists(os.path.join(d, "engine.lock")))
                finally:
                    e.stop()
            finally:
                os.environ.pop("FLYGATE_RUNS", None)


if __name__ == "__main__":
    unittest.main()
