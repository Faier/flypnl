"""Multi-chain token information lookup.

Fetches real pair data from DexScreener (price / FDV / liquidity / momentum /
pair creation time) and attaches a GoPlus security check. Wallet lookups only
resolve what we actually know: an address not in the local database carries no
track record, and none is invented.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

from . import http
from .security import check_token_security

log = logging.getLogger("flygate.radar")


# Public Solana RPC endpoints (no key required), tried in order. Probed
# 2026-10-02 from this host: publicnode rejects getTokenAccountsByOwner with
# 403 outright, so the official endpoint (which supports the full method set,
# rate-limited per IP) goes first; publicnode stays as a fallback for the day
# its policy changes.
SOL_RPC_ENDPOINTS = [
    "https://api.mainnet.solana.com",
    "https://solana-rpc.publicnode.com",
]
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Wrappers/wrapped assets that always sit in a wallet but carry no narrative
KNOWN_MINTS_SKIP = {
    "So11111111111111111111111111111111111111112",          # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",          # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",          # USDT
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",           # JUP (airdrop dust)
}


def fetch_solana_holdings(address: str, max_tokens: int = 30) -> list:
    """What SPL tokens does this wallet actually hold right now?

    Real on-chain data (public RPC getTokenAccountsByOwner), enriched with
    DexScreener market data for the non-wrapped mints. This is a holdings
    SNAPSHOT: buy vs transfer-in cannot be told apart without a full tx
    history, and nothing here measures the wallet's track record — that still
    needs a data source we do not have. Meme tokens under $5k liquidity are
    dropped as untradable dust.
    """
    rpc = None
    last_err = None
    for endpoint in SOL_RPC_ENDPOINTS:
        try:
            rpc = http.post_json(endpoint, {
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [address, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
            }, timeout=12.0)
            break
        except http.HttpError as e:
            last_err = e
            continue
    if rpc is None:
        log.warning("holdings RPC failed for %s: %s", address, last_err)
        return []

    accounts = (rpc or {}).get("result", {}).get("value") or []
    mints = []
    seen = set()
    for acc in accounts:
        info = ((acc.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {})
        mint = info.get("mint")
        amount = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
        if not mint or mint in seen or mint in KNOWN_MINTS_SKIP or amount <= 0:
            continue
        seen.add(mint)
        mints.append(mint)
    if not mints:
        return []

    # batch market data (DexScreener accepts up to 30 comma-separated addrs)
    holdings = []
    for i in range(0, min(len(mints), max_tokens), 30):
        batch = mints[i:i + 30]
        try:
            data = http.get_json(
                "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(batch))
        except http.HttpError:
            continue
        pairs_by_mint = {}
        for p in data.get("pairs") or []:
            m = (p.get("baseToken") or {}).get("address")
            if m and m not in pairs_by_mint:
                pairs_by_mint[m] = p
        for m in batch:
            p = pairs_by_mint.get(m)
            if not p:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq < 5000:                # untradable dust for this purpose
                continue
            pc = p.get("priceChange") or {}
            holdings.append({
                "mint": m,
                "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
                "name": (p.get("baseToken") or {}).get("name", ""),
                "chain": p.get("chainId", "solana"),
                "price_usd": float(p.get("priceUsd") or 0),
                "market_cap": float(p.get("fdv") or 0),
                "liquidity_usd": liq,
                "change_h24": float(pc.get("h24") or 0),
                "dex_id": (p.get("dexId") or "").capitalize(),
                "chart_url": p.get("url") or f"https://dexscreener.com/solana/{m}",
            })
    holdings.sort(key=lambda h: -h["market_cap"])
    return holdings


DEX_CHAIN_MAP = {
    "SOL": "solana",
    "BASE": "base",
    "BSC": "bsc",
    "ARB": "arbitrum",
    "ARC": "arbitrum",
    "ETH": "ethereum",
    "ROBINHOOD": "ethereum",
}

# Reverse map: resolve the real chain from a pair's chainId. Queries can
# arrive with chain="ALL" (dashboard default) — storing "ALL" would break
# watchlist polling and the GoPlus audit (which needs a concrete chain).
CHAIN_BY_DEX = {"solana": "SOL", "base": "BASE", "bsc": "BSC",
                "arbitrum": "ARB", "ethereum": "ETH"}


def query_token_info(chain: str, token_or_pair: str) -> dict:
    """Fetch real-time price, market cap, liquidity, and security rating for a Meme token or pair."""
    chain = chain.upper()
    dex_chain = DEX_CHAIN_MAP.get(chain, chain.lower())
    token_or_pair = token_or_pair.strip()

    # 1. Parse if user passed a DexScreener URL (e.g. https://dexscreener.com/solana/gdjvv2smnxvvbouxkp1puduvhupjbgrvgr9topcc9fkc)
    target_addr = token_or_pair
    if "dexscreener.com" in token_or_pair:
        parts = token_or_pair.rstrip("/").split("/")
        if len(parts) >= 2:
            url_chain = parts[-2].upper()
            if url_chain in ("SOLANA", "SOL"):
                chain, dex_chain = "SOL", "solana"
            elif url_chain in ("BASE",):
                chain, dex_chain = "BASE", "base"
            elif url_chain in ("BSC", "BINANCE"):
                chain, dex_chain = "BSC", "bsc"
            elif url_chain in ("ARBITRUM", "ARB", "ARC"):
                chain, dex_chain = "ARB", "arbitrum"
            elif url_chain in ("ETHEREUM", "ETH"):
                chain, dex_chain = "ETH", "ethereum"
            target_addr = parts[-1]

    # DexScreener EVM endpoints are case-sensitive: normalize 0x addresses.
    if target_addr.startswith("0x") and len(target_addr) == 42:
        target_addr = target_addr.lower()

    info = {
        "chain": chain,
        "token_address": target_addr,
        "symbol": "MEME",
        "name": "Unknown Meme",
        "price_usd": 0.0,
        "price_native": "",
        "market_cap": 0.0,
        "liquidity_usd": 0.0,
        "volume_24h": 0.0,
        "volume_5m": 0.0,
        "volume_1h": 0.0,
        "change_m5": 0.0,
        "change_h1": 0.0,
        "change_h24": 0.0,
        "txns_m5": {"buys": 0, "sells": 0},
        "txns_h1": {"buys": 0, "sells": 0},
        "dex_id": "DEX",
        "pair_address": "",
        "pair_created_at": 0,
        "token_age_text": "",
        "is_pumpfun": False,
        "bonding_curve_pct": 0.0,
        "tactical_assessment": {},
        "chart_url": f"https://dexscreener.com/{dex_chain}/{target_addr}",
        "gmgn_url": f"https://gmgn.ai/{chain.lower()}/token/{target_addr}",
        "security": {}
    }

    try:
        pairs = []
        # Attempt 1: fetch as pair address (needs a concrete chain; "ALL"
        # isn't a valid DexScreener chain so skip straight to token lookup)
        if dex_chain != "all":
            try:
                p_data = http.get_json(
                    f"https://api.dexscreener.com/latest/dex/pairs/{dex_chain}/{target_addr}")
                pairs = p_data.get("pairs") or []
            except http.HttpError:
                pairs = []

        # Attempt 2: fetch by token address if the pair didn't match
        if not pairs:
            try:
                t_data = http.get_json(
                    f"https://api.dexscreener.com/latest/dex/tokens/{target_addr}")
                pairs = t_data.get("pairs") or []
            except http.HttpError:
                pairs = []

        # Attempt 3: search fallback
        if not pairs:
            try:
                q_data = http.get_json(
                    "https://api.dexscreener.com/latest/dex/search?q="
                    + urllib.parse.quote(target_addr))
                pairs = q_data.get("pairs") or []
            except http.HttpError:
                pairs = []

        if pairs:
            matched = [p for p in pairs if p.get("chainId") == dex_chain]
            if matched:
                p = matched[0]
            else:
                # chain-agnostic query: prefer the most liquid pair
                p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))

            # Resolve the real chain from the pair itself (handles chain="ALL")
            resolved = CHAIN_BY_DEX.get((p.get("chainId") or "").lower())
            if resolved:
                chain = resolved
                info["chain"] = chain

            base_token = p.get("baseToken", {})
            real_token_addr = base_token.get("address", target_addr)
            info["symbol"] = base_token.get("symbol", "MEME")
            info["name"] = base_token.get("name", "Unknown Meme")
            info["token_address"] = real_token_addr
            info["price_usd"] = float(p.get("priceUsd", 0.0) or 0.0)
            info["price_native"] = p.get("priceNative", "")
            info["market_cap"] = float(p.get("fdv", 0.0) or p.get("marketCap", 0.0) or 0.0)
            info["liquidity_usd"] = float(p.get("liquidity", {}).get("usd", 0.0) or 0.0)
            
            vol = p.get("volume", {})
            info["volume_24h"] = float(vol.get("h24", 0.0) or 0.0)
            info["volume_1h"] = float(vol.get("h1", 0.0) or 0.0)
            info["volume_5m"] = float(vol.get("m5", 0.0) or 0.0)

            pc = p.get("priceChange", {})
            info["change_m5"] = float(pc.get("m5", 0.0) or 0.0)
            info["change_h1"] = float(pc.get("h1", 0.0) or 0.0)
            info["change_h24"] = float(pc.get("h24", 0.0) or 0.0)

            txns = p.get("txns", {})
            info["txns_m5"] = txns.get("m5", {"buys": 0, "sells": 0})
            info["txns_h1"] = txns.get("h1", {"buys": 0, "sells": 0})

            dex_id_raw = (p.get("dexId") or "DEX").lower()
            info["dex_id"] = "Pump.fun (内盘)" if dex_id_raw in ("pumpfun", "pump") else dex_id_raw.capitalize()
            info["is_pumpfun"] = (dex_id_raw in ("pumpfun", "pump"))
            info["pair_address"] = p.get("pairAddress", "")
            info["chart_url"] = p.get("url") or info["chart_url"]
            info["gmgn_url"] = f"https://gmgn.ai/{chain.lower()}/token/{real_token_addr}"

            # Pump.fun bonding curve progress
            if info["is_pumpfun"]:
                fdv = info["market_cap"]
                # Pumpfun curve starts around $4.5k and migrates at ~$69k
                pct = max(0.0, min(100.0, (fdv - 4500.0) / (69000.0 - 4500.0) * 100.0))
                info["bonding_curve_pct"] = round(pct, 1)

            # Token age (real pair creation time only; no invented fallback)
            pair_created = p.get("pairCreatedAt")
            if pair_created:
                info["pair_created_at"] = pair_created
                age_min = (time.time() - pair_created / 1000.0) / 60.0
                if age_min < 60:
                    info["token_age_text"] = f"上线 {int(age_min)} 分钟"
                elif age_min < 1440:
                    info["token_age_text"] = f"上线 {age_min/60.0:.1f} 小时"
                else:
                    info["token_age_text"] = f"上线 {age_min/1440.0:.1f} 天"
            else:
                info["token_age_text"] = "币龄未知"

            # Tactical Trade Assessment
            mcap = info["market_cap"]
            ch_m5 = info["change_m5"]
            ch_h1 = info["change_h1"]

            tactical = {
                "support_fdv": round(mcap * 0.75, 0),
                "resistance_fdv": round(mcap * 1.5, 0),
                "stop_loss_pct": -25.0,
                "stage": "Pump.fun 内盘冲盘期" if info["is_pumpfun"] else "DEX 外盘交易期",
                "bias": "NEUTRAL",
                "risk_rating": "HIGH",
                "notes": ["支撑/阻力为按当前市值的启发式估算（×0.75 / ×1.5），非链上筹码分析"]
            }

            if info["is_pumpfun"]:
                tactical["target_graduation_fdv"] = 69000.0
                tactical["distance_to_raydium"] = f"{round(69000.0 / max(1.0, mcap), 1)}x (打满毕业)"
                tactical["notes"].append(f"当前处于 Pump.fun 内盘 ({info['bonding_curve_pct']}% 进度) · 需打满 ~$69k FDV 才会迁移至 Raydium")
                tactical["notes"].append("⚠️ 内盘特性: 任何早期大户抛售几枚 SOL 都会造成大幅回撤，务必设好硬止损")

            if ch_m5 < -8.0:
                tactical["bias"] = "BEARISH_PULLBACK"
                tactical["notes"].append(f"短线 5 分钟急跌 {ch_m5:+.1f}% · 获利盘正在了结出逃，切忌盲目逆势加仓")
            elif ch_h1 > 20.0 and ch_m5 >= 0:
                tactical["bias"] = "BULLISH_CONTINUATION"
                tactical["notes"].append("1 小时买盘动量强劲 · 关注突破前高点")

            info["tactical_assessment"] = tactical
    except http.HttpError:
        pass                        # already logged in http.get_json
    except Exception as e:
        log.warning("token info parse failed for %s: %s", target_addr, e)

    # Attach security check
    real_sec_addr = info.get("token_address") or target_addr
    info["security"] = check_token_security(chain, real_sec_addr, info["liquidity_usd"])
    return info


def query_wallet_profile(chain: str, address: str, wallet_db=None) -> dict:
    """Resolve a wallet to what we actually know about it.

    Known only means present in the local database (user-curated). An address
    we have no data source for gets an honest "no track record" profile —
    win-rate / PnL stay None instead of being invented.
    """
    chain = chain.upper()
    address = address.strip()

    # 1. Check if known in our database
    known = wallet_db.get_wallet(address) if wallet_db else None
    if known:
        res = known.to_dict()
        res["source"] = "本地地址收藏池"
    else:
        # 2. Chain detection by address format (display links only)
        detected_chain = chain
        if address.startswith("0x") and len(address) == 42:
            if chain not in ("BASE", "BSC", "ARB", "ARC", "ETH", "ROBINHOOD"):
                detected_chain = "BASE"
        elif 32 <= len(address) <= 44 and not address.startswith("0x"):
            detected_chain = "SOL"

        res = {
            "chain": detected_chain,
            "address": address,
            "alias": f"未收录地址 ({address[:6]}...{address[-4:]})",
            "win_rate": None,
            "pnl_usd": None,
            "style": "",
            "avg_hold_min": None,
            "top_tokens": [],
            "enabled": True,
            "verified": False,
            "source": "未收录（系统未接入钱包级链上成交数据源，暂无战绩数据）",
            "explorer_url": (
                f"https://solscan.io/account/{address}" if detected_chain == "SOL"
                else f"https://basescan.org/address/{address}" if detected_chain == "BASE"
                else f"https://bscscan.com/address/{address}" if detected_chain == "BSC"
                else f"https://arbiscan.io/address/{address}" if detected_chain in ("ARB", "ARC")
                else f"https://etherscan.io/address/{address}"
            ),
            "gmgn_url": f"https://gmgn.ai/{detected_chain.lower()}/address/{address}"
        }

    # Real on-chain holdings snapshot (SOL only: no free EVM source is wired
    # in). This is what the wallet holds NOW — it says nothing about when it
    # bought, at what price, or whether it is actually "smart" money.
    if (res.get("chain") or "").upper() == "SOL":
        try:
            res["holdings"] = fetch_solana_holdings(address)
        except Exception as e:
            log.warning("holdings lookup failed for %s: %s", address, e)
            res["holdings"] = []
    else:
        res["holdings"] = []
        res["holdings_note"] = "链上持仓透视暂只支持 Solana 钱包（EVM 无免费数据源）"

    return res
