"""Multi-chain NEW-token radar — honest scope.

What this module does:
- Scans DexScreener for newly-launched tokens (verified age <= 6h) per chain,
- runs GoPlus security checks on each discovery,
- paper-tracks every pick (1h/6h/24h real returns) so badge quality is measured,
- tracks a user watchlist with live PnL and momentum alerts.

What it explicitly does NOT do:
- It does not track wallet-level on-chain trades. No data source for
  per-wallet swaps is wired in, so no event is attributed to any wallet
  ("smart money bought X" claims cannot be made) and there is no copy-trade
  signal. Every number in an event comes from a DexScreener/GoPlus response
  or is marked unknown (None). Tokens whose launch time cannot be verified,
  and mature tokens, are skipped entirely — this radar only covers new coins.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
from collections import deque
from typing import Dict, List, Optional

from . import http
from .narratives import aggregate_narrative_heat, classify_narrative, narrative_label
from .wallet_db import WalletDB
from .security import check_token_security
from .multi_chain import query_token_info, query_wallet_profile

log = logging.getLogger("flygate.radar")

CHAINS = ["SOL", "BASE", "BSC", "ARB", "ETH"]
DEX_CHAIN = {"SOL": "solana", "BASE": "base", "BSC": "bsc", "ARB": "arbitrum", "ETH": "ethereum"}
GMGN_CHAIN = {"SOL": "sol", "BASE": "base", "BSC": "bsc", "ARB": "arb", "ETH": "eth",
              "ROBINHOOD": "eth"}
SEARCH_TERMS = {
    "SOL": ["pump", "sol", "doge", "cat"],
    "BASE": ["base", "ai", "virtual"],
    "BSC": ["four", "bnb", "baby"],
    "ARB": ["arb", "camelot"],
    "ETH": ["pepe", "shib", "spx"],
}

SCAN_INTERVAL_S = 15.0   # one scan cycle
EMIT_TTL_S = 1800.0      # re-emit the same token at most every 30 minutes
MAX_EVENTS_PER_SCAN = 3  # bounded work per cycle (GoPlus + DexScreener calls)
ALERT_COOLDOWN_S = 600.0 # watchlist alert cooldown per token

# Push gate. Measured on the first 14h of radar_picks.json: tokens below the
# push lines were 74% of all pushes and carried no signal (DUMP_AVOID 1h win
# 22%, median -36%; ULTRA_EARLY_GAMBLE 6h win 8%, median -87%). They are still
# scanned and counted; a softer watch tier (see WATCH_* below) surfaces the
# near-misses on the board, honestly labelled.
# Only a NEW_TOKEN_WATCH decision (audit passed, not dumping, liquidity and
# market cap above the lines) is pushed. Everything else is counted, not shown.
PUSH_ELIGIBLE_STATUSES = {"NEW_TOKEN_WATCH"}
# Watch tier: audit-passed and not dumping, but liquidity/market cap just
# short of the push lines (LOW_LIQUIDITY_RISK / HIGH_RISK_EARLY /
# ULTRA_EARLY_GAMBLE in the cascade). These surface on the board tagged
# 🧪 观察级 — visible during quiet hours, honestly labelled as below-gate.
WATCH_ELIGIBLE_STATUSES = {"LOW_LIQUIDITY_RISK", "HIGH_RISK_EARLY", "ULTRA_EARLY_GAMBLE"}
# Local pre-filter floor for the watch tier: below these the token is not
# worth an audit call even as a watch row (dust pools, nano caps).
WATCH_MIN_LIQ_USD = 10000.0
WATCH_MIN_MCAP_USD = 20000.0

# Junk-name patterns: launchers spam placeholder names (test / broadcast:N /
# single characters). Numeric gates cannot catch these, so they are blocked
# here — conservative rules only, anything real-looking passes to the audit.
_JUNK_SYMBOL_RE = re.compile(r"^(test|token|coin|meme|new|airdrop|xxx)[0-9]*$")


def _is_junk_name(symbol: str, name: str) -> bool:
    joined = re.sub(r"\s+", "", f"{symbol or ''}{name or ''}").lower()
    if len(joined) <= 3:                            # "币", "?", lone letters
        return True
    if _JUNK_SYMBOL_RE.match((symbol or "").strip().lower()):
        return True
    if (name or "").strip().lower().startswith("broadcast:"):
        return True
    if (symbol or "").strip().isdigit():            # symbol is a bare number
        return True
    return False

# Paper tracking of radar picks: every first-time discovery is recorded with
# its entry price, and 1h/6h/24h later the real return is filled in. Grouped
# by decision badge this answers "do the labels mean anything at all" —
# measured, not asserted.
PICK_HORIZONS = (("h1", 3600.0), ("h6", 6 * 3600.0), ("h24", 24 * 3600.0))
EVAL_INTERVAL_S = 60.0   # picks evaluation cycle
EVAL_BUDGET = 8          # price lookups per eval cycle (rate-limit friendliness)
HORIZON_GRACE_S = 24 * 3600.0  # keep retrying an unavailable horizon for 24h past due
MAX_PICKS = 1000


def compute_token_age(pair_created_at_ms: Optional[float],
                      now_sec: Optional[float] = None) -> tuple:
    """Token age from the pair creation timestamp.

    Returns (is_new_token, age_min, age_text, age_tier). When the creation
    time is unknown the age is honestly reported as unknown — never invented.
    """
    if now_sec is None:
        now_sec = time.time()

    created = float(pair_created_at_ms or 0)
    if created <= 0:
        return False, None, "币龄未知", "UNKNOWN"

    age_min = max(0.0, (now_sec * 1000.0 - created) / 60000.0)

    # Within 6 hours (360 minutes) counts as a new token
    is_new = age_min <= 360.0

    if age_min < 60.0:
        age_text = f"刚上线 {max(1, int(age_min))} 分钟"
        age_tier = "JUST_LAUNCHED" if age_min < 30 else "ULTRA_EARLY"
    elif age_min < 180.0:
        h = int(age_min // 60)
        m = int(age_min % 60)
        age_text = f"上线 {h}小时{m}分"
        age_tier = "EARLY"
    elif age_min < 1440.0:
        age_text = f"上线 {age_min / 60.0:.1f} 小时"
        age_tier = "DAY_1"
    else:
        days = int(age_min // 1440)
        age_text = f"上线 {days} 天前"
        age_tier = "MATURE"

    return is_new, round(age_min, 1), age_text, age_tier


def _norm_chain(chain: Optional[str]) -> Optional[str]:
    c = (chain or "").upper()
    if c in ("ARC", "ARBITRUM"):
        return "ARB"
    return c or None


class RadarTracker:
    def __init__(self, runs_dir: str):
        self.runs_dir = runs_dir
        self.wallet_db = WalletDB(runs_dir)
        self.events: deque = deque(maxlen=300)
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._chain_idx = 0
        self._seen: Dict[str, float] = {}        # token address -> last emit ts
        self._alert_ts: Dict[str, float] = {}    # watchlist addr -> last alert ts

        # Push-gate counters (in-memory, since process start): what the scanner
        # saw vs. what it pushed, by rejection reason. The board answers
        # "why is it so quiet" without un-filtering the feed.
        self.gate_stats = {
            "since_ts": time.time(),
            "passed": 0,
            "watch": 0,
            "blocked": {"liquidity": 0, "market_cap": 0, "dump": 0,
                        "security": 0, "junk_name": 0},
        }

        # Custom user-watched tokens / pairs
        self.watchlist_file = os.path.join(runs_dir, "watchlist.json")
        self.watchlist: Dict[str, dict] = {}
        self._load_watchlist()
        if not self.watchlist:
            # Seed the user's tracked position (real market data is fetched for it)
            self.add_to_watchlist(
                chain="SOL",
                target="https://dexscreener.com/solana/gdjvv2smnxvvbouxkp1puduvhupjbgrvgr9topcc9fkc",
                note="用户买入持仓 · $NOXBANK"
            )

        # Paper tracking of every first-time discovery (see PICK_HORIZONS)
        self.picks_file = os.path.join(runs_dir, "radar_picks.json")
        self.picks: Dict[str, dict] = {}
        self._load_picks()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="radar-tracker")
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _monitor_loop(self):
        """Background scanner loop: one chain per cycle + watchlist polling."""
        print("[radar] new-token scanner started "
              "(DexScreener new launches ≤6h + GoPlus security; no wallet tracking)")
        next_eval = 0.0
        while not self._stop.wait(SCAN_INTERVAL_S):
            try:
                chain = CHAINS[self._chain_idx % len(CHAINS)]
                self._chain_idx += 1
                self._scan_chain(chain)
            except Exception as e:
                log.warning("chain scan failed: %s", e)
            try:
                self._poll_watchlist()
            except Exception as e:
                log.warning("watchlist poll failed: %s", e)
            now = time.time()
            if now >= next_eval:
                next_eval = now + EVAL_INTERVAL_S
                try:
                    self._eval_picks(now=now)
                except Exception as e:
                    log.warning("picks evaluation failed: %s", e)

    # ------------------------------------------------------------ scanning
    def _fetch_candidate_pairs(self, chain: str) -> List[dict]:
        """Fetch newly-launched / trending pairs for a chain from DexScreener."""
        chain_key = DEX_CHAIN[chain]
        pairs: List[dict] = []

        # New-launch feed first (token profiles), trending search as fallback.
        # Two profiles per cycle: one random pick saw ~9 gated candidates/hour,
        # far below what the feed actually lists — the sampler was the bottleneck.
        try:
            profiles = http.get_json(
                "https://api.dexscreener.com/token-profiles/latest/v1") or []
            matched = [pr.get("tokenAddress") for pr in profiles
                       if pr.get("chainId") == chain_key and pr.get("tokenAddress")]
            for addr in random.sample(matched[:8], min(2, len(matched[:8]))):
                try:
                    data = http.get_json(
                        f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
                    pairs.extend(data.get("pairs") or [])
                except http.HttpError:
                    continue
        except http.HttpError:
            pass

        # profiles often resolve to mature tokens — fall back to trending
        # search whenever nothing <=6h came back, not only on a hard empty list
        now = time.time()
        has_new = any(compute_token_age(p.get("pairCreatedAt"), now_sec=now)[0]
                      for p in pairs)
        if not has_new:
            term = random.choice(SEARCH_TERMS.get(chain, ["meme"]))
            try:
                data = http.get_json(
                    "https://api.dexscreener.com/latest/dex/search?q="
                    + urllib.parse.quote(term))
                pairs = data.get("pairs") or []
            except http.HttpError:
                return []

        return [p for p in pairs if p.get("chainId") == chain_key]

    def _scan_chain(self, chain: str):
        """Emit discovery events for NEW tokens worth looking at.

        新币雷达: only tokens with a verified launch age <= 6h are considered —
        mature tokens and unverifiable-age tokens are skipped before any
        security call is spent on them. Of those, only tokens that clear the
        push gate (GATE_MIN_* + GoPlus audit + not dumping) are emitted; the
        rest are counted in gate_stats, never shown.
        """
        pairs = self._fetch_candidate_pairs(chain)
        now = time.time()
        audited = 0
        for p in pairs[:25]:
            if audited >= MAX_EVENTS_PER_SCAN:
                break
            is_new, _age_min, _text, _tier = compute_token_age(
                p.get("pairCreatedAt"), now_sec=now)
            if not is_new:
                continue
            addr = (p.get("baseToken") or {}).get("address", "")
            price_usd = float(p.get("priceUsd", 0.0) or 0.0)
            if not addr or price_usd <= 0:
                continue
            if now - self._seen.get(addr, 0.0) < EMIT_TTL_S:
                continue

            # junk placeholder names (test / broadcast:N / lone characters):
            # blocked before anything else is spent on them
            base_token = p.get("baseToken") or {}
            if _is_junk_name(base_token.get("symbol", ""), base_token.get("name", "")):
                self._gate_block(addr, "junk_name", now)
                continue

            # Cheap local gate first — no audit call is spent on a token below
            # even the watch floor (0 = data missing; a missing value does not
            # block, same semantics as the cascade).
            liq = float((p.get("liquidity") or {}).get("usd", 0.0) or 0.0)
            mcap = float(p.get("fdv", 0.0) or p.get("marketCap", 0.0) or 0.0)
            if 0 < liq < WATCH_MIN_LIQ_USD:
                self._gate_block(addr, "liquidity", now)
                continue
            if 0 < mcap < WATCH_MIN_MCAP_USD:
                self._gate_block(addr, "market_cap", now)
                continue

            ev = self._build_token_event(chain, p, now)
            audited += 1
            if ev is None:
                continue
            status = ev.get("decision", {}).get("status")
            if status in PUSH_ELIGIBLE_STATUSES:
                ev["push_tier"] = "push"
                self._seen[addr] = now
                with self.lock:
                    self.events.appendleft(ev)
                self._record_pick(ev)
                self.gate_stats["passed"] += 1
            elif status in WATCH_ELIGIBLE_STATUSES:
                # audited clean and not dumping, just short on liquidity/mcap:
                # shown on the board, honestly labelled as below-gate
                ev["push_tier"] = "watch"
                ev["decision"]["badge"] = "🧪 观察级 · " + ev["decision"]["badge"]
                self._seen[addr] = now
                with self.lock:
                    self.events.appendleft(ev)
                self._record_pick(ev)
                self.gate_stats["watch"] += 1
            else:
                self._gate_block(addr, "dump" if status == "DUMP_AVOID" else "security",
                                 now)

    def _gate_block(self, addr: str, reason: str, now: float):
        """Count one gate rejection and park the token for the emit TTL, so the
        same reject is not re-evaluated (and re-audited) on every 15s cycle."""
        self.gate_stats["blocked"][reason] = self.gate_stats["blocked"].get(reason, 0) + 1
        self._seen[addr] = now

    def _build_token_event(self, chain: str, p: dict, now: float) -> Optional[dict]:
        """Build one discovery event from a real DexScreener pair object.

        Every field is either taken from the pair/security response or set to
        None — nothing is randomized or inferred.
        """
        base_token = p.get("baseToken", {})
        token_addr = base_token.get("address", "")
        symbol = base_token.get("symbol", "MEME")
        name = base_token.get("name", "Meme Coin")
        price_usd = float(p.get("priceUsd", 0.0) or 0.0)
        mcap = float(p.get("fdv", 0.0) or p.get("marketCap", 0.0) or 0.0)
        liq = float(p.get("liquidity", {}).get("usd", 0.0) or 0.0)
        dex = (p.get("dexId") or "DEX").capitalize()

        price_change = p.get("priceChange", {})
        ch_m5 = float(price_change.get("m5", 0.0) or 0.0)
        ch_h1 = float(price_change.get("h1", 0.0) or 0.0)
        ch_h24 = float(price_change.get("h24", 0.0) or 0.0)

        m5_txns = p.get("txns", {}).get("m5", {})
        m5_buys = int(m5_txns.get("buys", 0) or 0)
        m5_sells = int(m5_txns.get("sells", 0) or 0)
        m5_vol = float(p.get("volume", {}).get("m5", 0.0) or 0.0)
        total_tx = m5_buys + m5_sells
        # Real average trade size; None when the 5m window has no trades
        avg_trade_usd = round(m5_vol / total_tx, 1) if (total_tx > 0 and m5_vol > 0) else None

        is_new, age_min, age_text, age_tier = compute_token_age(
            p.get("pairCreatedAt"), now_sec=now)

        # Narrative (题材) classification — keyword heuristic over name/symbol;
        # a miss just means independent, it is never treated as bearish
        narrative_id = classify_narrative(name, symbol)
        narrative = {"id": narrative_id, "label": narrative_label(narrative_id)}

        # Trend / liquidity / dump inspection — all from real metrics
        is_dumping = False
        trend_warning = ""
        if ch_m5 < -6.0 or ch_h1 < -12.0:
            is_dumping = True
            trend_warning = f"5分钟跳水 {ch_m5:+.1f}% / 1小时 {ch_h1:+.1f}% · 短线极速破位"
        elif 0 < liq < 20000.0:
            is_dumping = True
            trend_warning = f"底池仅 ${liq/1000:.1f}k 流动性 (<$20k) · 纸级池子一砸就穿"
        elif 0 < mcap < 25000:
            is_dumping = True
            trend_warning = f"市值仅 ${mcap/1000:.1f}k · 谨防归零"
        elif ch_h24 < -40.0:
            is_dumping = True
            trend_warning = f"24H暴跌 {ch_h24:.1f}% · 巨鲸砸盘出逃"
        elif m5_sells > m5_buys * 1.5 and m5_sells >= 6:
            is_dumping = True
            trend_warning = f"短线抛压失衡 (5分卖{m5_sells}/买{m5_buys}) · 获利盘砸盘"

        sec = check_token_security(chain, token_addr, pool_liquidity=liq)

        # Decision cascade (heuristic risk labels, not investment advice)
        if is_dumping:
            action = "WATCH"
            decision_badge = "⛔ 破位跳水 · 严禁接盘"
            decision_status = "DUMP_AVOID"
            decision_reason = trend_warning or "K线瀑布下行，严禁抄底"
        elif not sec["checked"]:
            action = "WATCH"
            decision_badge = "⏳ 安全审计不可用 · 暂缓评估"
            decision_status = "SECURITY_UNKNOWN"
            decision_reason = "GoPlus 数据源未能完成核验，安全性未知，先当不可信处理"
        elif not sec["is_safe"]:
            action = "WATCH"
            decision_badge = "⛔ 高危合约 · 建议回避"
            decision_status = "UNSAFE_RUG"
            decision_reason = "GoPlus 安全审计未通过：" + "；".join(sec["warnings"][:2])
        elif 0 < liq < 30000.0:
            action = "WATCH"
            decision_badge = f"⚠️ 纸级流动性 (${liq/1000:.1f}k) · 极易闪崩"
            decision_status = "LOW_LIQUIDITY_RISK"
            decision_reason = f"底池仅 ${liq/1000:.1f}k 流动性 (<$30k安全底线) · 经不起任何大单抛售"
        elif is_new and age_min is not None and age_min < 30 and mcap < 50000:
            action = "SCAN"
            decision_badge = "🔥 极早期次新 · 谨防内盘流产"
            decision_status = "ULTRA_EARLY_GAMBLE"
            decision_reason = f"{age_text} · 极早期博弈，随时可能归零"
        elif 0 < mcap < 45000:
            action = "SCAN"
            decision_badge = "⚠️ 超低市值 · 博彩级波动"
            decision_status = "HIGH_RISK_EARLY"
            decision_reason = f"市值仅 ${mcap/1000:.1f}k · 归零风险极高"
        elif is_new:
            action = "SCAN"
            decision_badge = "👀 新上榜新币 · 适度关注"
            decision_status = "NEW_TOKEN_WATCH"
            nar_txt = f" · {narrative['label']}题材" if narrative_id else ""
            decision_reason = f"{age_text} · 已通过安全审计{nar_txt}，观察后续动量"
        else:
            action = "SCAN"
            decision_badge = "🚀 热门上榜 · 适量关注"
            decision_status = "BULLISH_FOLLOW"
            decision_reason = f"热门扫描命中 · 1H {ch_h1:+.1f}% · 底池 ${liq/1000:.1f}k"

        gmgn_slug = GMGN_CHAIN.get(chain, chain.lower())
        source = "new_launch" if is_new else "trending"

        ev = {
            "id": f"radar_{int(now)}_{random.randint(1000, 9999)}",
            "ts": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "chain": chain,
            "action": action,
            "source": source,
            "is_new_token": is_new,
            "token_age_text": age_text,
            "token": {
                "symbol": symbol,
                "name": name,
                "address": token_addr,
                "price_usd": price_usd,
                "market_cap": mcap,
                "liquidity_usd": liq,
                "volume_5m": m5_vol,
                "change_m5": ch_m5,
                "change_h1": ch_h1,
                "change_h24": ch_h24,
                "is_dumping": is_dumping,
                "is_new_token": is_new,
                "token_age_min": age_min,
                "token_age_text": age_text,
                "token_age_tier": age_tier,
            },
            "avg_trade_usd": avg_trade_usd,
            "dex": dex,
            "narrative": narrative,
            "security": sec,
            "decision": {
                "badge": decision_badge,
                "status": decision_status,
                "reason": decision_reason,
            },
            "chart_url": p.get("url") or f"https://dexscreener.com/{DEX_CHAIN[chain]}/{token_addr}",
            "gmgn_url": f"https://gmgn.ai/{gmgn_slug}/token/{token_addr}",
        }

        # Refresh any earlier events of the same token with the live metrics
        with self.lock:
            for existing_ev in self.events:
                if existing_ev.get("token", {}).get("address") == token_addr:
                    existing_ev["token"]["price_usd"] = price_usd
                    existing_ev["token"]["change_m5"] = ch_m5
                    existing_ev["token"]["change_h1"] = ch_h1
                    existing_ev["token"]["change_h24"] = ch_h24
                    existing_ev["token"]["liquidity_usd"] = liq
                    if is_dumping:
                        existing_ev["token"]["is_dumping"] = True
                        existing_ev["action"] = "WATCH"
                        existing_ev["decision"]["badge"] = "⛔ 破位跳水 · 严禁接盘"
                        existing_ev["decision"]["status"] = "DUMP_AVOID"
                        existing_ev["decision"]["reason"] = trend_warning or "短线极速跳水破位"

        return ev

    # ------------------------------------------------------------ watchlist
    def _load_watchlist(self):
        try:
            if os.path.exists(self.watchlist_file):
                with open(self.watchlist_file, "r", encoding="utf-8") as f:
                    self.watchlist = json.load(f)
        except Exception as e:
            log.warning("watchlist load failed: %s", e)
            self.watchlist = {}

    def _save_watchlist(self):
        """Caller must NOT hold self.lock (atomic-ish write of live state)."""
        try:
            with open(self.watchlist_file, "w", encoding="utf-8") as f:
                json.dump(self.watchlist, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("watchlist save failed: %s", e)

    def add_to_watchlist(self, chain: str, target: str, entry_price: float = 0.0, note: str = "") -> dict:
        info = query_token_info(chain, target)
        addr = info.get("token_address") or target
        with self.lock:
            item = {
                "chain": info.get("chain", chain),
                "target": target,
                "token_address": addr,
                "pair_address": info.get("pair_address", ""),
                "symbol": info.get("symbol", "$MEME"),
                "name": info.get("name", "Meme"),
                "price_usd": info.get("price_usd", 0.0),
                "entry_price": entry_price if entry_price > 0 else info.get("price_usd", 0.0),
                "market_cap": info.get("market_cap", 0.0),
                "liquidity_usd": info.get("liquidity_usd", 0.0),
                "bonding_curve_pct": info.get("bonding_curve_pct", 0.0),
                "change_m5": info.get("change_m5", 0.0),
                "change_h1": info.get("change_h1", 0.0),
                "dex_id": info.get("dex_id", "DEX"),
                "chart_url": info.get("chart_url", target),
                "gmgn_url": info.get("gmgn_url", ""),
                "security": info.get("security", {}),
                "tactical_assessment": info.get("tactical_assessment", {}),
                "note": note,
                "updated_at": time.time(),
                "created_at": time.time()
            }
            self.watchlist[addr] = item
        self._save_watchlist()
        return item

    def get_watchlist(self) -> List[dict]:
        with self.lock:
            return list(self.watchlist.values())

    def remove_from_watchlist(self, target_addr: str) -> bool:
        with self.lock:
            found = False
            for k in list(self.watchlist.keys()):
                if k == target_addr or self.watchlist[k].get("pair_address") == target_addr or target_addr in self.watchlist[k].get("target", ""):
                    del self.watchlist[k]
                    found = True
        if found:
            self._save_watchlist()
        return found

    def _poll_watchlist(self):
        """Poll watched tokens (real market data), update live PnL, emit alerts."""
        with self.lock:
            items = list(self.watchlist.values())
        if not items:
            return

        dirty = False
        for it in items:
            try:
                target = it.get("target") or it.get("token_address")
                chain = it.get("chain", "SOL")
                info = query_token_info(chain, target)
                if not info or info.get("price_usd", 0.0) <= 0:
                    continue

                curr_price = info.get("price_usd", 0.0)
                entry_price = it.get("entry_price") or curr_price
                pnl_pct = ((curr_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

                addr = it.get("token_address")
                with self.lock:
                    if addr in self.watchlist:
                        self.watchlist[addr].update({
                            "price_usd": curr_price,
                            "market_cap": info.get("market_cap", 0.0),
                            "liquidity_usd": info.get("liquidity_usd", 0.0),
                            "bonding_curve_pct": info.get("bonding_curve_pct", 0.0),
                            "change_m5": info.get("change_m5", 0.0),
                            "change_h1": info.get("change_h1", 0.0),
                            "change_h24": info.get("change_h24", 0.0),
                            "volume_5m": info.get("volume_5m", 0.0),
                            "pnl_pct": round(pnl_pct, 2),
                            "updated_at": time.time(),
                            "tactical_assessment": info.get("tactical_assessment", {}),
                        })
                dirty = True

                # Momentum alerts on real 5m moves, rate-limited per token
                ch_m5 = info.get("change_m5", 0.0)
                now = time.time()
                if ch_m5 < -8.0 or ch_m5 > 10.0:
                    if now - self._alert_ts.get(addr, 0.0) < ALERT_COOLDOWN_S:
                        continue
                    self._alert_ts[addr] = now
                    if ch_m5 < -8.0:
                        alert_badge = f"⚠️ 盯盘异动 · 5M急速回撤 {ch_m5:.1f}%"
                        alert_status = "WATCH_PULLBACK"
                        alert_reason = f"自选持仓盯盘 · 短线获利盘抛售 (当前FDV: ${info['market_cap']/1000:.1f}k)"
                    else:
                        alert_badge = f"🚀 盯盘异动 · 5M放量冲高 +{ch_m5:.1f}%"
                        alert_status = "WATCH_BREAKOUT"
                        alert_reason = f"自选持仓盯盘 · 动量上攻 (当前FDV: ${info['market_cap']/1000:.1f}k)"

                    ev = {
                        "id": f"watch_{int(now)}_{random.randint(1000, 9999)}",
                        "ts": now,
                        "time": time.strftime("%H:%M:%S", time.localtime(now)),
                        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                        "chain": chain,
                        "action": "ALERT",
                        "source": "watchlist",
                        "pnl_pct": round(pnl_pct, 2),
                        "is_new_token": info.get("token_age_min") is not None and info.get("token_age_min", 1e9) <= 360,
                        "token_age_text": info.get("token_age_text", "币龄未知"),
                        "token": {
                            "symbol": info.get("symbol", "$MEME"),
                            "name": info.get("name", "Meme"),
                            "address": info.get("token_address", ""),
                            "price_usd": curr_price,
                            "market_cap": info.get("market_cap", 0.0),
                            "liquidity_usd": info.get("liquidity_usd", 0.0),
                            "volume_5m": info.get("volume_5m", 0.0),
                            "change_m5": ch_m5,
                            "change_h1": info.get("change_h1", 0.0),
                            "change_h24": info.get("change_h24", 0.0),
                            "is_dumping": (ch_m5 < -8.0),
                            "is_new_token": False,
                            "token_age_min": None,
                            "token_age_text": info.get("token_age_text", "币龄未知"),
                            "token_age_tier": "UNKNOWN",
                        },
                        "avg_trade_usd": None,
                        "dex": info.get("dex_id", "DEX"),
                        "security": info.get("security", {}),
                        "decision": {
                            "badge": alert_badge,
                            "status": alert_status,
                            "reason": alert_reason,
                        },
                        "chart_url": info.get("chart_url", ""),
                        "gmgn_url": info.get("gmgn_url", ""),
                    }
                    with self.lock:
                        self.events.appendleft(ev)
            except Exception as e:
                log.warning("watchlist poll failed for %s: %s", it.get("token_address"), e)
        if dirty:
            self._save_watchlist()

    # ------------------------------------------------------------ pick tracking
    def _load_picks(self):
        try:
            if os.path.exists(self.picks_file):
                with open(self.picks_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.picks = data
        except Exception as e:
            log.warning("picks load failed: %s", e)
            self.picks = {}

    def _save_picks(self):
        """Caller must NOT hold self.lock."""
        try:
            with open(self.picks_file, "w", encoding="utf-8") as f:
                json.dump(self.picks, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("picks save failed: %s", e)

    def _record_pick(self, ev: dict):
        """First time a token surfaces in a scan, start paper-tracking it.

        Watchlist alerts are the user's own positions, not radar picks —
        they are never recorded. Re-sightings only bump the hit counter.
        """
        if ev.get("source") not in ("trending", "new_launch"):
            return
        tok = ev.get("token", {})
        addr = tok.get("address")
        if not addr:
            return
        key = f"{ev['chain']}:{addr}"
        now = ev.get("ts", time.time())
        with self.lock:
            if key in self.picks:
                self.picks[key]["scan_hits"] = (self.picks[key].get("scan_hits") or 0) + 1
                return
            self.picks[key] = {
                "key": key,
                "chain": ev["chain"],
                "address": addr,
                "symbol": tok.get("symbol", "?"),
                "name": tok.get("name", ""),
                "status": ev.get("decision", {}).get("status", "?"),
                "badge": ev.get("decision", {}).get("badge", ""),
                "narrative": (ev.get("narrative") or {}).get("id"),
                # snapshot of the push moment: without it we can never answer
                # "what did the winners look like when we pushed them"
                "snapshot": {
                    "mcap": tok.get("market_cap"),
                    "liq": tok.get("liquidity_usd"),
                    "ch_m5": tok.get("change_m5"),
                    "ch_h1": tok.get("change_h1"),
                    "ch_h24": tok.get("change_h24"),
                    "vol_5m": tok.get("volume_5m"),
                    "age_min": tok.get("token_age_min"),
                    "avg_trade_usd": ev.get("avg_trade_usd"),
                },
                "ts": now,
                "time": ev.get("time", ""),
                "entry_price_usd": tok.get("price_usd", 0.0),
                "last_price_usd": tok.get("price_usd", 0.0),
                "horizons": {h: None for h, _ in PICK_HORIZONS},
                "due": {h: now + dur for h, dur in PICK_HORIZONS},
                "scan_hits": 1,
            }
            self._prune_picks_locked()
        self._save_picks()

    def _prune_picks_locked(self):
        if len(self.picks) <= MAX_PICKS:
            return
        now = time.time()
        # drop oldest finished picks first, then oldest anything
        finished = [k for k, p in self.picks.items()
                    if not any(p["horizons"][h] is None and now < p["due"][h] + HORIZON_GRACE_S
                               for h, _ in PICK_HORIZONS)]
        ordered = sorted(self.picks, key=lambda k: self.picks[k]["ts"])
        for k in ordered:
            if len(self.picks) <= MAX_PICKS:
                break
            if not finished or k in finished:
                del self.picks[k]

    def _horizon_pending(self, p: dict, h: str, now: float) -> bool:
        return p["horizons"][h] is None and now < p["due"][h] + HORIZON_GRACE_S

    def _eval_picks(self, now: Optional[float] = None):
        """Fill due horizons with real prices (bounded lookups per cycle)."""
        if now is None:
            now = time.time()
        with self.lock:
            pending = [p for p in self.picks.values()
                       if any(self._horizon_pending(p, h, now) for h, _ in PICK_HORIZONS)]
        if not pending:
            return

        looked_up = 0
        updated = False
        for p in pending:
            if looked_up >= EVAL_BUDGET:
                break
            due = [h for h, _ in PICK_HORIZONS
                   if p["horizons"][h] is None and now >= p["due"][h]]
            if not due:
                continue
            try:
                info = query_token_info(p["chain"], p["address"])
            except Exception as e:
                log.warning("pick price lookup failed for %s: %s", p["key"], e)
                continue
            price = float(info.get("price_usd", 0.0) or 0.0)
            looked_up += 1
            if price <= 0:
                continue                       # unavailable; retried until grace expires
            entry = float(p.get("entry_price_usd") or 0.0)
            p["last_price_usd"] = price
            if entry > 0:
                for h in due:
                    p["horizons"][h] = round((price / entry - 1.0) * 100.0, 2)
            updated = True

        if updated:
            self._save_picks()

    def get_picks_summary(self) -> dict:
        """Per-badge win rates / average returns over 1h/6h/24h horizons."""
        now = time.time()
        with self.lock:
            picks_snapshot = list(self.picks.values())

        def agg(vals):
            if not vals:
                return {"n": 0, "win_rate": None, "survival_rate": None,
                        "avg_pct": None, "median_pct": None,
                        "best_pct": None, "worst_pct": None}
            s = sorted(vals)
            return {
                "n": len(vals),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
                # survival: still above -20% at the horizon — the gate promised
                # quality, this measures whether it delivered any
                "survival_rate": round(sum(1 for v in vals if v > -20.0) / len(vals), 3),
                "avg_pct": round(sum(vals) / len(vals), 2),
                "median_pct": round(s[len(s) // 2], 2),
                "best_pct": s[-1],
                "worst_pct": s[0],
            }

        groups: Dict[str, dict] = {}
        all_by = {h: [] for h, _ in PICK_HORIZONS}
        open_count = 0
        for p in picks_snapshot:
            is_open = any(self._horizon_pending(p, h, now) for h, _ in PICK_HORIZONS)
            if is_open:
                open_count += 1
            g = groups.setdefault(p.get("status", "?"), {
                "status": p.get("status", "?"),
                "badge": p.get("badge", ""),
                "n": 0,
                "by": {h: [] for h, _ in PICK_HORIZONS},
            })
            g["n"] += 1
            for h, _ in PICK_HORIZONS:
                v = (p.get("horizons") or {}).get(h)
                if v is not None:
                    g["by"][h].append(v)
                    all_by[h].append(v)

        for g in groups.values():
            g["by"] = {h: agg(vals) for h, vals in g["by"].items()}

        picks_list = sorted(picks_snapshot, key=lambda p: p.get("ts", 0), reverse=True)
        return {
            "total": len(self.picks),
            "open": open_count,
            "since_ts": min((p.get("ts", 0) for p in self.picks.values()), default=None),
            "all": {h: agg(vals) for h, vals in all_by.items()},
            "groups": sorted(groups.values(), key=lambda g: -g["n"]),
            "picks": picks_list[:200],
        }

    # ------------------------------------------------------------ queries
    def get_narrative_stats(self, window_h: float = 24.0) -> List[dict]:
        """Rolling narrative heat: real h1/h6 fills of paper-tracked picks
        (inside the window) plus live h1 momentum of coins currently on the
        board. Computed on read — no extra state to go stale."""
        now = time.time()
        with self.lock:
            picks = [dict(p) for p in self.picks.values()
                     if now - p.get("ts", 0.0) <= window_h * 3600.0]
            evs = [e for e in self.events
                   if e.get("source") in ("trending", "new_launch")]
        return aggregate_narrative_heat(picks, evs)

    def get_gate_stats(self) -> dict:
        """Push-gate counters for the dashboard: what was scanned vs. pushed."""
        with self.lock:
            return {
                "since_ts": self.gate_stats["since_ts"],
                "passed": self.gate_stats["passed"],
                "watch": self.gate_stats.get("watch", 0),
                "blocked_total": sum(self.gate_stats["blocked"].values()),
                "blocked": dict(self.gate_stats["blocked"]),
            }

    def get_feed(self, chain: Optional[str] = None, limit: int = 50,
                 new_only: bool = False) -> List[dict]:
        with self.lock:
            all_evs = list(self.events)

        target = _norm_chain(chain)
        if target and target != "ALL":
            if target == "ETH":
                all_evs = [e for e in all_evs if e["chain"] in ("ETH", "ROBINHOOD")]
            else:
                all_evs = [e for e in all_evs if e["chain"] == target]

        if new_only:
            all_evs = [e for e in all_evs if e.get("is_new_token") is True]

        all_evs.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return all_evs[:limit]

    def get_new_tokens_summary(self, chain: Optional[str] = None) -> List[dict]:
        """Aggregate scanned new tokens (age verified <= 6h) into a board.

        Grouping is by scan hits — how often the token surfaced in trending /
        new-launch scans — not by wallet buys (we have no wallet trade data).
        """
        with self.lock:
            all_evs = list(self.events)

        target = _norm_chain(chain)
        if target and target != "ALL":
            if target == "ETH":
                all_evs = [e for e in all_evs if e["chain"] in ("ETH", "ROBINHOOD")]
            else:
                all_evs = [e for e in all_evs if e["chain"] == target]

        # Board rows come from scanner pushes only. Watchlist (user's own
        # positions) and alert events carry is_new_token too, but they never
        # passed the push gate — mixing them in would re-open the flood the
        # gate is there to close.
        new_hits = [e for e in all_evs
                    if e.get("is_new_token") is True
                    and e.get("source") in ("trending", "new_launch")]

        tokens_map: Dict[str, dict] = {}
        for ev in new_hits:
            t = ev.get("token", {})
            addr = t.get("address")
            if not addr:
                continue

            if addr not in tokens_map:
                tokens_map[addr] = {
                    "chain": ev["chain"],
                    "symbol": t.get("symbol", "$MEME"),
                    "name": t.get("name", "Meme"),
                    "address": addr,
                    "push_tier": ev.get("push_tier", "push"),
                    "narrative": ev.get("narrative") or {"id": None, "label": None},
                    "price_usd": t.get("price_usd", 0.0),
                    "market_cap": t.get("market_cap", 0.0),
                    "liquidity_usd": t.get("liquidity_usd", 0.0),
                    "change_m5": t.get("change_m5", 0.0),
                    "change_h1": t.get("change_h1", 0.0),
                    "change_h24": t.get("change_h24", 0.0),
                    "is_dumping": t.get("is_dumping", False),
                    "is_new_token": True,
                    "token_age_min": t.get("token_age_min"),
                    "token_age_text": ev.get("token_age_text") or t.get("token_age_text", "币龄未知"),
                    "token_age_tier": t.get("token_age_tier", "EARLY"),
                    "dex": ev.get("dex", "DEX"),
                    "security": ev.get("security", {}),
                    "decision": ev.get("decision", {}),
                    "chart_url": ev.get("chart_url", ""),
                    "gmgn_url": ev.get("gmgn_url", ""),
                    "first_seen_ts": ev.get("ts", 0),
                    "first_seen_time": ev.get("time", "--"),
                    "latest_seen_ts": ev.get("ts", 0),
                    "latest_seen_time": ev.get("time", "--"),
                    "scan_hits": 0,
                }

            entry = tokens_map[addr]
            entry["scan_hits"] += 1
            # keep freshest market data
            if t.get("price_usd"):
                entry["price_usd"] = t["price_usd"]
                entry["market_cap"] = t.get("market_cap", entry["market_cap"])
                entry["liquidity_usd"] = t.get("liquidity_usd", entry["liquidity_usd"])
                entry["change_m5"] = t.get("change_m5", entry["change_m5"])
                entry["change_h1"] = t.get("change_h1", entry["change_h1"])
            if t.get("is_dumping"):
                entry["is_dumping"] = True
            if ev.get("ts", 0) > entry["latest_seen_ts"]:
                entry["latest_seen_ts"] = ev.get("ts", 0)
                entry["latest_seen_time"] = ev.get("time", "--")

        res = []
        heat_by_id = {n["id"]: n for n in self.get_narrative_stats() if n.get("id")}
        for v in tokens_map.values():
            sec = v.get("security", {})
            dec = v.get("decision", {})
            v_liq = float(v.get("liquidity_usd", 0.0) or 0.0)
            mcap = float(v.get("market_cap", 0.0) or 0.0)

            # attach the narrative's rolling heat to the row (evidence-backed
            # tier from paper-tracked h1 fills; never a mood guess)
            nar_id = (v.get("narrative") or {}).get("id")
            h = heat_by_id.get(nar_id)
            if h:
                v["narrative_tier"] = h["tier"]
                v["narrative_tier_emoji"] = h["tier_emoji"]
                v["narrative_h1_median"] = h["h1_median"]

            # Honest attention labels: only what the data supports
            if v.get("push_tier") == "watch":
                v["attention_badge"] = "🧪 观察级（未达推送门槛）"
            elif v.get("is_dumping") or dec.get("status") == "DUMP_AVOID":
                v["attention_badge"] = "⛔ 破位跳水"
            elif not sec.get("checked"):
                v["attention_badge"] = "⏳ 未完成安全审计"
            elif sec.get("is_safe") is False:
                v["attention_badge"] = "⛔ 安全高危"
            elif 0 < v_liq < 30000.0:
                v["attention_badge"] = f"⚠️ 纸级流动性 (${v_liq/1000:.1f}k)"
            elif 0 < mcap < 45000:
                v["attention_badge"] = f"🎰 超低市值 (${mcap/1000:.1f}k)"
            elif v["scan_hits"] >= 3:
                v["attention_badge"] = f"🔥 高频命中扫描 ({v['scan_hits']}次)"
            else:
                v["attention_badge"] = "👀 新上榜"
            res.append(v)

        # Sort by scan hits desc, then latest sighting desc
        res.sort(key=lambda x: (x["scan_hits"], x["latest_seen_ts"]), reverse=True)
        return res

    def query(self, chain: str, query_str: str) -> dict:
        """Unified query: token lookup via DexScreener, or local wallet profile."""
        query_str = query_str.strip()
        chain = _norm_chain(chain) or "SOL"

        if self.wallet_db.get_wallet(query_str):
            return {
                "type": "wallet",
                "data": query_wallet_profile(chain, query_str, self.wallet_db)
            }

        t_info = query_token_info(chain, query_str)
        if t_info["price_usd"] > 0:
            return {
                "type": "token",
                "data": t_info
            }

        return {
            "type": "wallet",
            "data": query_wallet_profile(chain, query_str, self.wallet_db)
        }
