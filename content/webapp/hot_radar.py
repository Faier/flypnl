#!/usr/bin/env python3
"""全网热搜与热点风向标雷达
聚合百度、微博、CoinGecko热币、Google Trends、Hacker News热帖
提供跨榜去重、热度归一化打分与缓存回退
"""
import re
import json
import time
import urllib.request
from sources_core import parse_items, _http_raw, classify, _mk

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

_HOT_CACHE = {}


def _norm_topic(t):
    return re.sub(r"[\s#•·!！?？,，。.：:;；\"'（）()\[\]【】]+", "", t or "").lower()


from concurrent.futures import ThreadPoolExecutor, as_completed

HOT_TIMEOUT = 3.5

def hot_baidu():
    raw = _http_raw("https://top.baidu.com/api/board?platform=wise&tab=realtime", HOT_TIMEOUT, BROWSER_HEADERS)
    data = json.loads(raw.decode("utf-8"))
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "word" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    items = []
    for x in found[:50]:
        w = (x.get("word") or "").strip()
        if not w:
            continue
        it = _mk(w, "百度热搜", x.get("url") or f"https://www.baidu.com/s?wd={w}", None, "")
        it["board_score"] = float(x.get("hotScore") or 0)
        items.append(it)
    return items


def hot_weibo():
    h = dict(BROWSER_HEADERS)
    h["Referer"] = "https://weibo.com/"
    raw = _http_raw("https://weibo.com/ajax/side/hotSearch", HOT_TIMEOUT, h)
    data = json.loads(raw.decode("utf-8"))
    from urllib.parse import quote
    items = []
    for x in (data.get("data") or {}).get("realtime") or []:
        w = (x.get("word") or "").strip()
        if not w:
            continue
        it = _mk(w, "微博热搜", "https://s.weibo.com/weibo?q=" + quote("#" + w + "#"), None, "")
        it["board_score"] = float(x.get("num") or 0)
        items.append(it)
    return items


def hot_cgtrending():
    raw = _http_raw("https://api.coingecko.com/api/v3/search/trending", HOT_TIMEOUT)
    data = json.loads(raw.decode("utf-8"))
    items = []
    for c in data.get("coins") or []:
        item = c.get("item") or {}
        name = f"{item.get('name')} ({item.get('symbol')})"
        it = _mk(name, "CoinGecko热币", f"https://www.coingecko.com/en/coins/{item.get('id')}", None, "")
        it["board_rank"] = item.get("rank") or 50
        it["category"] = "币圈"
        items.append(it)
    return items


def hot_gtrends():
    items = []
    raw = _http_raw("https://trends.google.com/trending/rss?geo=US", HOT_TIMEOUT)
    from urllib.parse import quote
    for d in parse_items(raw):
        it = _mk(d["title"], "Google Trends", d["link"] or f"https://trends.google.com/trends/explore?q={quote(d['title'])}", d["pub"], "")
        it["board_rank"] = len(items) + 1
        items.append(it)
    return items


def hot_hn():
    raw = _http_raw("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30", HOT_TIMEOUT)
    data = json.loads(raw.decode("utf-8"))
    items = []
    for h in data.get("hits") or []:
        title = h.get("title") or ""
        if not title:
            continue
        it = _mk(title, "HN热帖", h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}", None, "")
        it["board_score"] = float(h.get("points") or 0)
        it["category"] = "AI"
        items.append(it)
    return items


BOARDS = [
    ("百度热搜", hot_baidu),
    ("微博热搜", hot_weibo),
    ("CoinGecko热币", hot_cgtrending),
    ("Google Trends", hot_gtrends),
    ("HN热帖", hot_hn)
]


def _board_with_cache(name, fn):
    # 若5分钟内有成功缓存，直接返回秒开
    ts, cached = _HOT_CACHE.get(name, (0, []))
    if cached and (time.time() - ts) < 300:
        return cached, "ok", f"{len(cached)} 条(缓)"

    try:
        items = fn()
        if items:
            _HOT_CACHE[name] = (time.time(), items)
            return items, "ok", f"{len(items)} 条"
    except Exception:
        pass

    # 失败回退到过期缓存
    if cached:
        return cached, "ok", f"缓存 {time.time() - ts:.0f}s 前"
    return [], "fail", "抓取失败"


def get_hot_topics(cat_filter=""):
    """跨榜并发抓取，统一归一化 0-100 热度分"""
    topics, statuses, dropped = {}, [], 0

    results = []
    with ThreadPoolExecutor(max_workers=len(BOARDS)) as ex:
        future_map = {ex.submit(_board_with_cache, name, fn): name for name, fn in BOARDS}
        for f in as_completed(future_map):
            name = future_map[f]
            try:
                items, st, detail = f.result()
                results.append((name, items, st, detail))
            except Exception:
                results.append((name, [], "fail", "错误"))

    for name, items, st, detail in results:
        statuses.append({"name": name, "state": st, "detail": detail})
        if not items:
            continue
        mx = max([abs(i.get("board_score") or 0) for i in items] or [0])
        for it in items:
            c = it.get("category") or classify(it["title"])
            if c == "政治":
                dropped += 1
                continue
            if it.get("board_score"):
                norm = it["board_score"] / mx * 100 if mx else 0
            else:
                norm = max(0.0, 100 - (it.get("board_rank") or 50) * 5)
            key = _norm_topic(it["title"])
            merged = None
            for k in topics:
                if k == key or (min(len(k), len(key)) >= 5 and (k in key or key in k)):
                    merged = k
                    break
            if merged is None:
                topics[key] = {
                    "title": it["title"],
                    "category": c,
                    "link": it.get("link", ""),
                    "boards": {name: round(norm, 1)},
                    "heat": norm
                }
            else:
                t = topics[merged]
                t["boards"][name] = round(max(t["boards"].get(name, 0), norm), 1)
                if norm > t["heat"]:
                    t["title"], t["category"], t["link"], t["heat"] = it["title"], c, it.get("link", ""), norm

    for t in topics.values():
        t["heat"] = round(min(100, max(t["boards"].values()) + 12 * (len(t["boards"]) - 1)), 1)
        t["board_names"] = list(t["boards"].keys())

    rows = sorted(topics.values(), key=lambda t: -t["heat"])
    if cat_filter and cat_filter != "全部":
        rows = [t for t in rows if t["category"] == cat_filter]

    return {
        "topics": rows,
        "statuses": statuses,
        "dropped_politics": dropped
    }
