"""Radar tests — fully offline (all HTTP monkeypatched).

The suite pins the honesty contract of the radar:
- no fabricated fields: events carry no wallet attribution, no copy-trade
  status, no randomized amounts/ages; unknowns stay None/UNKNOWN;
- the security gate fails CLOSED when GoPlus is unreachable;
- the wallet DB ships empty (no invented track records).
"""
import json
import tempfile
import time
from urllib.parse import parse_qs, urlparse

import pytest

from flygate.radar import http as radar_http
from flygate.radar.security import check_token_security, _SECURITY_CACHE
from flygate.radar.multi_chain import query_wallet_profile
from flygate.radar.narratives import classify_narrative, aggregate_narrative_heat
from flygate.radar.tracker import RadarTracker, compute_token_age, PICK_HORIZONS, HORIZON_GRACE_S
from flygate.radar.wallet_db import WalletDB

TOKEN_ADDR = "TokenAAA111111111111111111111111111111111"

SAFE_SOL_SEC = {"mintable": "0", "freezable": "0", "is_honeypot": "0", "holders_percent": "0.15"}

SEARCH_PAIR = {
    "chainId": "solana",
    "dexId": "raydium",
    "url": f"https://dexscreener.com/solana/{TOKEN_ADDR}",
    "pairAddress": "PairAAA1111111111111111111111111111111111",
    "pairCreatedAt": (time.time() - 25 * 60) * 1000,   # 25 minutes ago
    "baseToken": {"address": TOKEN_ADDR, "symbol": "AAA", "name": "A Token"},
    "priceUsd": "0.0012",
    "fdv": "80000",
    "liquidity": {"usd": 45000},
    "volume": {"m5": 1200.0},
    "txns": {"m5": {"buys": 20, "sells": 8}},
    "priceChange": {"m5": 1.2, "h1": 5.0, "h24": 10.0},
}


def _contract_from_url(url: str) -> str:
    return url.split("contract_addresses=")[1]


def make_fake_http(goplus_result=None, fail_goplus=False):
    """URL-dispatching stand-in for flygate.radar.http.get_json."""
    def fake_get_json(url, timeout=4.0, retries=1):
        low = url.lower()
        if "gopluslabs.io" in low:
            if fail_goplus:
                raise radar_http.HttpError("network down")
            if goplus_result is not None:
                return {"result": {_contract_from_url(url): goplus_result}}
            if "tokenaaa" in low:
                return {"result": {_contract_from_url(url): SAFE_SOL_SEC}}
            return {"result": {}}
        if "dexscreener.com" in low:
            # token lookups only resolve for TOKEN_ADDR
            if "tokenaaa" in low:
                return {"pairs": [SEARCH_PAIR]}
            if "search" in low:
                q = (parse_qs(urlparse(url).query).get("q") or [""])[0].lower()
                # short queries are trending search terms (return the pair);
                # long ones are address lookups that should not resolve
                if "tokenaaa" in q or len(q) < 15:
                    return {"pairs": [SEARCH_PAIR]}
                return {"pairs": []}
            if "token-profiles" in low:
                return []
            return {"pairs": []}
        raise radar_http.HttpError(f"unexpected url in test: {url}")
    return fake_get_json


@pytest.fixture(autouse=True)
def clean_security_cache():
    _SECURITY_CACHE.clear()
    yield
    _SECURITY_CACHE.clear()


@pytest.fixture
def fake_http(monkeypatch):
    fake = make_fake_http()
    monkeypatch.setattr(radar_http, "get_json", fake)
    return fake


# ---------------------------------------------------------------- wallet db
def test_wallet_db_starts_empty_and_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = WalletDB(tmpdir)
        assert db.list_wallets() == []          # ships empty: no invented presets

        w = db.add_wallet("BASE", "0x1234567890abcdef1234567890abcdef12345678",
                          alias="Test Whale")
        assert w["win_rate"] is None            # no fabricated track record
        assert w["pnl_usd"] is None

        w2 = db.add_wallet("SOL", "SoLAddR11111111111111111111111111111111111",
                           alias="With note", win_rate=0.88, pnl_usd=99000.0)
        assert w2["win_rate"] == 0.88           # user-supplied values pass through

        assert db.toggle_wallet(w2["address"], False) is True
        assert db.get_wallet(w2["address"]).enabled is False
        assert db.remove_wallet(w2["address"]) is True
        assert db.get_wallet(w2["address"]) is None
        assert len(db.list_wallets()) == 1


# ---------------------------------------------------------------- security
def test_security_fails_closed_when_goplus_unreachable(monkeypatch):
    monkeypatch.setattr(radar_http, "get_json", make_fake_http(fail_goplus=True))
    sec = check_token_security("SOL", TOKEN_ADDR, pool_liquidity=50000.0)
    assert sec["checked"] is False
    assert sec["risk_level"] == "UNKNOWN"
    assert sec["is_safe"] is False                 # never "safe" by absence of data
    assert sec["score"] is None
    assert any("不可用" in w for w in sec["warnings"])


def test_security_honeypot_detected(monkeypatch):
    honeypot = {"is_honeypot": "1", "cannot_sell_all": "0", "buy_tax": "0",
                "sell_tax": "0", "is_mintable": "0", "is_open_source": "1"}
    monkeypatch.setattr(radar_http, "get_json",
                        make_fake_http(goplus_result=honeypot))
    evm_addr = "0x1234567890abcdef1234567890abcdef12345678"
    sec = check_token_security("BASE", evm_addr, pool_liquidity=50000.0)
    assert sec["checked"] is True
    assert sec["is_safe"] is False
    assert sec["risk_level"] == "CRITICAL"


def test_security_clean_token_is_safe(fake_http):
    sec = check_token_security("SOL", TOKEN_ADDR, pool_liquidity=50000.0)
    assert sec["checked"] is True
    assert sec["is_safe"] is True
    assert sec["risk_level"] == "LOW"
    assert sec["score"] >= 80


def test_security_low_liquidity_penalized(monkeypatch):
    monkeypatch.setattr(radar_http, "get_json", make_fake_http())
    sec = check_token_security("SOL", TOKEN_ADDR, pool_liquidity=500.0)
    assert sec["checked"] is True
    assert sec["score"] < 80
    assert any("流动性" in w for w in sec["warnings"])


# ------------------------------------------------------------- token age
def test_compute_token_age_real_timestamps():
    now = time.time()
    is_new, age_min, text, tier = compute_token_age((now - 1200) * 1000, now_sec=now)
    assert is_new is True and 19 <= age_min <= 21
    assert "刚上线" in text and tier == "JUST_LAUNCHED"

    _, age_min, text, tier = compute_token_age((now - 2 * 86400) * 1000, now_sec=now)
    assert age_min >= 2880 and tier == "MATURE"


def test_compute_token_age_unknown_is_honest():
    for missing in (None, 0):
        is_new, age_min, text, tier = compute_token_age(missing)
        assert is_new is False
        assert age_min is None                       # never invented
        assert text == "币龄未知"
        assert tier == "UNKNOWN"


# ---------------------------------------------------------------- scanner
def test_tracker_scan_emits_real_event_without_fabrication(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        assert tracker.get_feed() == []              # nothing is seeded

        tracker._scan_chain("SOL")
        feed = tracker.get_feed()
        assert len(feed) == 1
        ev = feed[0]

        # honesty contract: no wallet attribution, no copy-trade, no random amounts
        assert "wallet" not in ev
        assert "copy_trade" not in ev
        assert ev["source"] in ("trending", "new_launch")

        # real values propagate from the DexScreener pair
        assert ev["token"]["address"] == TOKEN_ADDR
        assert ev["token"]["market_cap"] == 80000.0
        assert ev["token"]["liquidity_usd"] == 45000.0
        assert abs(ev["avg_trade_usd"] - round(1200.0 / 28, 1)) < 0.01

        # 25-minute-old token with $80k mcap, clean audit -> new-token watch
        assert ev["decision"]["status"] == "NEW_TOKEN_WATCH"
        assert ev["security"]["checked"] is True

        # push gate: the clean token passed and is counted
        gate = tracker.get_gate_stats()
        assert gate["passed"] == 1
        assert gate["blocked_total"] == 0
        assert gate["since_ts"] > 0

        # dedupe: the same token is not re-emitted within the TTL
        tracker._scan_chain("SOL")
        assert len(tracker.get_feed()) == 1


def test_tracker_scan_dump_detection_blocked_by_gate(monkeypatch):
    """跳水破位的币被门槛拦下：计数不推送——不再淹没真正的信号（实测 1h 胜率 22%）。"""
    crashing = json.loads(json.dumps(SEARCH_PAIR))
    crashing["priceChange"] = {"m5": -9.5, "h1": -18.0, "h24": -50.0}

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and ("tokenaaa" in low or "search" in low):
            return {"pairs": [crashing]}
        if "gopluslabs.io" in low and "tokenaaa" in low:
            return {"result": {_contract_from_url(url): SAFE_SOL_SEC}}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []              # never pushed
        assert tracker.picks == {}                   # never paper-tracked
        gate = tracker.get_gate_stats()
        assert gate["blocked"]["dump"] == 1
        assert gate["passed"] == 0


def test_tracker_scan_unaudited_token_blocked_by_gate(monkeypatch):
    """审计不可用的币 fail-closed：不推送（安全性未知先当不可信），计数进 security。"""
    monkeypatch.setattr(radar_http, "get_json", make_fake_http(fail_goplus=True))
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []
        gate = tracker.get_gate_stats()
        assert gate["blocked"]["security"] == 1


# ---------------------------------------------------------------- push gate
def test_push_gate_blocks_paper_liquidity_without_audit(monkeypatch):
    """纸级流动性在本地就被拦下——不为它花任何 GoPlus 调用。"""
    paper = json.loads(json.dumps(SEARCH_PAIR))
    paper["liquidity"] = {"usd": 5000}
    calls = []

    def routed(url, timeout=4.0, retries=1):
        calls.append(url)
        low = url.lower()
        if "dexscreener.com" in low:
            if "tokenaaa" in low:
                return {"pairs": [paper]}
            if "search" in low:
                # short trending terms hit the paper pair; the watchlist seed's
                # long address lookup must not resolve to it
                q = (parse_qs(urlparse(url).query).get("q") or [""])[0].lower()
                return {"pairs": [paper]} if len(q) < 15 else {"pairs": []}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []
        assert tracker.get_gate_stats()["blocked"]["liquidity"] == 1
        # no audit was spent on the gated token itself
        assert not any("gopluslabs.io" in u and "tokenaaa" in u.lower()
                       for u in calls)


def test_push_gate_blocks_low_mcap(monkeypatch):
    """<$20k 市值（连观察级下限都不够）不推送不展示。"""
    lottery = json.loads(json.dumps(SEARCH_PAIR))
    lottery["fdv"] = "15000"

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and ("tokenaaa" in low or
                ("search" in low and len((parse_qs(urlparse(url).query).get("q") or [""])[0]) < 15)):
            return {"pairs": [lottery]}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []
        assert tracker.get_gate_stats()["blocked"]["market_cap"] == 1


def test_watch_tier_near_misses_surface_labelled(fake_http, monkeypatch):
    """差一口气的币（审计✔不跳水、市值 $30k<$50k）以🧪观察级上榜，不占推送级。"""
    near_miss = json.loads(json.dumps(SEARCH_PAIR))
    near_miss["fdv"] = "30000"          # above watch floor, below push line

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and ("tokenaaa" in low or
                ("search" in low and len((parse_qs(urlparse(url).query).get("q") or [""])[0]) < 15)):
            return {"pairs": [near_miss]}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        feed = tracker.get_feed()
        assert len(feed) == 1
        ev = feed[0]
        assert ev["push_tier"] == "watch"
        assert ev["decision"]["badge"].startswith("🧪 观察级")
        # 25min age + $30k mcap lands in the ultra-early branch of the cascade
        assert ev["decision"]["status"] == "ULTRA_EARLY_GAMBLE"
        gate = tracker.get_gate_stats()
        assert gate["passed"] == 0 and gate["watch"] == 1 and gate["blocked_total"] == 0
        # still paper-tracked, so the board can measure whether watch-tier
        # coins deserve promotion later
        assert tracker.picks["SOL:" + TOKEN_ADDR]["status"] == "ULTRA_EARLY_GAMBLE"
        summary = tracker.get_new_tokens_summary()
        assert summary[0]["attention_badge"].startswith("🧪")


def test_push_gate_missing_liquidity_or_mcap_does_not_block(monkeypatch):
    """DexScreener 漏回 liq/mcap（=0，数据缺失）不拦截——与决策级联同语义。
    币龄取 50 分钟（≥30min），否则 mcap=0 会落进极早期赌博分支被拦。"""
    missing = json.loads(json.dumps(SEARCH_PAIR))
    missing["pairCreatedAt"] = (time.time() - 50 * 60) * 1000
    missing["liquidity"] = {"usd": 0}
    missing["fdv"] = None

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and "tokenaaa" in low:
            return {"pairs": [missing]}
        if "dexscreener.com" in low and "search" in low:
            q = (parse_qs(urlparse(url).query).get("q") or [""])[0].lower()
            return {"pairs": [missing]} if len(q) < 15 else {"pairs": []}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert len(tracker.get_feed()) == 1          # emitted, audited clean


def test_mature_and_unknown_age_tokens_are_skipped(monkeypatch):
    """新币雷达: mature tokens and unverifiable-age tokens never get emitted."""
    mature = json.loads(json.dumps(SEARCH_PAIR))
    mature["pairCreatedAt"] = (time.time() - 90 * 86400) * 1000    # 90 days old
    unknown = json.loads(json.dumps(SEARCH_PAIR))
    unknown["pairCreatedAt"] = None                                # unverifiable
    unknown["baseToken"]["address"] = "UnknownAgeTok11111111111111111111111"

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and ("tokenaaa" in low or "search" in low or "unknownage" in low):
            return {"pairs": [mature, unknown]}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []            # nothing emitted
        assert tracker.picks == {}                 # nothing tracked


# ------------------------------------------------------------ new token board
def test_new_tokens_summary_counts_scan_hits(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        first = tracker.get_feed()[0]

        # simulate two more sightings
        for delta in (60, 120):
            dup = json.loads(json.dumps(first))
            dup["ts"] = first["ts"] + delta
            tracker.events.appendleft(dup)

        summary = tracker.get_new_tokens_summary()
        assert len(summary) == 1
        row = summary[0]
        assert row["scan_hits"] == 3
        assert row["attention_badge"].startswith("🔥")
        assert row["first_seen_time"] == first["time"]

        # a token whose age is unknown never lands on the "new tokens" board
        unknown_age = json.loads(json.dumps(first))
        unknown_age["is_new_token"] = False
        unknown_age["token"]["address"] = "UnknownAgeToken111111111111111111111"
        tracker.events.appendleft(unknown_age)
        assert all(s["address"] != "UnknownAgeToken111111111111111111111"
                   for s in tracker.get_new_tokens_summary())


def test_new_tokens_summary_dump_badge(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        ev = tracker.get_feed()[0]
        ev["token"]["is_dumping"] = True
        summary = tracker.get_new_tokens_summary()
        assert summary[0]["attention_badge"].startswith("⛔")


# ------------------------------------------------------- narrative layer
def test_narrative_classification_roots():
    # long roots match as substrings
    assert classify_narrative("Pepe King", "PEPE2") == "pepe"
    assert classify_narrative("baby doge", "BDOGE") == "dog"
    assert classify_narrative("AI agent", "AGENT") == "ai"
    # short ASCII roots match at word start only: 'brain' is not the ai theme
    assert classify_narrative("brain trust", "BRAIN") is None
    assert classify_narrative("aigent mk2", "AIGENT") == "ai"   # typo'd still caught
    # CJK roots match as substrings
    assert classify_narrative("奶龙", "NAILONG") == "cn_meme"
    # first matching root in lexicon order wins: 'inu' is listed before 'cat'
    assert classify_narrative("cat inu", "CATINU") == "dog"
    # unmatchable stays independent (never invented)
    assert classify_narrative("Totally Random Name", "XYZABC") is None
    assert classify_narrative("", "") is None


def test_narrative_heat_aggregation_and_tiers():
    picks = [
        # pepe board pumping: 2 tracked, both positive h1 -> hot/warm tier
        {"name": "pepe king", "symbol": "PEPE1", "narrative": "pepe",
         "chain": "SOL", "address": "PepeAAAA111111111111111111111111111111111",
         "horizons": {"h1": 40.0, "h6": 80.0}},
        {"name": "frog god", "symbol": "FROG", "narrative": "pepe",
         "chain": "SOL", "address": "FrogAAAA111111111111111111111111111111111",
         "horizons": {"h1": 10.0, "h6": None}},
        # inu board bleeding
        {"name": "doge killer", "symbol": "INU", "narrative": "dog",
         "horizons": {"h1": -60.0, "h6": -70.0}},
        # tracked but no fills yet -> warming
        {"name": "ai agent", "symbol": "AIX", "narrative": "ai",
         "horizons": {"h1": None, "h6": None}},
    ]
    live = [
        {"narrative": {"id": "pepe"}, "token": {"symbol": "PEPE9", "name": "x", "change_h1": 22.0}},
        {"narrative": {"id": None}, "token": {"symbol": "ZZZ", "name": "unrelated", "change_h1": 99.0}},
    ]
    res = aggregate_narrative_heat(picks, live)
    by_id = {r["id"]: r for r in res}
    assert set(by_id) == {"pepe", "dog", "ai"}

    pepe = by_id["pepe"]
    assert pepe["tracked"] == 2 and pepe["h1_median"] == 40.0
    assert pepe["tier"] == "hot"                    # median >= 15
    assert pepe["live_coins"] == 1 and pepe["live_h1_median"] == 22.0
    # per-token detail for the narrative tab: tracked picks + live board rows
    assert {t["symbol"] for t in pepe["recent_picks"]} == {"PEPE1", "FROG"}
    assert pepe["recent_picks"][0]["h1"] in (40.0, 10.0)
    # every detail row carries K-line + chip links (dexscreener + gmgn)
    for t in pepe["recent_picks"]:
        assert t["chart_url"].startswith("https://dexscreener.com/")
        assert t["gmgn_url"].startswith("https://gmgn.ai/sol/token/")
    assert pepe["live_tokens"][0]["symbol"] == "PEPE9"
    assert by_id["dog"]["recent_picks"][0]["h6"] == -70.0

    dog = by_id["dog"]
    assert dog["h1_median"] == -60.0 and dog["tier"] == "cold"

    ai = by_id["ai"]
    assert ai["tier"] == "warming"                  # no fills yet, honest

    # hottest first
    assert res[0]["id"] == "pepe"

    # a pick without a narrative field is classified from name/symbol on read
    legacy = [{"name": "musk coin", "symbol": "MUSK", "horizons": {"h1": 5.0, "h6": None}}]
    res2 = aggregate_narrative_heat(legacy, [])
    assert res2[0]["id"] == "politics" and res2[0]["tracked"] == 1


def test_tracker_event_and_pick_carry_narrative(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        ev = tracker.get_feed()[0]
        # SEARCH_PAIR ("A Token"/AAA) matches no root -> independent, stated honestly
        assert ev["narrative"] == {"id": None, "label": None}
        pick = tracker.picks["SOL:" + TOKEN_ADDR]
        assert pick["narrative"] is None

        # a pepe-named pair that clears the gate is labelled and its narrative
        # shows up in the rolling stats
        pepe_pair = json.loads(json.dumps(SEARCH_PAIR))
        pepe_pair["baseToken"] = {"address": "PepeTok111111111111111111111111111111111",
                                  "symbol": "PEPE", "name": "Pepe King"}
        def pepe_http(url, timeout=4.0, retries=1):
            low = url.lower()
            if "dexscreener.com" in low and ("pepetok" in low or
                    ("search" in low and len((parse_qs(urlparse(url).query).get("q") or [""])[0]) < 15)):
                return {"pairs": [pepe_pair]}
            if "gopluslabs.io" in low and "pepetok" in low:
                return {"result": {_contract_from_url(url): {"mintable": "0", "freezable": "0",
                                                             "is_honeypot": "0", "holders_percent": "0.1"}}}
            return make_fake_http()(url, timeout, retries)

        import flygate.radar.tracker as T
        orig = radar_http.get_json
        radar_http.get_json = pepe_http
        try:
            tracker._scan_chain("SOL")
        finally:
            radar_http.get_json = orig

        ev2 = [e for e in tracker.get_feed() if e["token"]["address"].startswith("PepeTok")][0]
        assert ev2["narrative"]["id"] == "pepe"
        assert "题材" in ev2["decision"]["reason"]

        nar = {n["id"]: n for n in tracker.get_narrative_stats()}
        assert "pepe" in nar and nar["pepe"]["tracked"] == 1


def test_push_gate_blocks_junk_placeholder_names(monkeypatch):
    """test / broadcast:N / 单字符占位名：数值门槛拦不住，名字门槛拦。"""
    junk = json.loads(json.dumps(SEARCH_PAIR))
    junk["baseToken"] = {"address": "JunkTest111111111111111111111111111111111",
                         "symbol": "test", "name": "broadcast:8472016"}

    def routed(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and ("junktest" in low or
                ("search" in low and len((parse_qs(urlparse(url).query).get("q") or [""])[0]) < 15)):
            return {"pairs": [junk]}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", routed)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        assert tracker.get_feed() == []
        assert tracker.get_gate_stats()["blocked"]["junk_name"] == 1

    from flygate.radar.tracker import _is_junk_name
    assert _is_junk_name("币", "币") is True
    assert _is_junk_name("?", "?") is True
    assert _is_junk_name("123", "123456") is True
    assert _is_junk_name("PEPE", "Pepe King") is False
    assert _is_junk_name("GAMBY", "Gamby") is False


def test_watchlist_events_never_board_as_new_tokens(fake_http):
    """自选盯盘事件（用户自己的持仓，未经推送门槛）不得混进新币扫描榜。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")                     # one gated scan event
        scan_ev = tracker.get_feed()[0]

        wl_ev = json.loads(json.dumps(scan_ev))
        wl_ev["source"] = "watchlist"
        wl_ev["action"] = "ALERT"
        tracker.events.appendleft(wl_ev)               # same token, watchlist path

        summary = tracker.get_new_tokens_summary()
        assert len(summary) == 1                       # counted once, from the scan only
        assert summary[0]["scan_hits"] == 1


def test_narrative_xmoney_theme():
    assert classify_narrative("X Money", "XMONEY") == "xmoney"
    assert classify_narrative("xpay", "XPAY") == "xmoney"
    assert classify_narrative("x money launch", "X") == "xmoney"
    # look-alikes must not match: "1000X" has no space/word 'money' or 'pay'
    assert classify_narrative("1000X", "1000X") is None
    # musk/elon still land in politics when the name is not x-money specific
    assert classify_narrative("Elon Musk coin", "MUSK") == "politics"


def test_solana_holdings_from_rpc(monkeypatch):
    """Wallet holdings = real RPC snapshot + DexScreener enrichment, dust dropped."""
    from flygate.radar import multi_chain as mc
    from flygate.radar import http as radar_http_mod

    def fake_post(url, payload, timeout=6.0, retries=1):
        assert "getTokenAccountsByOwner" in payload["method"]
        return {"result": {"value": [
            {"account": {"data": {"parsed": {"info": {
                "mint": "So11111111111111111111111111111111111111112",   # wSOL skip
                "tokenAmount": {"uiAmount": 3.0}}}}}},
            {"account": {"data": {"parsed": {"info": {
                "mint": "DustMint11111111111111111111111111111111111",   # dust skip
                "tokenAmount": {"uiAmount": 100.0}}}}}},
            {"account": {"data": {"parsed": {"info": {
                "mint": "PepeMint1111111111111111111111111111111111",
                "tokenAmount": {"uiAmount": 5000.0}}}}}},
        ]}}

    def fake_get(url, timeout=4.0, retries=1):
        if "dex/tokens" in url:
            assert "PepeMint" in url and "DustMint" in url    # both queried, dust filtered later
            return {"pairs": [
                {"chainId": "solana", "dexId": "raydium", "url": "https://x/1",
                 "baseToken": {"address": "PepeMint1111111111111111111111111111111111",
                               "symbol": "PEPE", "name": "Pepe"},
                 "priceUsd": "0.001", "fdv": "90000",
                 "liquidity": {"usd": 40000}, "priceChange": {"h24": 12.0}},
                {"chainId": "solana", "dexId": "raydium", "url": "https://x/2",
                 "baseToken": {"address": "DustMint11111111111111111111111111111111111",
                               "symbol": "DUST", "name": "Dust"},
                 "priceUsd": "0.0001", "fdv": "2000",
                 "liquidity": {"usd": 3000}, "priceChange": {"h24": 0.0}},
            ]}
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http_mod, "post_json", fake_post)
    monkeypatch.setattr(radar_http_mod, "get_json", fake_get)
    hs = mc.fetch_solana_holdings("FakeWallet111111111111111111111111111111111")
    assert len(hs) == 1                       # wSOL skipped, dust filtered
    assert hs[0]["symbol"] == "PEPE" and hs[0]["liquidity_usd"] == 40000.0


def test_wallet_profile_carries_holdings(fake_http, monkeypatch):
    from flygate.radar import multi_chain as mc
    calls = {"n": 0}
    def no_rpc(url, payload, timeout=6.0, retries=1):
        calls["n"] += 1
        return {"result": {"value": []}}      # empty wallet, but RPC answered
    import flygate.radar.http as h
    monkeypatch.setattr(h, "post_json", no_rpc)

    prof = mc.query_wallet_profile("SOL", "UnknownSoL111111111111111111111111111111111111")
    assert prof["holdings"] == []             # honest empty, no invented data
    assert calls["n"] == 1                    # the RPC really was consulted
    evm = mc.query_wallet_profile("BASE", "0x1234567890abcdef1234567890abcdef12345678")
    assert evm["holdings"] == [] and "Solana" in (evm.get("holdings_note") or "")


# ---------------------------------------------------------------- queries
def test_query_unknown_wallet_has_no_invented_track_record(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        unknown = "UnknownSoL111111111111111111111111111111111111"
        res = tracker.query("SOL", unknown)
        assert res["type"] == "wallet"
        assert res["data"]["win_rate"] is None
        assert res["data"]["pnl_usd"] is None
        assert "未收录" in res["data"]["source"]
        assert "solscan.io" in res["data"]["explorer_url"]


def test_query_token_resolves_via_dexscreener(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        res = tracker.query("SOL", TOKEN_ADDR)
        assert res["type"] == "token"
        assert res["data"]["price_usd"] == 0.0012
        assert res["data"]["market_cap"] == 80000.0


def test_query_with_chain_all_resolves_real_chain(fake_http):
    """chain=ALL 查询必须从交易对反推真实链，否则自选入库和审计都会失效。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        res = tracker.query("ALL", TOKEN_ADDR)
        assert res["type"] == "token"
        assert res["data"]["chain"] == "SOL"          # resolved, not "ALL"
        assert res["data"]["security"]["checked"] is True

        # watchlist item stores the resolved chain
        item = tracker.add_to_watchlist("ALL", TOKEN_ADDR)
        assert item["chain"] == "SOL"


def test_evm_addresses_are_lowercased_for_dexscreener(monkeypatch):
    """DexScreener 对 EVM 地址大小写敏感：混合大小写合约必须先转小写再查。"""
    seen = []

    def capturing(url, timeout=4.0, retries=1):
        seen.append(url)
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", capturing)
    from flygate.radar.multi_chain import query_token_info
    info = query_token_info("BSC", "0xABCdef00112233445566778899aabbccddeeff11")
    for url in seen:
        if "dexscreener.com" in url:
            assert "0xABCdef" not in url          # never queried with mixed case
    assert info["token_address"] == "0xabcdef00112233445566778899aabbccddeeff11"


def test_wallet_profile_known_entry(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = WalletDB(tmpdir)
        db.add_wallet("SOL", "SoLAddR11111111111111111111111111111111111", alias="My watch")
        prof = query_wallet_profile("SOL", "soladdr11111111111111111111111111111111111", db)
        assert prof["alias"] == "My watch"
        assert prof["source"] == "本地地址收藏池"


# ------------------------------------------------------- pick paper tracking
def test_picks_recorded_evaluated_and_persisted(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")

        assert len(tracker.picks) == 1                     # first sight -> one pick
        pick = tracker.picks["SOL:" + TOKEN_ADDR]
        assert pick["entry_price_usd"] == 0.0012
        assert pick["status"] == "NEW_TOKEN_WATCH"
        assert all(pick["horizons"][h] is None for h, _ in PICK_HORIZONS)
        # push-moment snapshot: this is what future data-driven gates learn from
        snap = pick["snapshot"]
        assert snap["mcap"] == 80000.0 and snap["liq"] == 45000.0
        assert snap["ch_m5"] == 1.2 and snap["ch_h1"] == 5.0
        assert 24.0 <= snap["age_min"] <= 30.0 and snap["avg_trade_usd"] == round(1200.0 / 28, 1)

        # re-sighting (after the emit TTL) does not duplicate the pick,
        # only bumps the hit counter
        tracker._seen[TOKEN_ADDR] = 0.0
        tracker._scan_chain("SOL")
        assert len(tracker.picks) == 1
        assert tracker.picks["SOL:" + TOKEN_ADDR]["scan_hits"] == 2

        # nothing is due yet -> horizons stay empty
        tracker._eval_picks(now=pick["ts"] + 60)
        assert pick["horizons"]["h1"] is None

        try:
            # price doubles; h1 and h6 are due, h24 is not
            SEARCH_PAIR["priceUsd"] = "0.0024"
            tracker._eval_picks(now=pick["ts"] + 7 * 3600)
            assert pick["horizons"]["h1"] == 100.0
            assert pick["horizons"]["h6"] == 100.0
            assert pick["horizons"]["h24"] is None

            # h24 becomes due
            tracker._eval_picks(now=pick["ts"] + 25 * 3600)
            assert pick["horizons"]["h24"] == 100.0
        finally:
            SEARCH_PAIR["priceUsd"] = "0.0012"

        # summary: per-badge stats with win rate / mean / survival
        s = tracker.get_picks_summary()
        assert s["total"] == 1
        assert s["open"] == 0
        assert s["all"]["h1"]["win_rate"] == 1.0
        assert s["all"]["h1"]["avg_pct"] == 100.0
        assert s["all"]["h1"]["survival_rate"] == 1.0       # +100% is alive
        assert s["groups"][0]["status"] == "NEW_TOKEN_WATCH"
        assert s["groups"][0]["by"]["h24"]["n"] == 1

        # persistence across restarts
        tracker2 = RadarTracker(tmpdir)
        assert len(tracker2.picks) == 1
        assert tracker2.picks["SOL:" + TOKEN_ADDR]["horizons"]["h24"] == 100.0


def test_picks_price_unavailable_stays_open_then_expires(fake_http, monkeypatch):
    # price lookups fail entirely: horizons stay None while inside grace window
    from flygate.radar import tracker as tracker_mod

    def dead_price(url, timeout=4.0, retries=1):
        low = url.lower()
        if "dexscreener.com" in low and "tokenaaa" in low:
            return {"pairs": []}                       # token gone / not resolvable
        return make_fake_http()(url, timeout, retries)

    monkeypatch.setattr(radar_http, "get_json", dead_price)
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        tracker._scan_chain("SOL")
        pick = tracker.picks["SOL:" + TOKEN_ADDR]

        tracker._eval_picks(now=pick["ts"] + 2 * 3600)
        assert pick["horizons"]["h1"] is None

        # past the grace window the horizon expires (still None) and pick closes
        far = pick["ts"] + 25 * 3600 + HORIZON_GRACE_S + 1
        tracker._eval_picks(now=far)
        assert pick["horizons"]["h1"] is None          # honest: unavailable, not zero
        summary = tracker.get_picks_summary()
        assert summary["total"] == 1


def test_watchlist_source_never_recorded_as_pick(fake_http):
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = RadarTracker(tmpdir)
        ev = {"source": "watchlist", "chain": "SOL", "ts": 1.0,
              "token": {"address": "TokXYZ", "price_usd": 1.0},
              "decision": {"status": "WATCH_BREAKOUT", "badge": "x"}}
        tracker._record_pick(ev)
        assert tracker.picks == {}
