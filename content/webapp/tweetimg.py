#!/usr/bin/env python3
"""tweetimg.py — 发推配图工具

两个子命令：
  chart  数据图：QuickChart (Chart.js) 渲染，URL 即图片，支持中文
  card   文字卡：carbon 风格金句卡模板，渲染出 HTML 后可用浏览器截图成 PNG

示例：
  python3 tweetimg.py chart --title "毕业潮 vs 退休潮（万人/年）" \
      --labels 2020,2021,2022,2023,2024,2025,2026E \
      --series "高校毕业生:bar:874,909,1076,1158,1179,1222,1270" \
      --series "年均退休:line:null,null,2000,2000,2000,2000,2000" \
      --out assets/graduate-vs-retiree.png

  python3 tweetimg.py card --tag "毕业1270万 vs 退休2000万" \
      --text "岗位是腾出来了，\\n但[[腾出来的，和想要的，不是同一批]]。" \
      --foot "@yourhandle · 数据：教育部 / 半月谈" \
      --out assets/quote-card.html
"""
import argparse
import json
import pathlib
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "assets"

QUICKCHART = "https://quickchart.io/chart"

# carbon 默认配色：青绿 → 靛蓝渐变 + seti 风格窗口；数据大数字为主角
CARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: 1200px; height: 675px; overflow: hidden;
    background: linear-gradient(to bottom right, rgb(12, 125, 125), rgb(24, 33, 123));
    display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  }}
  .window {{
    width: 1040px;
    background: #1d2433; border-radius: 14px;
    box-shadow: 0 30px 60px rgba(0,0,0,.45);
    padding: 26px 34px 42px;
  }}
  .bar {{
    display: flex; align-items: center; gap: 9px;
  }}
  .dot {{ width: 15px; height: 15px; border-radius: 50%; }}
  .dot.r {{ background: #ff5f57; }} .dot.y {{ background: #febc2e; }} .dot.g {{ background: #28c840; }}
  .tag {{
    margin-left: 14px; font: 600 17px/1 "SF Mono", Menlo, monospace;
    color: #7dd3c0; letter-spacing: 1px;
  }}
  .title {{
    margin-top: 28px; font-size: 50px; font-weight: 900; line-height: 1.35;
    color: #ffffff; letter-spacing: 1px;
  }}
  .stats {{
    display: flex; align-items: flex-end; gap: 34px; margin: 32px 0 32px;
  }}
  .num {{ font-size: 88px; font-weight: 800; line-height: 1; letter-spacing: 0; }}
  .num .u {{ font-size: 40px; font-weight: 800; margin-left: 4px; }}
  .label {{ margin-top: 12px; font-size: 20px; color: #8b96a8; }}
  .vs {{
    font: 700 28px/1 "SF Mono", Menlo, monospace; color: #5d6a7d;
    padding-bottom: 42px;
  }}
  .body {{
    font-size: 30px; font-weight: 600; line-height: 1.65; color: #c9d2de;
    letter-spacing: 1px;
  }}
  .body b {{ color: #ffd166; font-weight: 800; }}
  .source {{
    margin-top: 24px; font-size: 16px; color: #6b7688; letter-spacing: .5px;
  }}
</style>
</head>
<body>
  <div class="window">
    <div class="bar">
      <div class="dot r"></div><div class="dot y"></div><div class="dot g"></div>
      <span class="tag">{tag}</span>
    </div>
    {title}
    {stats}
    <div class="body">{body}</div>
    {source}
  </div>
</body>
</html>
"""


def _parse_value(v):
    v = v.strip()
    return None if v.lower() == "null" else (float(v) if "." in v else int(v))


def build_chart_config(args):
    labels = args.labels.split(",")
    datasets = []
    for i, s in enumerate(args.series):
        name, kind, raw = s.split(":", 2)
        data = [_parse_value(x) for x in raw.split(",")]
        if args.horizontal:
            ds = {"label": name, "data": data}
        else:
            ds = {"label": name, "type": kind, "data": data}
        if args.horizontal or kind == "bar":
            if args.highlight:
                ds["backgroundColor"] = [
                    "rgba(255,190,80,.92)" if args.highlight in l
                    else "rgba(99,159,240,.88)" for l in labels]
            else:
                ds["backgroundColor"] = "rgba(99,159,240,.88)"
        else:
            ds.update({
                "borderColor": "rgba(255,190,80,1)", "borderDash": [9, 7],
                "borderWidth": 4, "pointRadius": 0, "fill": False,
                "pointStyle": "line",
            })
        datasets.append(ds)
    title = [args.title] if args.subtitle else [args.title]
    if args.subtitle:
        title.append(args.subtitle)
    if args.horizontal:
        scales = {
            "xAxes": [{"ticks": {"beginAtZero": True, "fontColor": "#b9c2cd"},
                       "gridLines": {"color": "rgba(255,255,255,.08)"}}],
            "yAxes": [{"ticks": {"fontColor": "#d6dce4", "fontSize": 15}}],
        }
        cfg_type = "horizontalBar"
    else:
        scales = {
            "yAxes": [{"ticks": {"fontColor": "#b9c2cd", "beginAtZero": True},
                       "gridLines": {"color": "rgba(255,255,255,.08)"}}],
            "xAxes": [{"ticks": {"fontColor": "#b9c2cd"}}],
        }
        cfg_type = "bar"
    return {
        "type": cfg_type,
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "title": {"display": True, "text": title, "fontSize": 20,
                      "fontColor": "#f2f5f9", "padding": 18},
            "legend": {"display": len(datasets) > 1,
                       "labels": {"fontColor": "#d6dce4", "fontSize": 14,
                                  "usePointStyle": True}},
            "scales": scales,
        },
    }


def cmd_chart(args):
    cfg = build_chart_config(args)
    params = {"c": json.dumps(cfg, ensure_ascii=False), "w": args.w, "h": args.h,
              "b": "rgb(15,18,25)"}
    url = f"{QUICKCHART}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "tweetimg/1.0"})
    data = urllib.request.urlopen(req, timeout=30).read()
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"已生成 {path}  ({len(data)//1024} KB, {args.w}x{args.h})")


def _stat_html(num, label, color):
    unit = '<span class="u">万</span>' if num.endswith("万") else ""
    digits = num[:-1] if num.endswith("万") else num
    return (f'<div class="stat"><div class="num" style="color:{color}">'
            f'{digits}{unit}</div><div class="label">{label}</div></div>')


def cmd_card(args):
    body = (args.text.replace("\\n", "\n")
            .replace("[[", "<b>").replace("]]", "</b>").replace("\n", "<br>"))
    stats = ""
    if args.stat:
        colors = ["#6a9ef0", "#ffbe50", "#7dd3c0", "#f28b82"]
        items = [_stat_html(s.split(":", 1)[0], s.split(":", 1)[1],
                            colors[i % len(colors)])
                 for i, s in enumerate(args.stat)]
        joiner = '<div class="vs">VS</div>' if len(items) == 2 else ""
        stats = f'<div class="stats">{joiner.join(items)}</div>'
    title = f'<div class="title">{args.title}</div>' if args.title else ""
    source = f'<div class="source">{args.source}</div>' if args.source else ""
    html = CARD_TEMPLATE.format(tag=args.tag, title=title, stats=stats,
                                body=body, source=source)
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"已生成 {path}\n浏览器打开后截图即可；或用浏览器自动化按 1200x675 截图。")


def main():
    p = argparse.ArgumentParser(description="发推配图：QuickChart 数据图 / carbon 风格文字卡")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("chart", help="QuickChart 数据图")
    c.add_argument("--title", required=True)
    c.add_argument("--subtitle", default="")
    c.add_argument("--labels", required=True, help="逗号分隔，如 2020,2021,2022")
    c.add_argument("--series", action="append", required=True,
                   help="名称:bar|line:数据(逗号分隔，空位用null)，可多次")
    c.add_argument("--w", type=int, default=1200)
    c.add_argument("--h", type=int, default=675)
    c.add_argument("--horizontal", action="store_true",
                   help="横向条形图（国家/地区对比用）")
    c.add_argument("--highlight", default="",
                   help="高亮包含此文字的条目（如 --highlight 中国，橙色显示）")
    c.add_argument("--out", default="assets/chart.png")

    d = sub.add_parser("card", help="carbon 风格文字卡")
    d.add_argument("--tag", default="SNAPSHOT")
    d.add_argument("--title", default="", help="主标题（观点钩子），全卡最突出")
    d.add_argument("--stat", action="append", default=[],
                   help="大数字:标签，如 '1270万:高校毕业生/年'，可多次（两个自动加VS）")
    d.add_argument("--text", required=True, help="观点收尾，[[...]] 金色高亮，\\n 换行")
    d.add_argument("--source", default="", help="数据来源注脚（左下角小字，体现真实性）")
    d.add_argument("--out", default="assets/card.html")

    args = p.parse_args()
    {"chart": cmd_chart, "card": cmd_card}[args.cmd](args)


if __name__ == "__main__":
    main()
