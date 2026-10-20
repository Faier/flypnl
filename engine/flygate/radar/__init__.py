"""Multi-chain Meme token radar for FlyGate.

Scope (honest): scans DexScreener for trending / newly-launched tokens,
audits them through GoPlus, and tracks a user watchlist. It does NOT track
wallet-level on-chain trades — no per-wallet data source is wired in, so no
event is attributed to a wallet and there is no copy-trade signal.
"""
from __future__ import annotations

from .wallet_db import WalletDB, SmartWallet
from .security import check_token_security
from .tracker import RadarTracker
