"""Narrative (题材板块) classification and rolling heat for new-token picks.

Meme launches move in narrative waves: coins in a hot theme correlate hard,
so "which narrative does this coin belong to, and is that narrative paying"
carries real information that single-token metrics do not.

Honest scope — this is a keyword heuristic over token names/symbols:
- launchers name-drop whatever is trending, so name roots are a usable proxy,
  but a match is NOT confirmation the coin belongs to the theme, and a
  miss is NOT proof it does not (typo'd or unrelated names classify as
  independent);
- heat numbers come only from our own paper-tracked picks (real 1h/6h fills)
  and live board events — nothing is imported from third-party theme indexes.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Root -> narrative. Ordered: the first root that matches wins, so put
# specific roots before generic ones. The big groups below were validated on
# our own pick history (14h sample, 2026-10-01/02): inu 犬系 n=20 h1 median
# -63%, pump n=8 h6 median +17%, ai n=14 h1 avg -1.5% (most resilient), cat
# n=18 fading (h6 median -30%) — narrative is a real discriminator.
NARRATIVE_LEXICON = [
    ("pepe",       "pepe",      "🐸 青蛙 Pepe 系"),
    ("frog",       "pepe",      "🐸 青蛙 Pepe 系"),
    ("toad",       "pepe",      "🐸 青蛙 Pepe 系"),
    ("inu",        "dog",       "🐕 犬系 Inu", ),
    ("doge",       "dog",       "🐕 犬系 Inu"),
    ("dog",        "dog",       "🐕 犬系 Inu"),
    ("shib",       "dog",       "🐕 犬系 Inu"),
    ("wif",        "dog",       "🐕 犬系 Inu"),
    ("puppy",      "dog",       "🐕 犬系 Inu"),
    ("cat",        "cat",       "🐱 猫系"),
    ("meow",       "cat",       "🐱 猫系"),
    ("kitty",      "cat",       "🐱 猫系"),
    ("neko",       "cat",       "🐱 猫系"),
    ("ai",         "ai",        "🤖 AI / Agent 系"),
    ("gpt",        "ai",        "🤖 AI / Agent 系"),
    ("agent",      "ai",        "🤖 AI / Agent 系"),
    ("bot",        "ai",        "🤖 AI / Agent 系"),
    ("llm",        "ai",        "🤖 AI / Agent 系"),
    ("virtual",    "ai",        "🤖 AI / Agent 系"),
    ("pump",       "pump",      "🎪 Pump 生态"),
    ("xmoney",     "xmoney",    "💳 X Money 支付系"),
    ("x money",    "xmoney",    "💳 X Money 支付系"),
    ("xpay",       "xmoney",    "💳 X Money 支付系"),
    ("x pay",      "xmoney",    "💳 X Money 支付系"),
    ("trump",      "politics",  "🗳️ 政治 / 名人梗系"),
    ("musk",       "politics",  "🗳️ 政治 / 名人梗系"),
    ("elon",       "politics",  "🗳️ 政治 / 名人梗系"),
    ("president",  "politics",  "🗳️ 政治 / 名人梗系"),
    ("america",    "politics",  "🗳️ 政治 / 名人梗系"),
    ("usa",        "politics",  "🗳️ 政治 / 名人梗系"),
    ("gov",        "politics",  "🗳️ 政治 / 名人梗系"),
    ("cash",       "money",     "💰 暴富金钱系"),
    ("money",      "money",     "💰 暴富金钱系"),
    ("rich",       "money",     "💰 暴富金钱系"),
    ("billionaire", "money",    "💰 暴富金钱系"),
    ("millionaire", "money",    "💰 暴富金钱系"),
    ("moon",       "moon",      "🚀 登月系"),
    ("rocket",     "moon",      "🚀 登月系"),
    ("奶龙",        "cn_meme",   "🐉 国产梗系"),
    ("牛马",        "cn_meme",   "🐉 国产梗系"),
    ("dragon",     "cn_meme",   "🐉 国产梗系"),
    ("panda",      "cn_meme",   "🐉 国产梗系"),
    ("china",      "cn_meme",   "🐉 国产梗系"),
    ("ape",        "ape",       "🐵 猿猴系"),
    ("monkey",     "ape",       "🐵 猿猴系"),
    ("banana",     "ape",       "🐵 猿猴系"),
    ("degen",      "degen",     "💎 Degen 赌狗系"),
    ("fomo",       "degen",     "💎 Degen 赌狗系"),
    ("jeet",       "degen",     "💎 Degen 赌狗系"),
    ("casino",     "degen",     "💎 Degen 赌狗系"),
    ("gambl",      "degen",     "💎 Degen 赌狗系"),
]

_NARRATIVE_BY_ID = {nid: label for _, nid, label in NARRATIVE_LEXICON}

# Link builders for per-token detail rows (chain ids as stored in picks/events)
_GMGN_CHAIN = {"SOL": "sol", "BASE": "base", "BSC": "bsc", "ARB": "arb", "ETH": "eth"}
_DEX_CHAIN = {"SOL": "solana", "BASE": "base", "BSC": "bsc", "ARB": "arbitrum", "ETH": "ethereum"}


def pick_links(chain: str, address: str) -> dict:
    g = _GMGN_CHAIN.get((chain or "").upper())
    d = _DEX_CHAIN.get((chain or "").upper())
    return {
        "chart_url": f"https://dexscreener.com/{d}/{address}" if d and address else "",
        "gmgn_url": f"https://gmgn.ai/{g}/token/{address}" if g and address else "",
    }


def narrative_label(nid: Optional[str]) -> Optional[str]:
    return _NARRATIVE_BY_ID.get(nid)


def classify_narrative(name: str, symbol: str) -> Optional[str]:
    """Map a token name/symbol to a narrative id, or None (independent).

    Short ASCII roots match at a word start only, so 'ai' hits 'AI Agent'
    and 'aigent' but not 'brain'. CJK and long roots match as substrings.
    """
    text = f"{symbol or ''} {name or ''}".lower()
    if not text.strip():
        return None
    for root, nid, _label in NARRATIVE_LEXICON:
        if any(ord(c) > 127 for c in root):
            if root in text:
                return nid
        elif len(root) <= 3:
            if re.search(rf"\b{re.escape(root)}", text):
                return nid
        elif root in text:
            return nid
    return None


# heat tiers, from the same paper-tracked evidence as the rest of the radar
TIER_META = {
    "hot":     ("🔥", "hot"),
    "warm":    ("🌡️", "warm"),
    "warming": ("❔", "warming"),   # tracked, no filled horizons yet
    "cool":    ("🌫️", "cool"),
    "cold":    ("❄️", "cold"),
}


def heat_tier(n_picks: int, med_h1: Optional[float]) -> str:
    if n_picks == 0:
        return "warming"
    if med_h1 is None:
        return "warming"
    if med_h1 >= 15.0:
        return "hot"
    if med_h1 >= 3.0 or (med_h1 > 0.0 and n_picks >= 3):
        return "warm"
    if med_h1 > -10.0:
        return "cool"
    return "cold"


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[len(s) // 2], 1)


def aggregate_narrative_heat(picks: List[dict], live_events: List[dict]) -> List[dict]:
    """Rolling narrative heat from our own measured data.

    ``picks``: paper-tracked picks inside the window (real h1/h6 fills).
    ``live_events``: current board events (live h1 momentum, "moving now").
    Both carry a ``narrative`` id, or one is derived from name/symbol.
    """
    groups: Dict[str, dict] = {}

    def bucket(nid: str) -> dict:
        return groups.setdefault(nid, {
            "id": nid,
            "label": narrative_label(nid) or nid,
            "tracked": 0,
            "h1": [], "h6": [],
            "live_coins": 0, "live_h1": [],
            "recent_picks": [],          # per-token detail for the narrative tab
            "live_tokens": [],
        })

    for p in picks:
        nid = p.get("narrative") or classify_narrative(p.get("name", ""), p.get("symbol", ""))
        if not nid:
            continue
        g = bucket(nid)
        g["tracked"] += 1
        horizons = p.get("horizons") or {}
        for h in ("h1", "h6"):
            v = horizons.get(h)
            if v is not None:
                g[h].append(float(v))
        g["recent_picks"].append({
            "symbol": p.get("symbol", "?"),
            "name": p.get("name", ""),
            "chain": p.get("chain", "?"),
            "address": p.get("address", ""),
            "time": p.get("time", ""),
            "ts": p.get("ts", 0),
            "h1": horizons.get("h1"),
            "h6": horizons.get("h6"),
            "status": p.get("status", "?"),
            **pick_links(p.get("chain", ""), p.get("address", "")),
        })

    for ev in live_events:
        tok = ev.get("token", {})
        nid = (ev.get("narrative") or {}).get("id") if isinstance(ev.get("narrative"), dict) else None
        nid = nid or classify_narrative(tok.get("name", ""), tok.get("symbol", ""))
        if not nid:
            continue
        g = bucket(nid)
        g["live_coins"] += 1
        ch = tok.get("change_h1")
        if ch is not None:
            g["live_h1"].append(float(ch))
        g["live_tokens"].append({
            "symbol": tok.get("symbol", "?"),
            "name": tok.get("name", ""),
            "chain": ev.get("chain", "?"),
            "address": tok.get("address", ""),
            "change_h1": ch,
            "change_h24": tok.get("change_h24"),
            "market_cap": tok.get("market_cap", 0.0),
            "chart_url": ev.get("chart_url", ""),
            "gmgn_url": ev.get("gmgn_url", ""),
        })

    res = []
    for g in groups.values():
        med_h1 = _median(g["h1"])
        tier = heat_tier(g["tracked"], med_h1)
        emoji, _ = TIER_META[tier]
        res.append({
            **{k: v for k, v in g.items() if k not in ("h1", "h6", "live_h1")},
            "h1_n": len(g["h1"]),
            "h1_median": med_h1,
            "h1_win_rate": round(sum(1 for v in g["h1"] if v > 0) / len(g["h1"]), 2) if g["h1"] else None,
            "h6_median": _median(g["h6"]),
            "live_h1_median": _median(g["live_h1"]),
            "tier": tier,
            "tier_emoji": emoji,
            "recent_picks": sorted(g["recent_picks"], key=lambda t: -t.get("ts", 0))[:20],
            "live_tokens": g["live_tokens"][:12],
        })

    # hottest first: tier rank, then measured h1 median, then breadth
    tier_rank = {"hot": 0, "warm": 1, "warming": 2, "cool": 3, "cold": 4}
    res.sort(key=lambda g: (tier_rank[g["tier"]],
                            -(g["h1_median"] if g["h1_median"] is not None else -1e9),
                            -g["tracked"]))
    return res
