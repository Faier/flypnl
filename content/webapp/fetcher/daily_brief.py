#!/usr/bin/env python3
"""素材日报生成器 — 每天早上跑一次

纯本地读公开数据，不登录、不发布、不碰任何平台账号。
数据源：
  1. 币圈大事   CoinDesk / Cointelegraph / The Block / Decrypt RSS
  2. 打金狗素材 GeckoTerminal 新池+热池 (solana/bsc) + memescout 真实战绩
  3. 热门话题   新闻标题关键词频次

用法: python3 daily_brief.py
输出: ../daily/YYYY-MM-DD.md
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "daily")
MEMESCOUT = os.environ.get("MEMESCOUT_HOME", "/Users/mac/IdeaProjects/memescout")
TODAY = datetime.now().strftime("%Y-%m-%d")

UA = {"User-Agent": "Mozilla/5.0 (crypto-posts fetcher)"}


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- 币圈大事 (RSS)

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "https://decrypt.co/feed"),
]

HOT_KEYWORDS = {
    "etf": 3, "liquidat": 3, "crash": 3, "hack": 3, "exploit": 3, "fed": 3,
    "sec": 2, "rate": 2, "surge": 2, "plunge": 2, "rally": 2, "soar": 2,
    "bitcoin": 2, "btc": 2, "ethereum": 2, "eth": 1, "solana": 2, "sol": 1,
    "binance": 2, "meme": 2, "memecoin": 3, "whale": 2, "billion": 2,
    "million": 1, "record": 2, "unlock": 2, "listing": 1, "stablecoin": 2,
    "altcoin": 1, "xrp": 2, "zcash": 2, "pump": 1,
}


def parse_rss(xml_bytes, source):
    """兼容 RSS2.0 (channel/item) 与 Atom (feed/entry)，取标题/链接/时间。"""
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = pub = None
        for child in item:
            c = child.tag.rsplit("}", 1)[-1]
            if c == "title" and child.text:
                title = child.text.strip()
            elif c == "link":
                link = (child.get("href") if child.get("href") else child.text or "").strip()
            elif c == "pubDate" and child.text:
                pub = child.text
            elif c == "published" and child.text:
                pub = child.text
            elif c == "updated" and child.text and not pub:
                pub = child.text
        if title:
            items.append({"source": source, "title": title, "link": link, "pub": pub})
    return items


def fetch_news():
    all_items = []
    for name, url in FEEDS:
        try:
            all_items.extend(parse_rss(http_get(url), name))
        except Exception as e:
            print(f"  [warn] {name} 抓取失败: {e}", file=sys.stderr)
    now = datetime.now(timezone.utc)
    for it in all_items:
        score = 0
        low = it["title"].lower()
        for kw, w in HOT_KEYWORDS.items():
            if kw in low:
                score += w
        age_h = 24.0
        if it["pub"]:
            try:
                dt = parsedate_to_datetime(it["pub"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = max(0.0, (now - dt).total_seconds() / 3600)
                it["age"] = f"{age_h:.0f}h前" if age_h >= 1 else f"{max(1,int(age_h*60))}m前"
            except Exception:
                it["age"] = ""
        else:
            it["age"] = ""
        score += max(0.0, 6 - age_h / 4)  # 越新分越高
        it["score"] = score
    seen, unique = set(), []
    for it in sorted(all_items, key=lambda x: -x["score"]):
        key = it["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique[:12]


# ---------------------------------------------------------------- 打金狗素材 (GeckoTerminal)
# 同 IP 常有别的程序（memescout bot）在用配额，这里间隔拉满 + 429 退避

NET_LABELS = {"solana": "Solana", "bsc": "BNB链", "eth": "ETH", "base": "Base",
              "arc": "ARC", "monad": "Monad", "sol": "Solana", "robinhood": "Robinhood"}


GT_CACHE = os.path.join(HERE, ".gt_cache.json")


def gt_get(url, retries=3, backoff=45):
    for i in range(retries + 1):
        try:
            return json.loads(http_get(url))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries:
                wait = backoff
                try:
                    wait = int(e.headers.get("Retry-After") or backoff)
                except Exception:
                    pass
                print(f"  [warn] 429 限流，{wait}s 后重试 ({i + 1}/{retries})", file=sys.stderr)
                time.sleep(min(wait, 90))
            else:
                raise


def fmt_pool(p, net=None):
    if net is None:
        net = p.get("id", "_").split("_", 1)[0]
    net = NET_LABELS.get(net, net)
    a = p.get("attributes", {})
    name = (a.get("name") or "?").rsplit(" ", 1)[0]  # 去掉费率后缀 "1%"
    name = re.sub(r"\s*/\s*$", "", name).strip()  # 去掉孤立斜杠
    chg = (a.get("price_change_percentage") or {})
    h24 = chg.get("h24")
    h1 = chg.get("h1")
    vol = float(a.get("volume_usd", {}).get("h24") or 0)
    fdv = float(a.get("fdv_usd") or 0)
    reserve = float(a.get("reserve_in_usd") or 0)
    tx = (a.get("transactions") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    created = a.get("pool_created_at", "")
    age = ""
    if created:
        try:
            ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
            hours = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
            age = f"{hours:.0f}h" if hours >= 1 else f"{max(1,int(hours*60))}m"
        except Exception:
            pass
    def pct(v):
        return f"{float(v):+.1f}%" if v not in (None, "") else "-"
    return {
        "name": name, "h1": pct(h1), "h24": pct(h24),
        "vol": f"${vol/1000:,.0f}k" if vol < 1e6 else f"${vol/1e6:.1f}M",
        "fdv": f"${fdv/1000:,.0f}k" if fdv < 1e6 else (f"${fdv/1e6:.1f}M" if fdv < 1e9 else f"${fdv/1e9:.2f}B"),
        "liq": f"${reserve/1000:,.0f}k" if reserve < 1e6 else f"${reserve/1e6:.1f}M",
        "tx": f"{buys}/{sells}", "age": age,
        "hot": buys + sells, "vol_raw": vol, "net": net,
    }


def fetch_meme():
    """热池用跨网络接口(1次调用)，新池按链各1次；间隔拉开避免和 memescout bot 抢配额。"""
    rows = {"new": [], "trend": []}
    base = "https://api.geckoterminal.com/api/v2"
    calls = [
        ("new", f"{base}/networks/solana/new_pools?page=1"),
        ("new", f"{base}/networks/bsc/new_pools?page=1"),
        ("trend", f"{base}/networks/trending_pools?page=1"),
    ]
    for i, (kind, url) in enumerate(calls):
        if i:
            time.sleep(8)
        try:
            data = gt_get(url)
            for p in (data.get("data") or [])[:10]:
                rows[kind].append(fmt_pool(p))
        except Exception as e:
            print(f"  [warn] 抓取失败 {url.rsplit('/', 2)[-1]}: {e}", file=sys.stderr)
    # 新池只留已有成交的（零成交的 1 分钟池没有内容价值），按 24h 量排
    rows["new"] = [r for r in rows["new"] if r["vol_raw"] >= 5000]
    rows["new"].sort(key=lambda r: -r["vol_raw"])
    rows["trend"].sort(key=lambda r: -r["hot"])
    if rows["new"] or rows["trend"]:
        try:
            json.dump({"fetched_at": time.time(), "rows": rows}, open(GT_CACHE, "w"))
        except Exception:
            pass
    else:
        # 全被限流时回退上次成功结果，绝不用空数据覆盖日报
        if os.path.exists(GT_CACHE):
            try:
                cache = json.load(open(GT_CACHE))
                age_min = (time.time() - cache["fetched_at"]) / 60
                rows = cache["rows"]
                print(f"  [warn] 链上配额受限，回退缓存（{age_min:.0f} 分钟前）", file=sys.stderr)
            except Exception:
                pass
    return rows


# ---------------------------------------------------------------- memescout 真实战绩

def fetch_memescout():
    out = []
    for chain in ("arc", "bsc", "monad", "sol"):
        st_f = os.path.join(MEMESCOUT, chain, "state.json")
        if not os.path.exists(st_f):
            continue
        try:
            s = json.load(open(st_f))
        except Exception:
            continue
        eq = (s.get("equity") or [{}])[-1].get("eq", 0)
        day = s.get("day") or {}
        open_n = len(s.get("positions") or {})
        pnl, wins, losses = 0.0, 0, 0
        log_f = os.path.join(MEMESCOUT, chain, "trades.log")
        if os.path.exists(log_f):
            for m in re.finditer(r"本仓净([+-][0-9.]+)U", open(log_f, errors="ignore").read()):
                v = float(m.group(1))
                pnl += v
                wins += v > 0
                losses += v <= 0
        out.append({"chain": chain, "eq": eq, "buys": day.get("buys", 0),
                    "realized": day.get("realized", 0), "open": open_n,
                    "cum": pnl, "w": wins, "l": losses})
    return out


# ---------------------------------------------------------------- 热门话题

TOPIC_WATCH = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "etf", "fed",
               "sec", "xrp", "meme", "memecoin", "stablecoin", "binance", "whale",
               "zcash", "zec", "altcoin", "defi", "ai", "unlock", "liquidation", "hack"]


def top_topics(news):
    counter = {}
    for it in news:
        low = it["title"].lower()
        for t in TOPIC_WATCH:
            if t in low:
                counter[t] = counter.get(t, 0) + 1
    return sorted(counter.items(), key=lambda kv: -kv[1])[:8]


# ---------------------------------------------------------------- 输出

def main():
    print(f"素材日报生成中 {TODAY} ...")
    news = fetch_news()
    print(f"  新闻: {len(news)} 条")
    meme = fetch_meme()
    print(f"  链上: 新池 {len(meme['new'])} / 热池 {len(meme['trend'])}")
    scout = fetch_memescout()
    print(f"  memescout: {len(scout)} 条链数据")
    topics = top_topics(news)

    L = []
    L.append(f"# 素材日报 {TODAY}\n")
    L.append("> 用法：从下面挑 2-3 条素材 → 丢给 AI 按 templates.md 出稿 → 对照原文核对数字 → 手动发布。\n")

    L.append("## 一、打金狗素材（独家，优先用）\n")
    if scout:
        L.append("**memescout 今日真实战绩**（实盘监控流素材，直接引用数字）：\n")
        L.append("| 链 | 权益U | 今日买 | 今日已实现U | 在持 | 累计平仓U | 胜/负 |")
        L.append("|---|---|---|---|---|---|---|")
        for c in scout:
            L.append(f"| {c['chain']} | {c['eq']:.2f} | {c['buys']} | {c['realized']:+.2f} | "
                     f"{c['open']} | {c['cum']:+.2f} | {c['w']}/{c['l']} |")
        L.append("")
    L.append("**链上新池雷达**（ GeckoTerminal，按24h交易热度排）：\n")
    L.append("| 链 | 池 | 1h | 24h | 24h量 | FDV | 流动性 | 买/卖 | 池龄 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in meme["new"][:10]:
        L.append(f"| {r['net']} | {r['name']} | {r['h1']} | {r['h24']} | {r['vol']} | "
                 f"{r['fdv']} | {r['liq']} | {r['tx']} | {r['age']} |")
    L.append("\n**当前热池**（已经有热度的，适合写复盘/追涨风险角度）：\n")
    L.append("| 链 | 池 | 1h | 24h | 24h量 | FDV | 买/卖 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in meme["trend"][:8]:
        L.append(f"| {r['net']} | {r['name']} | {r['h1']} | {r['h24']} | {r['vol']} | {r['fdv']} | {r['tx']} |")
    L.append("")

    L.append("## 二、币圈大事（热度分排序）\n")
    for it in news:
        title = it["title"]
        link = it["link"] or ""
        L.append(f"- **{title}**  _{it['source']} {it['age']}_")
        if link:
            L.append(f"  {link}")
    L.append("")

    L.append("## 三、热门话题（新闻标题词频，可当话题标签蹭）\n")
    if topics:
        L.append(" · ".join(f"`{t}`×{n}" for t, n in topics))
        L.append("")
    L.append("---")
    L.append("*数据截至生成时刻。发布前按规矩核对：LLM 只改写以上事实，不补它不知道的数字。*")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.abspath(os.path.join(OUT_DIR, f"{TODAY}.md"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"已生成: {out_path}")

    try:
        import render_site
        render_site.build()
    except Exception as e:
        print(f"  [warn] 页面生成失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
