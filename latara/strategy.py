"""Pure trading logic (no network) so it can be unit-tested.

Strategy: 5-minute Opening Range Breakout on "Stocks in Play"
(Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy for the U.S.
Equity Market", SSRN 4729284). Adapted for a small cash account:
  * long-only (shorting needs a margin account)
  * a stock is "in play" when its first-5-minute volume is far above its
    normal first-5-minute volume
  * if that first 5-min candle is green, buy when price breaks its high
  * stop = 10% of the stock's 14-day ATR below the trigger
  * no profit target: winners ride until the end-of-day exit
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class DailyBar:
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Candidate:
    symbol: str
    atr: float
    avg_volume: float
    prev_close: float
    or_open: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    or_close: float = 0.0
    or_volume: float = 0.0
    rel_volume: float = 0.0

    @property
    def gap_pct(self) -> float:
        return (self.or_open / self.prev_close - 1) if self.prev_close else 0.0


@dataclass
class Plan:
    symbol: str
    trigger: float
    stop: float
    qty: float
    rel_volume: float
    state: str = "waiting"          # waiting -> open -> closed | expired | skipped
    entry_price: float = 0.0
    exit_price: float = 0.0
    last_price: float = 0.0
    filled_qty: float = 0.0
    stop_order_id: Optional[str] = None
    breakeven_done: bool = False
    note: str = ""
    events: list = field(default_factory=list)

    @property
    def risk_per_share(self) -> float:
        return max(self.trigger - self.stop, 0.0)

    @property
    def pnl(self) -> float:
        if self.state != "closed":
            return 0.0
        return (self.exit_price - self.entry_price) * self.filled_qty

    @property
    def r_multiple(self) -> float:
        r = (self.entry_price - self.stop) if self.entry_price else self.risk_per_share
        if self.state != "closed" or r <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / r


def atr(bars: Sequence[DailyBar], n: int = 14) -> float:
    """Average True Range over the last n bars."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else 0.0


def avg_volume(bars: Sequence[DailyBar], n: int = 14) -> float:
    vols = [b.volume for b in bars[-n:]]
    return sum(vols) / len(vols) if vols else 0.0


def relative_volume(today: float, history: Sequence[float]) -> float:
    hist = [h for h in history if h and h > 0]
    if not hist:
        return 0.0
    return today / (sum(hist) / len(hist))


def passes_filters(c: Candidate, cfg) -> tuple[bool, str]:
    if c.prev_close < cfg.min_price or c.prev_close > cfg.max_price:
        return False, f"price {c.prev_close:.2f} outside range"
    if c.avg_volume < cfg.min_avg_volume:
        return False, f"avg volume {c.avg_volume:,.0f} too low"
    if c.atr < cfg.min_atr:
        return False, f"ATR {c.atr:.2f} too small"
    if c.or_volume <= 0:
        return False, "no opening-range data"
    if c.rel_volume < cfg.min_rel_volume:
        return False, f"relative volume {c.rel_volume:.2f}x too low"
    if c.or_close <= c.or_open:
        return False, "first 5-min candle not green (long-only)"
    if getattr(cfg, "min_gap", 0.0) > 0 and c.gap_pct < cfg.min_gap:
        return False, f"gap {c.gap_pct:+.1%} below {cfg.min_gap:.0%} (gap-and-go rule)"
    return True, "ok"


def rank(cands: Sequence[Candidate]) -> list[Candidate]:
    """Most 'in play' first (highest relative volume)."""
    return sorted(cands, key=lambda c: c.rel_volume, reverse=True)


def floor_qty(qty: float, decimals: int = 3) -> float:
    f = 10 ** decimals
    return math.floor(qty * f) / f


def position_size(slot_budget: float, price: float, risk_per_share: float,
                  risk_dollars: float) -> float:
    """Shares to buy: limited by BOTH the cash in this slot and the risk budget.
    Fractional shares allowed (Alpaca supports them)."""
    if price <= 0 or slot_budget <= 0:
        return 0.0
    by_cash = slot_budget / price
    by_risk = risk_dollars / risk_per_share if risk_per_share > 0 else by_cash
    qty = min(by_cash, by_risk)
    if qty >= 1:
        qty = math.floor(qty)  # whole shares when possible (broker-side stops work)
    else:
        qty = floor_qty(qty)
    return qty if qty * price >= 1.0 else 0.0   # Alpaca minimum $1 order


def build_plan(c: Candidate, cfg, slot_budget: float, day_budget: float) -> Plan:
    trigger = round(c.or_high + 0.01, 2)
    stop = round(trigger - cfg.atr_stop_frac * c.atr, 2)
    if stop >= trigger:
        stop = round(trigger - 0.01, 2)
    risk_dollars = day_budget * cfg.risk_per_trade
    qty = position_size(slot_budget, trigger, trigger - stop, risk_dollars)
    return Plan(symbol=c.symbol, trigger=trigger, stop=stop, qty=qty, rel_volume=c.rel_volume)


def should_enter(plan: Plan, price: float, max_slippage: float) -> tuple[bool, str]:
    if plan.state != "waiting":
        return False, "not waiting"
    if price < plan.trigger:
        return False, "below trigger"
    if price > plan.trigger * (1 + max_slippage):
        return False, "ran past trigger (too much slippage)"
    return True, "breakout"


def should_stop_out(plan: Plan, price: float) -> bool:
    return plan.state == "open" and price <= plan.stop


def maybe_breakeven(plan: Plan, price: float, at_r: float) -> bool:
    """Return True if the stop should be raised to entry."""
    if at_r <= 0 or plan.state != "open" or plan.breakeven_done:
        return False
    r = plan.entry_price - plan.stop
    return r > 0 and price >= plan.entry_price + at_r * r
