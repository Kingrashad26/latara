"""Latara's settings. Everything can be changed with environment variables
(GitHub repo Variables / Secrets) without touching code."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _s(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _b(name: str, default: bool) -> bool:
    return _s(name, str(default)).lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    return float(_s(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(float(_s(name, str(default))))


def _brain() -> dict:
    """Strategy rules Latara learned in her weekly study (see lab.py).
    Repo Variables still override them."""
    try:
        from .lab import clamp
        return clamp(json.loads(Path(__file__).with_name("brain.json").read_text()).get("params", {}))
    except Exception:
        return {}


@dataclass(frozen=True)
class Config:
    # --- account ---
    api_key: str
    api_secret: str
    live: bool                 # False = paper trading (default)
    enabled: bool              # kill switch
    intraday_feed: str         # "iex" (free) or "sip" (paid)
    daily_feed: str            # "sip" works on free plan for bars older than 15 min

    # --- money / risk ---
    max_capital: float         # most Latara will ever deploy in one day
    max_positions: int         # trades per day (one per stock)
    risk_per_trade: float      # fraction of the day's budget risked per trade
    daily_loss_limit: float    # stop for the day if equity falls this fraction
    max_losers: int            # stop for the day after this many losing trades
    max_entry_slippage: float  # skip entry if price ran this far past the trigger

    # --- strategy (5-min ORB on Stocks in Play) ---
    atr_stop_frac: float       # stop = entry trigger - this * 14-day ATR
    min_price: float
    max_price: float
    min_avg_volume: float      # 14-day average daily shares (consolidated/SIP)
    min_atr: float             # dollars
    min_rel_volume: float      # first-5-min volume vs its 14-day average
    lookback_days: int
    require_spy_green: bool    # only go long when SPY's first 5-min bar is green
    breakeven_at_r: float      # move stop to entry after +N R (0 = off)
    min_gap: float             # gap-and-go filter: required opening gap (0 = off)

    # --- timing (US/Eastern) ---
    entry_cutoff: str          # no new entries after this time
    exit_time: str             # flatten everything by this time
    max_runtime_minutes: int   # GitHub Actions jobs die at 6h
    max_wait_for_open_minutes: int
    poll_seconds: int

    daily_goal: float          # dollars per day (scorecard target)
    dry_run: bool              # log signals, place no orders


def load() -> Config:
    mode = _s("LATARA_MODE", "paper").lower()
    confirm = _s("LATARA_CONFIRM_LIVE", "no").lower()
    live = mode == "live" and confirm == "i-understand-this-is-real-money"
    b = _brain()
    return Config(
        api_key=_s("ALPACA_API_KEY", ""),
        api_secret=_s("ALPACA_SECRET_KEY", ""),
        live=live,
        enabled=_b("LATARA_ENABLED", True),
        intraday_feed=_s("LATARA_INTRADAY_FEED", "iex"),
        daily_feed=_s("LATARA_DAILY_FEED", "sip"),
        max_capital=_f("LATARA_MAX_CAPITAL", 100.0),
        max_positions=_i("LATARA_MAX_POSITIONS", b.get("max_positions", 2)),
        risk_per_trade=_f("LATARA_RISK_PER_TRADE", 0.02),
        daily_loss_limit=_f("LATARA_DAILY_LOSS_LIMIT", 0.05),
        max_losers=_i("LATARA_MAX_LOSERS", 2),
        max_entry_slippage=_f("LATARA_MAX_ENTRY_SLIPPAGE", 0.005),
        atr_stop_frac=_f("LATARA_ATR_STOP_FRAC", b.get("atr_stop_frac", 0.10)),
        min_price=_f("LATARA_MIN_PRICE", 5.0),
        max_price=_f("LATARA_MAX_PRICE", 1000.0),
        min_avg_volume=_f("LATARA_MIN_AVG_VOLUME", 1_000_000),
        min_atr=_f("LATARA_MIN_ATR", 0.50),
        min_rel_volume=_f("LATARA_MIN_REL_VOLUME", b.get("min_rel_volume", 1.0)),
        lookback_days=_i("LATARA_LOOKBACK_DAYS", 14),
        require_spy_green=_b("LATARA_REQUIRE_SPY_GREEN", b.get("require_spy_green", False)),
        breakeven_at_r=_f("LATARA_BREAKEVEN_AT_R", b.get("breakeven_at_r", 0.0)),
        min_gap=_f("LATARA_MIN_GAP", b.get("min_gap", 0.0)),
        entry_cutoff=_s("LATARA_ENTRY_CUTOFF", "11:30"),
        exit_time=_s("LATARA_EXIT_TIME", "15:45"),
        max_runtime_minutes=_i("LATARA_MAX_RUNTIME_MINUTES", 340),
        max_wait_for_open_minutes=_i("LATARA_MAX_WAIT_FOR_OPEN", 50),
        poll_seconds=_i("LATARA_POLL_SECONDS", 10),
        daily_goal=_f("LATARA_DAILY_GOAL", 100.0),
        dry_run=_b("LATARA_DRY_RUN", False),
    )
