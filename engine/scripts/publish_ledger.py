"""Publish the public Season-1 ledger: one hash-chained JSON per day plus a
human-readable cumulative table, optionally committed to git.

Why a hash CHAIN: "trust me, the equity was 730" is a screenshot; a chain is a
record. Each day's file carries the SHA-256 of its own canonical content and the
previous day's digest as `prev`. Editing any past day breaks every later link,
so silent history edits become detectable — which is the only reason a paper
experiment can be more convincing than a broker screenshot.

What it reads (per --runs directory):
  {pair}.broker.json          main fly cash/base      (paper or live)
  shadow.{pair}.broker.json   frozen twin cash/base
  fut.state.json              futures fly state       (size/cash/entry)
  KILL                        present -> halted
  config.json                 copied by digest only (never inlined)
It writes:
  <out>/<YYYY-MM-DD>.json     the day's record (state digests + hash chain)
  <out>/LEDGER.md             cumulative table across all recorded days
and, with --git, commits + pushes <out>.

Usage:
  python3 scripts/publish_ledger.py [--out ledger] [--runs runs]...
      [--runs-dir-flag-repeatable] [--git] [--date YYYY-MM-DD] [--print]
Never prints or copies API keys: broker state files do not contain them, and
config is recorded as a digest, not as a copy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flygate.storage import atomic_write_json, load_json, default_runs_dir


def _digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _file_digest(path: str) -> str:
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_days(out_dir: str) -> dict[str, dict]:
    days = {}
    if not os.path.isdir(out_dir):
        return days
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".json") and len(name) == 15:      # YYYY-MM-DD.json
            rec = load_json(os.path.join(out_dir, name))
            if isinstance(rec, dict) and rec.get("date"):
                days[rec["date"]] = rec
    return days


def _pairs_in(runs: str) -> list[str]:
    pairs = []
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        if name.endswith(".broker.json") and not name.startswith("shadow."):
            pairs.append(name[: -len(".broker.json")])
    return pairs


def _equity(rec: dict, px: float) -> float:
    """Equity of a spot broker state. `base` valued at the last known price is
    unknown here (no price feed in the ledger), so the ledger records BOTH the
    raw cash and the base amount — recomputing equity with an external price is
    exactly the 'anyone can recompute' property §5 of the protocol claims."""
    return float(rec.get("cash") or 0.0)


def collect_day(runs_dirs: list[str], date: str) -> dict:
    arms: dict[str, dict] = {}
    kill = False
    for runs in runs_dirs:
        label = os.path.basename(os.path.dirname(runs.rstrip("/"))) or runs
        for pair in _pairs_in(runs):
            for prefix, arm, suffix in (("", "learning", ""),
                                        ("shadow.", "shadow", "#shadow")):
                p = os.path.join(runs, f"{prefix}{pair}.broker.json")
                rec = load_json(p, {})
                arms[f"{label}/{pair}{suffix}"] = {
                    "arm": arm,
                    "pair": pair,
                    "runs": label,
                    "cash": round(float(rec.get("cash") or 0.0), 6),
                    "base": round(float(rec.get("base") or 0.0), 10),
                    "trades": len(rec.get("trades") or []),
                    "file_sha256": _file_digest(p),
                }
        for fname in ("fut.state.json", "fut.state.paper.json"):
            fp = os.path.join(runs, fname)
            st = load_json(fp, {})
            if st:
                arms[f"{label}/{fname}"] = {
                    "arm": "futures",
                    "runs": label,
                    "cash": round(float(st.get("cash") or 0.0), 6),
                    "size": int(st.get("size") or 0),
                    "entry": float(st.get("entry") or 0.0),
                    "file_sha256": _file_digest(fp),
                }
        kill = kill or os.path.exists(os.path.join(runs, "KILL"))
    return {"date": date, "kill_switch": kill, "arms": arms}


def render_md(days: dict[str, dict]) -> str:
    lines = ["# FlyGate Season 1 — public ledger", "",
             "Hash-chained daily record. Every row is recomputable from the "
             "broker state files named in the matching JSON. Rules: "
             "[`docs/SEASON1_PREREG.md`](../docs/SEASON1_PREREG.md).", ""]
    if not days:
        lines.append("_(no days recorded yet)_")
        return "\n".join(lines) + "\n"
    dates = sorted(days)
    # arms present in the most recent N days
    arms = sorted({k for d in dates for k in days[d]["arms"]})
    days_cols = dates[-14:]
    lines.append(f"## Cash by arm (last {len(days_cols)} day(s))")
    lines.append("")
    lines.append("| arm | " + " | ".join(d[5:] for d in days_cols) + " |")
    lines.append("|---|" + "---|" * len(days_cols))
    for a in arms:
        cells = []
        for d in days_cols:
            rec = days[d]["arms"].get(a)
            cells.append(f"{rec['cash']:.2f}" if rec else "—")
        lines.append(f"| {a} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Shadow A/B gap (learning − shadow, cash)")
    lines.append("")
    lines.append("Only pairs where BOTH arms are flat (base = 0) are comparable "
                 "in cash; a held position needs an external price to value, so "
                 "such pairs print `(holding)`. This is the pre-registered "
                 "primary endpoint, day 30.")
    lines.append("")
    last = days[dates[-1]]["arms"]
    gap_pairs = sorted({
        k.rsplit("#", 1)[0].split("/", 1)[-1] for k in last
        if k.endswith("#shadow")
        and last[k]["cash"] > 0            # a 0-cash twin is not a real arm
        and last[k.rsplit("#", 1)[0]] and last[k.rsplit("#", 1)[0]]["cash"] > 0
    })
    lines.append("| date | " + " | ".join(gap_pairs) + " |")
    lines.append("|---|" + "---|" * len(gap_pairs))
    for d in days_cols:
        cells = []
        for pair in gap_pairs:
            got = next((v for k, v in days[d]["arms"].items()
                        if k.endswith("/" + pair) and not k.endswith("#shadow")),
                       None)
            sh = next((v for k, v in days[d]["arms"].items()
                       if k.endswith("/" + pair + "#shadow")), None)
            if not got or not sh:
                cells.append("—")
            elif got["base"] or sh["base"]:
                cells.append("(holding)")
            else:
                cells.append(f"{got['cash'] - sh['cash']:+.2f}")
        lines.append(f"| {d[5:]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Chain")
    lines.append("")
    lines.append("| date | digest | prev | kill |")
    lines.append("|---|---|---|---|")
    for d in dates:
        r = days[d]
        lines.append(f"| {d} | `{r.get('digest','')[:16]}…` | "
                     f"`{(r.get('prev') or '—')[:16]}` | "
                     f"{'⚠️ HALT' if r.get('kill_switch') else ''} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", action="append", default=None,
                    help="state dir to include (repeatable). Default: "
                         "the repo runs/ dir, plus $FLYGATE_RUNS when set")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ledger"))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--git", action="store_true",
                    help="git add/commit/push the ledger directory")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="print the day record JSON to stdout")
    args = ap.parse_args(argv)

    runs_dirs = args.runs or []
    default_dir = default_runs_dir()
    if default_dir not in runs_dirs:
        runs_dirs.insert(0, default_dir)
    runs_dirs = [os.path.abspath(os.path.expanduser(d)) for d in runs_dirs]

    date = args.date or dt.date.today().isoformat()
    days = _load_days(args.out)
    prev = days[max(days)]["digest"] if days else ""
    rec = collect_day(runs_dirs, date)
    rec["prev"] = prev
    rec["digest"] = _digest(rec)
    atomic_write_json(rec, os.path.join(args.out, f"{date}.json"))
    with open(os.path.join(args.out, "LEDGER.md"), "w", encoding="utf-8") as f:
        f.write(render_md({**days, date: rec}))

    n = len(rec["arms"])
    print(f"[ledger] {date}: {n} arms from {len(runs_dirs)} runs dir(s) "
          f"-> {os.path.join(args.out, date + '.json')}")
    print(f"[ledger] digest={rec['digest'][:16]}… prev={(prev or '—')[:16]}")
    if args.do_print:
        print(json.dumps(rec, indent=1, ensure_ascii=False))

    if args.git:
        try:
            subprocess.run(["git", "-C", args.out, "add", "-A"], check=True)
            msg = f"ledger {date} digest={rec['digest'][:12]}"
            r = subprocess.run(["git", "-C", args.out, "commit", "-m", msg],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[ledger] nothing to commit ({r.stdout.strip()[:80]})")
            else:
                print(f"[ledger] committed: {msg}")
            p = subprocess.run(["git", "-C", args.out, "push"],
                               capture_output=True, text=True)
            if p.returncode != 0:
                print("[ledger] push failed (no remote?):",
                      (p.stderr or p.stdout).strip()[:200])
        except subprocess.CalledProcessError as e:
            print(f"[ledger] git step failed: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
