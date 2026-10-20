"""export_from_flygate.py — 从 flygate 引擎的 runs/ 生成 flypnl 可公开数据。

只导出两类东西：
  1. 市场数据（三所资金费率时序——Gate 没有官方结算历史接口，这份自建时序是稀缺数据集）
  2. 虚拟组合曲线（A股ETF动量纸面组合，10万虚拟本金）

绝不导出：真实账户余额、巡检报告、.env、任何可关联身份的字段。
权益一律归一化为百分比（起点=0），绝对金额不出门。

用法：
  python3 tools/export_from_flygate.py --src /Users/mac/IdeaProjects/flygate/runs
产出：
  data/funding_series.csv     ts,exchange,contract,funding_rate,interval_h,funding_ann
  data/ashare_momentum.csv    date,value_index,mom60_nasdaq,holdings_code
  data/EXPORT_MANIFEST.md     本轮导出清单（行数、时间范围）
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import time


def load_jsonl(path: pathlib.Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def export_funding(src: pathlib.Path, dst: pathlib.Path) -> dict:
    rows = load_jsonl(src / "radar" / "funding.jsonl")
    kept = []
    for r in rows:
        if r.get("funding_rate") is None or not r.get("ts"):
            continue
        kept.append({
            "ts": r["ts"],
            "exchange": r.get("exchange", ""),
            "contract": r.get("contract", ""),
            "funding_rate": r["funding_rate"],
            "interval_h": r.get("interval_h", ""),
            "funding_ann": r.get("funding_ann", ""),
        })
    kept.sort(key=lambda x: (x["exchange"], x["contract"], x["ts"]))
    with dst.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(kept[0].keys()) if kept else
                           ["ts", "exchange", "contract", "funding_rate",
                            "interval_h", "funding_ann"])
        w.writeheader()
        w.writerows(kept)
    spans = {}
    for r in kept:
        k = f"{r['exchange']}:{r['contract']}"
        lo, hi, n = spans.get(k, (r["ts"], r["ts"], 0))
        spans[k] = (min(lo, r["ts"]), max(hi, r["ts"]), n + 1)
    return {"rows": len(kept), "series": len(spans), "spans": spans}


def export_ashare(src: pathlib.Path, dst: pathlib.Path) -> dict:
    rows = load_jsonl(src / "radar" / "ashare.jsonl")
    recs = [r for r in rows if "value" in r]
    recs.sort(key=lambda r: r["date"])
    base = recs[0]["value"] if recs else None
    out = []
    for r in recs:
        if base in (None, 0):
            break
        m60 = r.get("mom60") or {}
        out.append({
            "date": r["date"],
            "value_index": round((r["value"] / base - 1) * 100, 4),   # 起点=0%
            "mom60_nasdaq": (f"{m60.get('513100')*100:.4f}"
                             if m60.get("513100") is not None else ""),
            "holdings_code": "+".join(sorted(r.get("holdings") or {})) or "CASH",
        })
    with dst.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value_index",
                                          "mom60_nasdaq", "holdings_code"])
        w.writeheader()
        w.writerows(out)
    return {"rows": len(out)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/Users/mac/IdeaProjects/flygate/runs",
                    help="flygate 的 runs/ 目录（个人数据所在地，只读）")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).resolve().parent.parent / "data"))
    a = ap.parse_args()
    src, out = pathlib.Path(a.src), pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    fund = export_funding(src, out / "funding_series.csv")
    ashr = export_ashare(src, out / "ashare_momentum.csv")

    lines = [f"# 导出清单 — {time.strftime('%Y-%m-%d %H:%M')}", "",
             "## funding_series.csv（三所费率时序，公开市场数据）",
             f"- {fund['rows']} 行，{fund['series']} 个合约序列", ""]
    for k, (lo, hi, n) in sorted(fund["spans"].items(), key=lambda x: -x[1][2])[:12]:
        d0 = time.strftime("%Y-%m-%d", time.localtime(lo))
        d1 = time.strftime("%Y-%m-%d", time.localtime(hi))
        lines.append(f"- {k}: {n} 行（{d0} → {d1}）")
    lines += ["", "## ashare_momentum.csv（虚拟组合，起点=0%）",
              f"- {ashr['rows']} 个交易日", "",
              "> 隐私规则：百分比出门，绝对额不出门。真实余额/巡检/身份字段永不导出。"]
    (out / "EXPORT_MANIFEST.md").write_text("\n".join(lines) + "\n")
    print(f"[export] funding={fund['rows']} rows / ashare={ashr['rows']} rows -> {out}")


if __name__ == "__main__":
    main()
