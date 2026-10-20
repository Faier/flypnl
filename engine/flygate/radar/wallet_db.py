"""User-curated wallet address pool.

Stores addresses the user wants to keep for future tracking. The database
ships EMPTY on purpose: no wallet track record (win rate, PnL, style) is
preset, because the system has no wallet-level on-chain trade data source yet
and every such number would be invented. Win-rate / PnL fields are optional
user-supplied notes, rendered as "无数据" when absent.
"""
from __future__ import annotations

import json
import os
import time
from typing import List, Dict, Optional


class SmartWallet:
    def __init__(self, data: dict):
        self.chain = data.get("chain", "SOL").upper()
        self.address = data.get("address", "").strip()
        self.alias = data.get("alias", "未命名地址")
        wr = data.get("win_rate")
        self.win_rate = float(wr) if wr not in (None, "") else None
        pnl = data.get("pnl_usd")
        self.pnl_usd = float(pnl) if pnl not in (None, "") else None
        self.style = data.get("style", "")
        ahl = data.get("avg_hold_min")
        self.avg_hold_min = int(ahl) if ahl not in (None, "") else None
        self.top_tokens = data.get("top_tokens", [])
        self.enabled = bool(data.get("enabled", True))
        self.verified = bool(data.get("verified", False))
        self.added_at = data.get("added_at", time.time())

    def to_dict(self) -> dict:
        return {
            "chain": self.chain,
            "address": self.address,
            "alias": self.alias,
            "win_rate": round(self.win_rate, 2) if self.win_rate is not None else None,
            "pnl_usd": round(self.pnl_usd, 0) if self.pnl_usd is not None else None,
            "style": self.style,
            "avg_hold_min": self.avg_hold_min,
            "top_tokens": self.top_tokens,
            "enabled": self.enabled,
            "verified": self.verified,
            "added_at": self.added_at,
        }


class WalletDB:
    def __init__(self, runs_dir: str):
        self.db_dir = os.path.join(runs_dir, "radar")
        os.makedirs(self.db_dir, exist_ok=True)
        self.db_file = os.path.join(self.db_dir, "wallets.json")
        self.wallets: Dict[str, SmartWallet] = {}
        self.load()

    def load(self):
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        w = SmartWallet(item)
                        if w.address:
                            self.wallets[w.address.lower()] = w
            except Exception as e:
                print(f"[radar/db] failed to load wallets: {e}")

    def save(self):
        try:
            data = [w.to_dict() for w in self.wallets.values()]
            with open(self.db_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[radar/db] failed to save wallets: {e}")

    def list_wallets(self, chain: Optional[str] = None, enabled_only: bool = False) -> List[dict]:
        res = []
        for w in self.wallets.values():
            if enabled_only and not w.enabled:
                continue
            if chain and chain.upper() != "ALL" and w.chain != chain.upper():
                # Robinhood can be listed under ETH or ROBINHOOD
                if chain.upper() == "ETH" and w.chain in ("ETH", "ROBINHOOD"):
                    pass
                elif chain.upper() == "ARC" and w.chain == "ARB":
                    pass
                else:
                    continue
            res.append(w.to_dict())
        res.sort(key=lambda x: (x["chain"], -(x["win_rate"] or 0), -(x["pnl_usd"] or 0)))
        return res

    def add_wallet(self, chain: str, address: str, alias: str = "",
                   win_rate: Optional[float] = None, pnl_usd: Optional[float] = None,
                   style: str = "自定义关注") -> dict:
        address = address.strip()
        if not address:
            raise ValueError("address cannot be empty")
        key = address.lower()
        alias = alias.strip() or f"收藏地址 ({address[:6]}...)"
        chain = chain.upper()
        if chain in ("ARC", "ARBITRUM"):
            chain = "ARB"

        w = SmartWallet({
            "chain": chain,
            "address": address,
            "alias": alias,
            "win_rate": win_rate,
            "pnl_usd": pnl_usd,
            "style": style,
            "avg_hold_min": None,
            "top_tokens": [],
            "enabled": True,
            "verified": False,
            "added_at": time.time(),
        })
        self.wallets[key] = w
        self.save()
        return w.to_dict()

    def remove_wallet(self, address: str) -> bool:
        key = address.strip().lower()
        if key in self.wallets:
            del self.wallets[key]
            self.save()
            return True
        return False

    def toggle_wallet(self, address: str, enabled: bool) -> bool:
        key = address.strip().lower()
        if key in self.wallets:
            self.wallets[key].enabled = enabled
            self.save()
            return True
        return False

    def get_wallet(self, address: str) -> Optional[SmartWallet]:
        return self.wallets.get(address.strip().lower())
