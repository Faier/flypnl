"""make_retina_chart.py — 签名图：真实K线 vs 果蝇视网膜渲染（1200×675 推特尺寸）。

左：你看的图（真实蜡烛）。右：蝇看的图（48×27 像素，价格/余额/盘口一概不存在）。
渲染直接调引擎的 render_chart()（含 6× 垂直超采样 + 光学模糊），保证与生产一致。

用法：
  python3 tools/make_retina_chart.py [--pair BTC_USDT] [--out charts/retina-compare.png]
零第三方依赖（仅 PIL + 引擎 numpy 渲染函数）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import ssl
import sys
import urllib.request

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

import numpy as np                                          # noqa: E402
from PIL import Image, ImageDraw, ImageFont                 # noqa: E402
from flygate.brain.flybrain import render_chart             # noqa: E402

W, H = 1200, 675
BG = (13, 17, 23)
PANEL = (22, 27, 34)
FG = (139, 148, 158)
GREEN = (35, 134, 54)
RED = (248, 81, 73)
RETINA_TINT = (88, 166, 255)

CHART_W, CHART_H, WINDOW = 48, 27, 48      # 与生产配置一致的视网膜网格


def font(size: int):
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fetch_candles(pair: str, limit: int) -> list:
    url = (f"https://api.gateio.ws/api/v4/spot/candlesticks"
           f"?currency_pair={pair}&interval=1m&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "flypnl/0.1"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
        raw = json.loads(r.read())
    # Gate v4: [ts, quote_vol, close, high, low, open, base_vol, _]
    return [[int(x[0]), float(x[5]), float(x[3]), float(x[4]), float(x[2]), float(x[1])]
            for x in raw]


def draw_real_panel(img: ImageDraw.ImageDraw, box, candles) -> None:
    x0, y0, x1, y1 = box
    data = candles[-WINDOW:]
    img.rectangle(box, fill=PANEL)
    hi = max(c[2] for c in data); lo = min(c[3] for c in data)
    span = (hi - lo) or 1e-9
    pad = 12
    cw = (x1 - x0 - 2 * pad) / len(data)
    y_of = lambda p: y0 + pad + (hi - p) / span * (y1 - y0 - 2 * pad)
    for i, (ts, o, h, l, c, v) in enumerate(data):
        x = x0 + pad + i * cw + cw / 2
        col = GREEN if c >= o else RED
        img.line([(x, y_of(h)), (x, y_of(l))], fill=col, width=1)
        top, bot = sorted((y_of(o), y_of(c)))
        img.rectangle([x - cw * 0.3, top, x + cw * 0.3, max(bot, top + 1)], fill=col)


def draw_retina_panel(img: ImageDraw.ImageDraw, box, lum, col) -> None:
    x0, y0, x1, y1 = box
    img.rectangle(box, fill=(8, 12, 18))
    h, w = lum.shape
    cell_w = (x1 - x0 - 24) / w
    cell_h = (y1 - y0 - 24) / h
    for r in range(h):
        for c in range(w):
            L = float(lum[r, c]); C = float(col[r, c])
            if L <= 0.02:
                continue
            base = tuple(int(v * (0.35 + 0.65 * L)) for v in RETINA_TINT)
            if C > 0.15:
                base = tuple(min(255, int(v * (1 + 0.35 * L))) for v in GREEN)
            elif C < -0.15:
                base = tuple(min(255, int(v * (1 + 0.35 * L))) for v in RED)
            img.rectangle([x0 + 12 + c * cell_w, y0 + 12 + r * cell_h,
                           x0 + 12 + (c + 1) * cell_w - 1, y0 + 12 + (r + 1) * cell_h - 1],
                          fill=base)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC_USDT")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).resolve().parent.parent / "charts" / "retina-compare.png"))
    a = ap.parse_args()

    candles = fetch_candles(a.pair, 240)
    lum, col = render_chart(candles, CHART_W, CHART_H, WINDOW)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_head, f_sub, f_cap = font(44), font(20), font(17)

    d.text((40, 28), "WHAT YOU SEE", font=f_head, fill=FG)
    d.text((W // 2 + 40, 28), "WHAT THE FLY SEES", font=f_head, fill=RETINA_TINT)
    d.text((40, 86), f"{a.pair} · 1m · last {WINDOW} candles", font=f_sub, fill=FG)
    d.text((W // 2 + 40, 86), f"{CHART_W}×{CHART_H} pixels · 6× supersampled · optical blur",
           font=f_sub, fill=FG)

    gap = 24
    pw = (W - 40 * 2 - gap) // 2
    draw_real_panel(d, (40, 128, 40 + pw, H - 96), candles)
    draw_retina_panel(d, (W // 2 + gap, 128, W - 40, H - 96), lum, col)

    d.text((40, H - 66),
           "No price. No balance. No order book. A connectome decides.",
           font=f_cap, fill=FG)
    d.text((W - 40 - d.textlength("flypnl", font=f_cap), H - 66),
           "flypnl", font=f_cap, fill=RETINA_TINT)

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"[retina] wrote {out}")


if __name__ == "__main__":
    main()
