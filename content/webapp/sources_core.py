#!/usr/bin/env python3
"""数据源核心管理与通用抓取适配器
包含数据源 CRUD、连通性测试、智能多协议抓取、多源聚合资讯流、关键词检索与数据特征提取
"""
import os
import re
import json
import math
import time
import gzip
import hashlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import urllib.request
import urllib.error
from urllib.parse import quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(HERE, "sources.json")
CACHE_FILE = os.path.join(HERE, ".feed_cache.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (crypto-posts digest)"
REDDIT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
NEWS_TIMEOUT = 4.5

POLITICS = [
    "trump", "biden", "harris", "putin", "zelensky", "election", "president",
    "senate", "congress", "ukraine", "russia", "kyiv", "hamas", "netanyahu", "taiwan",
    "nato", "sanction", "white house",
    "白宫", "特朗普", "川普", "拜登", "选举", "总统", "国会", "参议院", "众议院",
    "俄乌", "加沙", "以色列", "gaza", "iran", "台湾", "台北", "两岸", "统一",
    "国台办", "新质生产力", "国务院", "外交部", "国防部", "习近平", "普京",
    "泽连斯基", "乌克兰", "俄罗斯", "北约", "海牙", "国际刑事法院", "哈马斯",
    "军演", "导弹", "停火", "制裁", "战争", "军事", "以军", "美军", "核武器", "大选"
]

CAT_RULES = [
    ("币圈", ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "binance", "币安", "币圈",
             "加密", "比特币", "以太坊", "区块链", "稳定币", "token", "代币", "coin", "etf",
             "链上", "爆仓", "defi", "nft", "web3", "meme", "memecoin", "狗狗", "挖矿",
             "dex", "uniswap", "airdrop", "空投", "rug", "fdv", "tvl", "coindesk"]),
    ("AI", ["openai", "anthropic", "chatgpt", "claude", "gemini", "llm", "gpu", "大模型",
            "人工智能", "nvidia", "英伟达", "智能体", "sam altman", "deepseek", "manus",
            "生成式", "机器学习", "算力", "agent", "transformer"]),
    ("宏观", ["美联储", "fed", "cpi", "利率", "加息", "降息", "非农", "纳斯达克", "标普",
            "a股", "上证", "深证", "恒生", "港股", "人民币", "美元指数", "国债", "央行",
            "通胀", "财报", "华尔街"]),
    ("热点", []),
]

WORD_BOUNDARY = {"ai", "btc", "eth", "etf", "coin", "gpu", "llm", "nft", "defi", "web3", "meme", "dex", "sol", "fed", "cpi"}

SYNONYMS = {
    "比特币": ["bitcoin", "btc"], "以太坊": ["ethereum", "eth"], "索拉纳": ["solana", "sol"],
    "稳定币": ["stablecoin", "usdt", "usdc"], "人工智能": ["ai", "artificial intelligence"],
    "大模型": ["llm", "large language model"], "英伟达": ["nvidia", "nvda"], "美联储": ["fed", "federal reserve"],
    "降息": ["rate cut", "interest rate cut"], "加息": ["rate hike"], "黄金": ["gold"],
    "美股": ["us stocks", "nasdaq", "s&p 500"], "a股": ["china stocks", "csec"], "股市": ["stocks"],
    "人民币": ["yuan", "rmb"], "美元": ["dollar", "usd"], "加密": ["crypto"],
    "加密货币": ["crypto", "cryptocurrency"], "狗狗币": ["dogecoin", "doge"], "狗狗": ["dogecoin"],
    "爆仓": ["liquidation"], "挖矿": ["mining"], "上币": ["listing"], "现货etf": ["spot etf"],
}

DEFAULT_SOURCES = {
    "sources": [
        {"id": "coindesk", "name": "CoinDesk", "type": "rss", "category": "币圈", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "enabled": True},
        {"id": "cointelegraph", "name": "Cointelegraph", "type": "rss", "category": "币圈", "url": "https://cointelegraph.com/rss", "enabled": True},
        {"id": "theblock", "name": "The Block", "type": "rss", "category": "币圈", "url": "https://www.theblock.co/rss.xml", "enabled": True},
        {"id": "decrypt", "name": "Decrypt", "type": "rss", "category": "币圈", "url": "https://decrypt.co/feed", "enabled": True},
        {"id": "panews", "name": "PANews", "type": "rss", "category": "币圈", "url": "https://www.panewslab.com/rss", "enabled": True},
        {"id": "odaily", "name": "Odaily星球日报", "type": "rss", "category": "币圈", "url": "https://rss.odaily.news/", "enabled": True},
        {"id": "techflow", "name": "深潮 TechFlow", "type": "rss", "category": "币圈", "url": "https://www.techflowpost.com/rss", "enabled": True},
        {"id": "google_news", "name": "Google 新闻", "type": "google_news", "category": "热点", "url": "", "enabled": True},
        {"id": "reddit", "name": "Reddit 社区", "type": "reddit", "category": "热点", "url": "", "subs": ["CryptoCurrency", "wallstreetbets", "stocks"], "enabled": True},
        {"id": "hn", "name": "Hacker News", "type": "hn", "category": "AI", "url": "", "enabled": True},
        {"id": "eastmoney", "name": "东方财富 7x24", "type": "eastmoney", "category": "宏观", "url": "", "enabled": True},
        {"id": "sina_fast", "name": "新浪 7x24", "type": "sina_fast", "category": "宏观", "url": "", "enabled": True},
        {"id": "bbc_world", "name": "BBC 国际新闻", "type": "rss", "category": "国际", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "enabled": True},
        {"id": "x_mirror", "name": "X（谷歌镜像）", "type": "x_mirror", "category": "热点", "url": "", "enabled": True},
        {"id": "binance_ann", "name": "Binance 公告", "type": "binance_ann", "category": "币圈", "url": "", "enabled": False, "note": "上新与下架事件"},
        {"id": "bybit_ann", "name": "Bybit 公告", "type": "bybit_ann", "category": "币圈", "url": "", "enabled": False, "note": "交易所官方公告"},
        {"id": "x_api", "name": "X（官方API·付费）", "type": "x", "category": "热点", "url": "", "enabled": False, "note": "需环境变量 X_BEARER_TOKEN，按量计费"},
        {"id": "gate_square", "name": "Gate 广场", "type": "none", "category": "币圈", "url": "", "enabled": False, "note": "Gate 无公开内容 API，手动发"}
    ]
}


def _gen_id(name):
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")
    return f"{slug}_{h}" if slug else f"src_{h}"


def load_sources():
    """读取 sources.json，并自动补全 id 与 category 结构"""
    data = None
    if os.path.exists(SOURCES_FILE):
        try:
            with open(SOURCES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    if not data or "sources" not in data or not isinstance(data["sources"], list):
        data = json.loads(json.dumps(DEFAULT_SOURCES))

    modified = False
    for i, s in enumerate(data["sources"]):
        if not s.get("id"):
            s["id"] = _gen_id(s.get("name", f"source_{i}"))
            modified = True
        if not s.get("category"):
            # 自动推断分类
            st = s.get("type", "")
            sn = s.get("name", "")
            if any(k in sn for k in ["Coin", "Block", "Decrypt", "Binance", "Bybit", "Gate", "PANews", "Odaily", "深潮"]):
                s["category"] = "币圈"
            elif any(k in sn for k in ["HN", "Hacker", "AI"]):
                s["category"] = "AI"
            elif any(k in sn for k in ["东方财富", "新浪", "A股", "财经", "金十"]):
                s["category"] = "宏观"
            else:
                s["category"] = "热点"
            modified = True

    if modified:
        save_sources(data)

    return data


def save_sources(cfg):
    """保存数据源配置到 sources.json"""
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def get_source_by_id(source_id):
    cfg = load_sources()
    for s in cfg.get("sources", []):
        if s.get("id") == source_id:
            return s
    return None


def save_source_item(item):
    """新增或修改单条数据源"""
    cfg = load_sources()
    sources = cfg.get("sources", [])
    src_id = item.get("id")
    if not src_id:
        src_id = _gen_id(item.get("name", "source"))
        item["id"] = src_id

    # 查重或更新
    found = False
    for i, s in enumerate(sources):
        if s.get("id") == src_id:
            sources[i].update(item)
            found = True
            break
    if not found:
        sources.append(item)

    cfg["sources"] = sources
    save_sources(cfg)
    return item


def toggle_source(source_id, enabled=None):
    """切换启用状态"""
    cfg = load_sources()
    for s in cfg.get("sources", []):
        if s.get("id") == source_id:
            if enabled is None:
                s["enabled"] = not s.get("enabled", True)
            else:
                s["enabled"] = bool(enabled)
            save_sources(cfg)
            return True, s["enabled"]
    return False, False


def delete_source(source_id):
    """删除指定数据源"""
    cfg = load_sources()
    before = len(cfg.get("sources", []))
    cfg["sources"] = [s for s in cfg.get("sources", []) if s.get("id") != source_id]
    if len(cfg["sources"]) < before:
        save_sources(cfg)
        return True
    return False


def reset_sources_to_default():
    """重置为默认数据源推荐列表"""
    data = json.loads(json.dumps(DEFAULT_SOURCES))
    save_sources(data)
    return data


# ---------------------------------------------------------------- 网络请求与条目解析

def _http_raw(url, timeout=NEWS_TIMEOUT, headers=None):
    h = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_encoding = resp.headers.get("Content-Encoding", "").lower()
        raw = resp.read()
        if content_encoding == "gzip" or (len(raw) > 2 and raw[:2] == b"\x1f\x8b"):
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return raw


def parse_items(xml_bytes):
    """解析 RSS 2.0 / Atom 统一输出 [{title, link, pub, desc}]"""
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        # 部分 XML 可能有非法控制字符或编码声明，清理后重试
        clean = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", xml_bytes)
        root = ET.fromstring(clean)

    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "pub": None, "desc": ""}
        for ch in it:
            c = ch.tag.rsplit("}", 1)[-1]
            if c == "title" and ch.text:
                d["title"] = ch.text.strip()
            elif c == "link":
                d["link"] = (ch.get("href") if ch.get("href") else ch.text or "").strip()
            elif c in ("pubDate", "published", "updated") and ch.text and not d["pub"]:
                d["pub"] = ch.text
            elif c in ("description", "summary", "content") and ch.text and not d["desc"]:
                d["desc"] = re.sub(r"<[^>]+>", " ", ch.text).strip()[:240]
        if d["title"]:
            out.append(d)
    return out


def _mk(title, src, link, pub=None, desc="", category=None, source_id=""):
    return {
        "title": title.strip(),
        "src": src,
        "source_id": source_id,
        "link": link,
        "pub": pub,
        "age": _age(pub),
        "desc": desc.strip() if desc else "",
        "category": category or classify(title)
    }


def _age(pub):
    if not pub:
        return None
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return None


def _fmt_age(age):
    if age is None:
        return ""
    if age < 1:
        m = int(age * 60)
        return f"{max(1, m)}m前"
    if age < 24:
        return f"{int(age)}h前"
    d = int(age / 24)
    return f"{d}天前"


def _terms(query):
    out = []
    for t in [x.lower() for x in (query or "").split() if x]:
        out.append(t)
        out += SYNONYMS.get(t, [])
    return list(dict.fromkeys(out))


def expand_query(query):
    return " OR ".join(_terms(query))


def classify(text):
    low = " " + text.lower() + " "
    if any(p in low for p in POLITICS):
        return "政治"
    for cat, kws in CAT_RULES:
        for kw in kws:
            if kw in WORD_BOUNDARY:
                if re.search(r"\b" + re.escape(kw) + r"\b", low):
                    return cat
            elif kw in low:
                return cat
    return "热点"


def extract_numbers(items):
    out, seen = [], set()
    pat = re.compile(r"\$\s?\d[\d,.]*\s*(?:billion|million|trillion|[bmtk]\b)?|\d[\d,.]*\s*[亿万]|[\d,.]+\s*%", re.I)
    for it in items:
        text = it.get("title", "") + " " + it.get("desc", "")
        for m in pat.finditer(text):
            num = m.group(0).strip()
            k = re.sub(r"\s+", "", num.lower())
            if not re.search(r"\d", num) or k in seen:
                continue
            seen.add(k)
            ctx = it["title"][:50] + ("…" if len(it["title"]) > 50 else "")
            out.append({"num": num, "ctx": ctx, "src": it.get("src", "")})
    return out[:12]


def matches_terms(text, terms):
    """精确匹配关键词：短词（如 eth、sol、ai）强制使用单词边界，避免误匹配"""
    if not terms:
        return True
    low = " " + text.lower() + " "
    for t in terms:
        if t in WORD_BOUNDARY or len(t) <= 3:
            if re.search(r"\b" + re.escape(t) + r"\b", low):
                return True
        else:
            if t in low:
                return True
    return False


# ---------------------------------------------------------------- 各类型数据源适配器

def ad_rss(src, query=""):
    url = src.get("url", "")
    if not url:
        return []
    terms = _terms(query) if query else []
    raw = _http_raw(url, NEWS_TIMEOUT)
    items = parse_items(raw)
    out = []
    for d in items:
        text = d["title"] + " " + d["desc"]
        if matches_terms(text, terms):
            out.append(_mk(d["title"], src["name"], d["link"], d["pub"], d["desc"], src.get("category"), src.get("id")))
    return out


def ad_json(src, query=""):
    """通用 JSON API 适配器：智能遍历列表，提取标题、链接、时间与简介"""
    url = src.get("url", "")
    if not url:
        return []
    terms = _terms(query) if query else []
    raw = _http_raw(url, NEWS_TIMEOUT)
    data = json.loads(raw.decode("utf-8", errors="replace"))

    # 寻找列表数据
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for k in ["data", "list", "items", "articles", "news", "fastNewsList", "feed", "posts"]:
            if k in data and isinstance(data[k], list):
                items = data[k]
                break
        if not items:
            for v in data.values():
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    items = v
                    break

    out = []
    for row in items:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("headline") or row.get("name") or row.get("summary") or row.get("text") or ""
        if not title:
            continue
        link = row.get("url") or row.get("link") or row.get("href") or ""
        desc = row.get("desc") or row.get("description") or row.get("summary") or row.get("content") or ""
        pub = row.get("pubDate") or row.get("publishDate") or row.get("created_at") or row.get("time") or row.get("date")

        text = str(title) + " " + str(desc)
        if matches_terms(text, terms):
            out.append(_mk(str(title)[:160], src["name"], str(link), str(pub) if pub else None, str(desc)[:200], src.get("category"), src.get("id")))
    return out


def ad_google(src, query=""):
    q = query if query else "加密货币 OR 比特币 OR AI 人工智能"
    # 快速单向探测：若含中文用中文源，纯英文用英文源，避免双倍串行延迟
    has_cn = bool(re.search(r"[\u4e00-\u9fa5]", q))
    lang = "hl=zh-CN&gl=CN&ceid=CN:zh-Hans" if has_cn else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(q)}&{lang}"
    out = []
    try:
        for d in parse_items(_http_raw(url, NEWS_TIMEOUT)):
            title, s = d["title"], src["name"]
            if " - " in title:
                title, s2 = title.rsplit(" - ", 1)
                s = f"{s}/{s2}"
            out.append(_mk(title, s, d["link"], d["pub"], "", src.get("category"), src.get("id")))
    except Exception:
        pass
    return out


def ad_reddit(src, query=""):
    ascii_terms = [t for t in _terms(query) if t.isascii()] if query else []
    q = ascii_terms[0] if ascii_terms else (query or "crypto")
    out = []
    subs = src.get("subs") or ["CryptoCurrency"]
    # 仅请求主要 sub，避免串行等待多个 subreddit 超时
    sub = subs[0] if subs else "CryptoCurrency"
    try:
        url = f"https://www.reddit.com/r/{sub}/search.rss?q={quote(q)}&restrict_sr=1&sort=new&limit=25" if query else f"https://www.reddit.com/r/{sub}/new.rss?limit=25"
        for d in parse_items(_http_raw(url, NEWS_TIMEOUT, {"User-Agent": REDDIT_UA})):
            out.append(_mk(d["title"], f"{src['name']} r/{sub}", d["link"], d["pub"], d["desc"], src.get("category"), src.get("id")))
    except Exception:
        pass
    return out


def ad_hn(src, query=""):
    ascii_terms = [t for t in _terms(query) if t.isascii()] if query else []
    q = ascii_terms[0] if ascii_terms else (query or "")
    url = f"https://hn.algolia.com/api/v1/search_by_date?query={quote(q)}&tags=story&hitsPerPage=30" if q else "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
    out = []
    try:
        data = json.loads(_http_raw(url, NEWS_TIMEOUT).decode("utf-8"))
        for h in data.get("hits") or []:
            title = h.get("title") or h.get("story_title") or ""
            if not title:
                continue
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            it = _mk(title, src["name"], link, None, "", src.get("category"), src.get("id"))
            try:
                cdt = datetime.fromisoformat(h.get("created_at").replace("Z", "+00:00"))
                it["age"] = max(0.0, (datetime.now(timezone.utc) - cdt).total_seconds() / 3600)
            except Exception:
                pass
            out.append(it)
    except Exception:
        pass
    return out


def ad_eastmoney(src, query=""):
    bj = timezone(timedelta(hours=8))
    terms = _terms(query) if query else []
    out = []
    try:
        raw = _http_raw("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=50&req_trace=1", NEWS_TIMEOUT)
        data = json.loads(raw.decode("utf-8"))
        for n in (data.get("data") or {}).get("fastNewsList") or []:
            text = (n.get("summary") or "").strip()
            if not text or not matches_terms(text, terms):
                continue
            title = n.get("title") or text[:60]
            it = _mk(title, src["name"], "", None, text[:240], src.get("category", "宏观"), src.get("id"))
            try:
                dt = datetime.strptime(n.get("showTime", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=bj)
                it["age"] = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            except Exception:
                pass
            out.append(it)
    except Exception:
        pass
    return out


def ad_sina_fast(src, query=""):
    bj = timezone(timedelta(hours=8))
    terms = _terms(query) if query else []
    out = []
    try:
        raw = _http_raw("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0", NEWS_TIMEOUT)
        data = json.loads(raw.decode("utf-8"))
        feed = (((data.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        for f in feed:
            text = re.sub(r"<[^>]+>", "", f.get("rich_text") or "").strip()
            if not text or not matches_terms(text, terms):
                continue
            title = text[:55]
            it = _mk(title, src["name"], "", None, text[:240], src.get("category", "宏观"), src.get("id"))
            try:
                dt = datetime.strptime(f.get("create_time", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=bj)
                it["age"] = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            except Exception:
                pass
            out.append(it)
    except Exception:
        pass
    return out


def ad_x_mirror(src, query=""):
    q = query if query else "crypto OR bitcoin"
    has_cn = bool(re.search(r"[\u4e00-\u9fa5]", q))
    lang = "hl=zh-CN&gl=CN&ceid=CN:zh-Hans" if has_cn else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(q)}%20site%3Ax.com&{lang}"
    out = []
    try:
        for d in parse_items(_http_raw(url, NEWS_TIMEOUT)):
            title = d["title"]
            src_name = src["name"]
            if title.endswith(" - X"):
                title = title[:-4]
                m = re.search(r"@(\w+)", title)
                if m:
                    src_name = f"{src['name']} @{m.group(1)}"
            it = _mk(title, src_name, d["link"], d["pub"], "", src.get("category"), src.get("id"))
            if it["age"] is not None and it["age"] > 240:
                continue
            out.append(it)
    except Exception:
        pass
    return out


def ad_binance_ann(src, query=""):
    terms = _terms(query) if query else []
    raw = _http_raw("https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=48&pageNo=1&pageSize=30", NEWS_TIMEOUT)
    data = json.loads(raw.decode("utf-8"))
    out = []
    for a in (data.get("data") or {}).get("articles") or []:
        t = a.get("title") or ""
        if matches_terms(t, terms):
            it = _mk(t, src["name"], f"https://www.binance.com/en/support/announcement/{a.get('code')}",
                     None, "", src.get("category", "币圈"), src.get("id"))
            try:
                dt = datetime.fromisoformat(a.get("publishDate") or "").replace(tzinfo=timezone.utc)
                it["age"] = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            except Exception:
                pass
            out.append(it)
    return out


def ad_bybit_ann(src, query=""):
    terms = _terms(query) if query else []
    raw = _http_raw("https://api.bybit.com/v5/announcements/index?locale=en-US&limit=40", NEWS_TIMEOUT)
    data = json.loads(raw.decode("utf-8"))
    out = []
    for a in (data.get("result") or {}).get("list") or []:
        t = a.get("title") or ""
        desc = (a.get("description") or "")[:180]
        if matches_terms(t + " " + desc, terms):
            it = _mk(t, src["name"], a.get("url") or "https://www.bybit.com/announcements", None, desc, src.get("category", "币圈"), src.get("id"))
            ts = a.get("dateTimestamp")
            if ts:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                it["age"] = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            out.append(it)
    return out


def ad_x(src, query=""):
    tok = os.environ.get("X_BEARER_TOKEN", "")
    if not tok:
        raise RuntimeError("缺 X_BEARER_TOKEN")
    q = query if query else "crypto"
    url = ("https://api.x.com/2/tweets/search/recent?query=" + quote(f"{q} -is:retweet")
           + "&max_results=25&tweet.fields=created_at&expansions=author_id")
    raw = _http_raw(url, NEWS_TIMEOUT, {"Authorization": f"Bearer {tok}"})
    data = json.loads(raw.decode("utf-8"))
    users = {u["id"]: u.get("username", "") for u in (data.get("includes") or {}).get("users", [])}
    out = []
    for t in data.get("data") or []:
        text = re.sub(r"https://t\.co/\w+", "", t.get("text", "")).strip()
        if not text:
            continue
        u = users.get(t.get("author_id", ""), "")
        it = _mk(text[:150], f"{src['name']}@{u}", f"https://x.com/{u}/status/{t.get('id')}", None, "", src.get("category"), src.get("id"))
        try:
            cdt = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            it["age"] = max(0.0, (datetime.now(timezone.utc) - cdt).total_seconds() / 3600)
        except Exception:
            pass
        out.append(it)
    return out


ADAPTERS = {
    "rss": ad_rss,
    "json": ad_json,
    "google_news": ad_google,
    "reddit": ad_reddit,
    "hn": ad_hn,
    "eastmoney": ad_eastmoney,
    "sina_fast": ad_sina_fast,
    "x_mirror": ad_x_mirror,
    "binance_ann": ad_binance_ann,
    "bybit_ann": ad_bybit_ann,
    "x": ad_x
}


def test_source(src_type, url="", name="测试源", subs=None):
    """在线测试数据源连通性，返回是否成功、耗时、捕获到的样例条目"""
    start_time = time.time()
    dummy = {"name": name, "type": src_type, "url": url, "category": "测试", "id": "test_src"}
    if subs:
        dummy["subs"] = subs if isinstance(subs, list) else [x.strip() for x in str(subs).split(",") if x.strip()]

    fn = ADAPTERS.get(src_type)
    if not fn:
        return {"success": False, "error": f"不支持的类型: {src_type}"}

    try:
        items = fn(dummy, query="")
        latency_ms = int((time.time() - start_time) * 1000)
        if not items:
            return {
                "success": True,
                "latency_ms": latency_ms,
                "count": 0,
                "sample": [],
                "message": "连接成功，但当前未解析到条目内容（请检查 URL 是否为空或返回为空列表）"
            }
        sample = items[:3]
        return {
            "success": True,
            "latency_ms": latency_ms,
            "count": len(items),
            "sample": sample,
            "message": f"连接成功！耗时 {latency_ms}ms，解析出 {len(items)} 条内容"
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "latency_ms": latency_ms,
            "error": str(e)
        }


# ---------------------------------------------------------------- 聚合资讯流与检索

def fetch_aggregated_feed(category=None, query="", force_refresh=False, max_workers=10):
    """聚合已启用的所有数据源，输出最新的聚合资讯流"""
    # 读缓存
    now_ts = time.time()
    cache_data = {}
    if not force_refresh and not query and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                if now_ts - cache_data.get("timestamp", 0) < 180:  # 3分钟有效缓存
                    items = cache_data.get("items", [])
                    if category and category != "全部":
                        items = [x for x in items if x.get("category") == category]
                    return items, cache_data.get("statuses", [])
        except Exception:
            pass

    cfg = load_sources()
    jobs = []
    for s in cfg.get("sources", []):
        if not s.get("enabled", True):
            jobs.append((s, "off"))
        elif s["type"] == "none":
            jobs.append((s, "none"))
        elif s["type"] == "x" and not os.environ.get("X_BEARER_TOKEN", ""):
            jobs.append((s, "nokey"))
        elif s["type"] not in ADAPTERS:
            jobs.append((s, "unsupported"))
        elif category and category != "全部" and s.get("category") != category:
            continue
        else:
            jobs.append((s, "run"))

    def worker(job):
        s, state = job
        if state != "run":
            return state, s, []
        try:
            fn = ADAPTERS[s["type"]]
            res = fn(s, query=query)
            return "ok", s, res
        except Exception as e:
            return "fail", s, []

    items, statuses = [], []
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(jobs)))) as ex:
        futures = [ex.submit(worker, j) for j in jobs]
        for f in as_completed(futures):
            state, s, its = f.result()
            name = s["name"]
            if state == "ok":
                statuses.append({"id": s.get("id"), "name": name, "state": "ok", "detail": f"{len(its)} 条"})
                items.extend(its)
            elif state == "off":
                statuses.append({"id": s.get("id"), "name": name, "state": "off", "detail": "已停用"})
            elif state == "none":
                statuses.append({"id": s.get("id"), "name": name, "state": "none", "detail": "无公开API"})
            elif state == "nokey":
                statuses.append({"id": s.get("id"), "name": name, "state": "nokey", "detail": "缺Token"})
            else:
                statuses.append({"id": s.get("id"), "name": name, "state": "fail", "detail": "抓取失败"})

    # 去重 + 政治过滤（标题和正文一起查，防漏网）
    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: x["age"] if x.get("age") is not None else 1e9):
        key = re.sub(r"\W+", "", it["title"].lower())[:40]
        if key and key not in seen:
            seen.add(key)
            if classify(it["title"]) == "政治" or classify(it["title"] + " " + it.get("desc", "")) == "政治":
                continue
            uniq.append(it)

    # 写入缓存（仅全量抓取时）
    if not query and not category:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"timestamp": now_ts, "items": uniq[:300], "statuses": statuses}, f, ensure_ascii=False)
        except Exception:
            pass

    return uniq, statuses


def search_sources(query, category=None, source_ids=None, limit=50):
    """基于关键词检索已配置的数据源，并按自媒体价值度进行排序打分"""
    q = (query or "").strip()
    if not q:
        return [], []

    cfg = load_sources()
    active_sources = []
    for s in cfg.get("sources", []):
        if not s.get("enabled", True):
            continue
        if source_ids and s.get("id") not in source_ids:
            continue
        if category and category != "全部" and s.get("category") != category:
            continue
        if s.get("type") in ADAPTERS:
            active_sources.append(s)

    items, statuses = [], []

    def run_one(s):
        try:
            fn = ADAPTERS[s["type"]]
            res = fn(s, query=q)
            return "ok", s, res
        except Exception:
            return "fail", s, []

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(active_sources)))) as ex:
        futures = [ex.submit(run_one, s) for s in active_sources]
        for f in as_completed(futures):
            st, s, its = f.result()
            statuses.append({"name": s["name"], "state": st, "count": len(its)})
            items.extend(its)

    # 去重
    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: x["age"] if x.get("age") is not None else 1e9):
        key = re.sub(r"\W+", "", it["title"].lower())[:40]
        if key and key not in seen:
            seen.add(key)
            cat = it.get("category") or classify(it["title"])
            if cat == "政治" or classify(it["title"] + " " + it.get("desc", "")) == "政治":
                continue
            it["category"] = cat
            it["score"] = score_item(it, cat)
            uniq.append(it)

    uniq.sort(key=lambda x: -x["score"])
    return uniq[:limit], statuses


SRC_WEIGHT = [
    ("panews", 1.1), ("odaily", 1.1), ("techflow", 1.1),
    ("coindesk", 1.0), ("cointelegraph", 1.0), ("theblock", 1.0), ("decrypt", 1.0),
    ("eastmoney", 0.95), ("sina", 0.95), ("binance", 0.95), ("bybit", 0.95),
    ("reddit", 0.85), ("hn", 0.85), ("google", 0.88), ("x@", 0.9)
]


def src_weight(src):
    sl = (src or "").lower()
    for k, w in SRC_WEIGHT:
        if k in sl:
            return w
    return 1.0


def score_item(it, cat):
    base = {"币圈": 1.0, "AI": 0.95, "宏观": 0.9, "热点": 0.5}.get(cat, 0.5)
    age = it.get("age")
    rec = 1.0 if age is None else math.exp(-age / 48.0)
    bonus = 0.2 if re.search(r"\$\s?\d|\d+\s*%|\d[\d,.]*\s*[万亿]", it["title"], re.I) else 0.0
    if it.get("desc"):
        bonus += 0.08
    return base * src_weight(it.get("src", "")) * rec + bonus
