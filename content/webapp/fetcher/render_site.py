#!/usr/bin/env python3
"""日报静态页 — 把 daily/*.md 渲染成 site/ 下的静态 HTML

用法: python3 render_site.py（daily_brief.py 跑完会自动调一次）
产物: ../site/index.html（目录）+ 每天一页 YYYY-MM-DD.html
纯标准库，生成的页面零依赖，双击离线就能看。
"""
import html
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY_DIR = os.path.join(HERE, "..", "daily")
SITE_DIR = os.path.join(HERE, "..", "site")

CSS = """
*{box-sizing:border-box}
body{margin:0 auto;max-width:920px;padding:24px 16px 60px;
     font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
     color:#1e1e1e;background:#fff}
a{color:#0b5bb5;text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:20px;margin:0 0 6px;padding-bottom:10px;border-bottom:2px solid #1e1e1e}
h2{font-size:16px;margin:34px 0 10px;padding-left:9px;border-left:4px solid #1e1e1e;line-height:1.4}
table{border-collapse:collapse;width:100%;margin:8px 0 4px;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border:1px solid #d6d6d6;padding:4px 9px;text-align:left}
th{background:#f4f4f2;white-space:nowrap}
td.num{text-align:right;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
p{margin:8px 0}
blockquote{margin:4px 0 16px;padding:0 12px;border-left:3px solid #dcdcdc;color:#7a7a7a;font-size:13px}
blockquote p{margin:4px 0}
ul{margin:8px 0;padding-left:22px}
li{margin:8px 0}
.meta{color:#8a8a8a;font-size:12px}
code{background:#f4f4f2;border-radius:3px;padding:1px 5px;font:12px ui-monospace,Menlo,Consolas,monospace}
hr{border:0;border-top:1px solid #e2e2e2;margin:26px 0 8px}
.tw{overflow-x:auto}
.daynav{font-size:13px;color:#999;margin-bottom:14px}
.daynav .dim{color:#c0c0c0}
.daylist{list-style:none;padding:0;margin:6px 0}
.daylist li{margin:6px 0}
.stats{color:#8a8a8a;font-size:12px;margin-left:12px}
.foot{margin-top:44px;padding-top:10px;border-top:1px solid #e2e2e2;color:#9a9a9a;font-size:12px}
"""


def esc(s):
    return html.escape(s, quote=False)


def inline(s):
    s = esc(s)
    # [文字](链接) 先收进占位符，防止后面的裸链接 autolink 把 href 再包一层
    links = []

    def _stash(m):
        links.append(f'<a href="{m.group(2)}">{m.group(1)}</a>')
        return f"\x00{len(links) - 1}\x00"

    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", _stash, s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    s = re.sub(r"(https?://[^\s<>]+)", r'<a href="\1">\1</a>', s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)
    return s


def num_cell(s):
    """数字样单元格右对齐（$16k / -28.8% / 13/5 / 8m …），池名等文本不受影响。"""
    return bool(re.fullmatch(r"[+\-$\d.,%kKMBm/ ]+", s)) and any(c.isdigit() for c in s)


# ---------------------------------------------------------------- 极简 markdown 解析

def parse_blocks(text):
    """只认日报里出现的结构：标题/引用/表格/列表/段落/分隔线。"""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c or "---") for c in r)]
            blocks.append(("table", rows))
            continue
        if s.startswith("###"):
            blocks.append(("h3", s.lstrip("#").strip()))
            i += 1
            continue
        if s.startswith("##"):
            blocks.append(("h2", s.lstrip("#").strip()))
            i += 1
            continue
        if s.startswith("#"):
            blocks.append(("h1", s.lstrip("#").strip()))
            i += 1
            continue
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(buf)))
            continue
        if s == "---":
            blocks.append(("hr", None))
            i += 1
            continue
        if s.startswith("- "):
            items = []
            while i < len(lines):
                t = lines[i].strip()
                if t.startswith("- "):
                    items.append([t[2:], []])
                    i += 1
                elif t and items and lines[i][:1] in (" ", "\t"):
                    items[-1][1].append(t)
                    i += 1
                else:
                    break
            blocks.append(("list", items))
            continue
        buf = [s]
        i += 1
        while i < len(lines):
            t = lines[i].strip()
            if not t or t.startswith(("#", ">", "|", "- ")) or t == "---":
                break
            buf.append(t)
            i += 1
        blocks.append(("para", " ".join(buf)))
    return blocks


# ---------------------------------------------------------------- 渲染

def render_li(first, conts):
    """币圈大事条目：标题加链、来源和时间做小字；其余列表按普通条目处理。"""
    m = re.match(r"\*\*(.+?)\*\*\s*_(.+?)_\s*$", first)
    urls = [c for c in conts if c.startswith("http")]
    if m and urls:
        title, meta = m.groups()
        dom = re.sub(r"^https?://(?:www\.)?([^/]+).*$", r"\1", urls[0])
        return (f'<li><a href="{esc(urls[0])}">{inline(title)}</a>'
                f' <span class="meta">{esc(meta)} · {esc(dom)}</span></li>')
    body = inline(first) + "".join(f'<div class="url">{inline(c)}</div>' for c in conts)
    return f"<li>{body}</li>"


def render_table(rows):
    if not rows:
        return ""
    head = rows[0]
    n = len(head)
    trs = []
    for r in rows[1:]:
        r = (r + [""] * n)[:n]
        tds = "".join(f'<td class="num">{inline(c)}</td>' if num_cell(c) else f"<td>{inline(c)}</td>"
                      for c in r)
        trs.append(f"<tr>{tds}</tr>")
    ths = "".join(f"<th>{inline(c)}</th>" for c in head)
    return f'<table><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def render_blocks(blocks):
    out = []
    for kind, p in blocks:
        if kind == "h1":
            out.append(f"<h1>{inline(p)}</h1>")
        elif kind == "h2":
            out.append(f"<h2>{inline(p)}</h2>")
        elif kind == "h3":
            out.append(f"<h3>{inline(p)}</h3>")
        elif kind == "quote":
            out.append(f"<blockquote><p>{inline(p)}</p></blockquote>")
        elif kind == "para":
            out.append(f"<p>{inline(p)}</p>")
        elif kind == "hr":
            out.append("<hr>")
        elif kind == "table":
            out.append(f'<div class="tw">{render_table(p)}</div>')
        elif kind == "list":
            out.append("<ul>" + "".join(render_li(f, c) for f, c in p) + "</ul>")
    return "\n".join(out)


def shell(title, body):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def render_day(date, blocks, newer, older):
    nav = []
    if newer:
        nav.append(f'<a href="{newer}.html">← {newer}</a>')
    else:
        nav.append('<span class="dim">已是最新</span>')
    if older:
        nav.append(f'<a href="{older}.html">{older} →</a>')
    else:
        nav.append('<span class="dim">没有更早的了</span>')
    body = f'<div class="daynav">{" ｜ ".join(nav)}</div>\n' + render_blocks(blocks)
    body += (f'\n<div class="foot">生成于 {datetime.now():%Y-%m-%d %H:%M} · '
             f'<a href="index.html">全部日报</a></div>')
    return shell(f"素材日报 {date}", body)


def day_stats(blocks):
    """从一天的块里数出：大事条数 / 新池数 / 热池数，给目录页用。"""
    news = new_pools = hot_pools = 0
    section = last_para = ""
    for kind, p in blocks:
        if kind == "h2":
            section = p
        elif kind == "para":
            last_para = p
        elif kind == "list" and "币圈大事" in section:
            news = len(p)
        elif kind == "table":
            if "新池雷达" in last_para:
                new_pools = len(p) - 1
            elif "当前热池" in last_para:
                hot_pools = len(p) - 1
    return news, new_pools, hot_pools


def render_index(days):
    total = len(days)
    parts = [f"<h1>素材日报</h1>",
             f"<blockquote><p>每天跑 fetcher/daily_brief.py 自动生成，共 {total} 天。"
             f"挑 2-3 条素材按 templates.md 出稿。</p></blockquote>"]
    cur = None
    for date, blocks in days:
        ym = date[:7]
        if ym != cur:
            if cur is not None:
                parts.append("</ul>")
            parts.append(f"<h2>{ym}</h2>")
            parts.append('<ul class="daylist">')
            cur = ym
        n, np_, hp = day_stats(blocks)
        parts.append(f'<li><a href="{date}.html">{date}</a>'
                     f'<span class="stats">大事 {n} · 新池 {np_} · 热池 {hp}</span></li>')
    if cur is not None:
        parts.append("</ul>")
    parts.append('<div class="foot">数据：CoinDesk / Cointelegraph / The Block / Decrypt RSS · '
                 'GeckoTerminal · memescout 实盘</div>')
    return shell("素材日报", "\n".join(parts))


def build():
    if not os.path.isdir(DAILY_DIR):
        print(f"[warn] 找不到 {DAILY_DIR}，先跑 daily_brief.py", file=os.sys.stderr)
        return 0
    days = []
    for f in sorted(os.listdir(DAILY_DIR)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f):
            continue
        with open(os.path.join(DAILY_DIR, f), encoding="utf-8") as fh:
            days.append((f[:-3], parse_blocks(fh.read())))
    days.sort(key=lambda d: d[0], reverse=True)

    os.makedirs(SITE_DIR, exist_ok=True)
    for i, (date, blocks) in enumerate(days):
        newer = days[i - 1][0] if i > 0 else None
        older = days[i + 1][0] if i + 1 < len(days) else None
        with open(os.path.join(SITE_DIR, f"{date}.html"), "w", encoding="utf-8") as fh:
            fh.write(render_day(date, blocks, newer, older))
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_index(days))
    print(f"页面已生成 {len(days)} 天 → {os.path.abspath(os.path.join(SITE_DIR, 'index.html'))}")
    return len(days)


if __name__ == "__main__":
    build()
