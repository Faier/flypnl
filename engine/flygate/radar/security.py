"""Token security and anti-rug checking engine.

Queries the GoPlus Security API to verify:
1. Honeypot check: can it be sold? Is buy/sell tax excessive?
2. Authority check: mint authority revoked? freeze authority revoked (Solana)?
3. Liquidity heuristics from the pool size passed by the caller.

Fail-closed: if the GoPlus query cannot be completed (network error, token not
indexed), the result is ``risk_level="UNKNOWN"`` with ``is_safe=False`` — a
token is never labeled safe by absence of data.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Any

from . import http

log = logging.getLogger("flygate.radar")

_SECURITY_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 300          # 5 minutes per token
CACHE_MAX = 512          # bounded: the scanner sees thousands of tokens a day

CHAIN_ID_MAP = {
    "ETH": "1",
    "ROBINHOOD": "1",
    "BSC": "56",
    "BASE": "8453",
    "ARB": "42161",
    "ARC": "42161",
}


def _flag(value) -> bool:
    """GoPlus returns booleans as "1"/"0"; some deployments nest {"status": "1"}."""
    if isinstance(value, dict):
        value = value.get("status")
    return str(value) == "1"


def check_token_security(chain: str, token_address: str, pool_liquidity: float = 0.0) -> dict:
    """Check a Meme token's security status.

    Returns a dict with ``checked`` (did the audit actually run?),
    ``is_safe`` (False whenever checked is False), ``score`` (None when
    unchecked), ``risk_level`` (LOW/MEDIUM/HIGH/CRITICAL/UNKNOWN) and warnings.
    """
    chain = chain.upper()
    if chain in ("ARC", "ARBITRUM"):
        chain = "ARB"
    token_address = token_address.strip()

    now = time.time()
    cache_key = f"{chain}:{token_address.lower()}"
    entry = _SECURITY_CACHE.get(cache_key)
    if entry and now - entry["ts"] < CACHE_TTL:
        return entry["data"]

    result = {
        "chain": chain,
        "token_address": token_address,
        "checked": False,
        "is_safe": False,
        "score": None,
        "risk_level": "UNKNOWN",
        "warnings": [],
        "details": {
            "is_honeypot": None,
            "buy_tax_pct": None,
            "sell_tax_pct": None,
            "mint_revoked": None,
            "freeze_revoked": None,
            "lp_locked": None,
            "top10_percent": None,
            "liquidity_usd": pool_liquidity,
        },
    }

    try:
        if chain == "SOL":
            url = (f"https://api.gopluslabs.io/api/v1/solana/token_security"
                   f"?contract_addresses={token_address}")
            data = http.get_json(url)
            sec = (data.get("result") or {}).get(token_address) or {}
            if sec:
                result["checked"] = True
                mintable = _flag(sec.get("mintable"))
                freezable = _flag(sec.get("freezable"))
                is_honeypot = _flag(sec.get("is_honeypot"))
                holders = sec.get("holders_percent")
                top10 = float(holders) * 100 if holders not in (None, "") else None

                result["details"]["mint_revoked"] = not mintable
                result["details"]["freeze_revoked"] = not freezable
                result["details"]["is_honeypot"] = is_honeypot

                if is_honeypot:
                    result["warnings"].append("⚠ 检测到貔貅盘 (无法正常卖出)")
                if freezable:
                    result["warnings"].append("⚠ 未丢弃 Freeze 冻结权限 (项目方可冻结钱包)")
                if mintable:
                    result["warnings"].append("⚠ 未丢弃 Mint 增发权限 (可无限增发砸盘)")
                if top10 is not None:
                    result["details"]["top10_percent"] = round(top10, 1)
                    if top10 > 35:
                        result["warnings"].append(f"⚠ 前十大持币地址占比过高 ({top10:.1f}%)")
        elif chain in CHAIN_ID_MAP:
            cid = CHAIN_ID_MAP[chain]
            url = (f"https://api.gopluslabs.io/api/v1/token_security/{cid}"
                   f"?contract_addresses={token_address}")
            data = http.get_json(url)
            sec = (data.get("result") or {}).get(token_address.lower()) or {}
            if sec:
                result["checked"] = True
                is_honeypot = _flag(sec.get("is_honeypot"))
                cannot_sell = _flag(sec.get("cannot_sell_all"))
                buy_raw, sell_raw = sec.get("buy_tax"), sec.get("sell_tax")
                buy_tax = float(buy_raw) * 100 if buy_raw not in (None, "") else None
                sell_tax = float(sell_raw) * 100 if sell_raw not in (None, "") else None
                is_mintable = _flag(sec.get("is_mintable"))
                is_open_source = _flag(sec.get("is_open_source"))

                result["details"]["is_honeypot"] = is_honeypot or cannot_sell
                if buy_tax is not None:
                    result["details"]["buy_tax_pct"] = round(buy_tax, 1)
                if sell_tax is not None:
                    result["details"]["sell_tax_pct"] = round(sell_tax, 1)
                result["details"]["mint_revoked"] = not is_mintable

                if is_honeypot or cannot_sell:
                    result["warnings"].append("⚠ 貔貅盘拦截 (限制代币卖出)")
                if (buy_tax or 0) > 10 or (sell_tax or 0) > 10:
                    result["warnings"].append(f"⚠ 高额税率买卖盘 (买:{buy_tax:.1f}% 卖:{sell_tax:.1f}%)")
                if is_mintable:
                    result["warnings"].append("⚠ 存在所有者增发权限 (Mintable)")
                if not is_open_source:
                    result["warnings"].append("⚠ 合约代码未开源")
    except http.HttpError:
        pass                       # already logged in http.get_json

    if not result["checked"]:
        result["warnings"].append("⚠ GoPlus 审计数据不可用，未完成安全核验")
        _cache(cache_key, now, result)
        return result

    # Score only what was actually checked, plus the caller's real liquidity.
    result["score"] = 95
    result["is_safe"] = True
    result["risk_level"] = "LOW"
    for _w in result["warnings"]:
        if "貔貅" in _w:
            result["score"] -= 60
        elif "Freeze" in _w:
            result["score"] -= 30
        elif "Mint" in _w or "增发" in _w:
            result["score"] -= 20
        elif "税率" in _w:
            result["score"] -= 25
        elif "占比过高" in _w:
            result["score"] -= 15
        elif "未开源" in _w:
            result["score"] -= 10

    if pool_liquidity > 0:
        if pool_liquidity < 3000:
            result["score"] -= 25
            result["warnings"].append(f"⚠ 底池流动性极低危险 (${pool_liquidity:,.0f})")
        elif pool_liquidity < 8000:
            result["score"] -= 15
            result["warnings"].append(f"⚠ 底池流动性较薄 (${pool_liquidity:,.0f})")

    result["score"] = max(10, min(99, result["score"]))
    if result["score"] >= 80 and not result["warnings"]:
        result["risk_level"] = "LOW"
    elif result["score"] >= 65:
        result["risk_level"] = "MEDIUM"
    elif result["score"] >= 45:
        result["risk_level"] = "HIGH"
        result["is_safe"] = False
    else:
        result["risk_level"] = "CRITICAL"
        result["is_safe"] = False

    _cache(cache_key, now, result)
    return result


def _cache(key: str, now: float, data: dict):
    _SECURITY_CACHE[key] = {"ts": now, "data": data}
    if len(_SECURITY_CACHE) > CACHE_MAX:
        for k in sorted(_SECURITY_CACHE, key=lambda k: _SECURITY_CACHE[k]["ts"]):
            del _SECURITY_CACHE[k]
            if len(_SECURITY_CACHE) <= CACHE_MAX:
                break
