"""Latara's weekly study session.

Every week Latara:
  1. downloads the last ~2 months of 5-minute market data,
  2. replays her strategy on it with many rule variations, including filters
     that successful momentum day traders use (e.g. "gap-and-go": only trade
     stocks that gapped up; "trade with the market": only when SPY opens green),
  3. learns on the older weeks and tests on the most recent weeks she has
     NOT seen (walk-forward), so she doesn't just memorise the past,
  4. adopts a new rule set only if it beat her current one on those unseen
     weeks, made money there, and had enough trades to mean something,
  5. compares the replay with her real trade journal ("reality check").

She also learns her loss limits (how many losing trades, or how big a daily
loss, before she stops for the day). Her capital compounds with her own
results (see money.py). Paper vs live is never touched by learning, and every
learned value stays inside fixed bounds.
"""
from __future__ import annotations

import csv
import itertools
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .strategy import DailyBar, atr, avg_volume, relative_volume
from .timeutil import ET, now_et

log = logging.getLogger("latara.lab")
BRAIN = Path(__file__).with_name("brain.json")
JOURNAL = Path("journal/trades.csv")

# What Latara is allowed to learn, and the only values she may choose from.
GRID = {
    "atr_stop_frac": [0.05, 0.10, 0.15, 0.20],
    "min_rel_volume": [1.0, 2.0, 3.0],
    "max_positions": [1, 2],
    "breakeven_at_r": [0.0, 1.5],
    "require_spy_green": [False, True],
    "min_gap": [0.0, 0.02],
    "max_losers": [1, 2],
    "daily_loss_limit": [0.02, 0.05],
}
DEFAULTS = {"atr_stop_frac": 0.10, "min_rel_volume": 1.0, "max_positions": 2,
            "breakeven_at_r": 0.0, "require_spy_green": False, "min_gap": 0.0,
            "max_losers": 2, "daily_loss_limit": 0.05}
MIN_TRAIN_TRADES = 15
MIN_TEST_TRADES = 4
MIN_EDGE = 0.01          # new rules must beat current ones by 1 percentage point on unseen weeks


@dataclass
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


def clamp(params: dict) -> dict:
    """Force any learned value back inside the allowed set."""
    out = dict(DEFAULTS)
    for k, allowed in GRID.items():
        v = params.get(k, DEFAULTS[k])
        if isinstance(allowed[0], bool):
            out[k] = bool(v)
        else:
            out[k] = min(allowed, key=lambda a: abs(a - float(v)))
    return out


def load_brain() -> dict:
    try:
        return json.loads(BRAIN.read_text())
    except Exception:
        return {"params": dict(DEFAULTS), "history": []}


# ---------------------------------------------------------------- replay
def simulate_day(day, bars, daily_info, hist_first, spy_green, p, slippage=0.0005,
                 max_chase=0.005, cutoff=time(11, 30), exit_t=time(15, 45)):
    """Replay one day with rule set p. Returns list of trades."""
    if p["require_spy_green"] and not spy_green:
        return []
    cands = []
    for sym, bs in bars.items():
        if sym == "SPY" or not bs or bs[0].t.time() != time(9, 30):
            continue
        info = daily_info.get(sym)
        if not info:
            continue
        a, av, pc = info
        if pc < 5 or av < 1e6 or a < 0.5:
            continue
        first = bs[0]
        rv = relative_volume(first.v, hist_first.get(sym, []))
        gap = first.o / pc - 1 if pc else 0
        if rv < p["min_rel_volume"] or first.c <= first.o or (p["min_gap"] > 0 and gap < p["min_gap"]):
            continue
        cands.append((rv, sym, first, a))
    cands.sort(key=lambda x: x[0], reverse=True)
    trades = []
    for rv, sym, first, a in cands[:p["max_positions"]]:
        trig = round(first.h + 0.01, 2)
        stop = round(trig - p["atr_stop_frac"] * a, 2)
        if stop >= trig:
            stop = trig - 0.01
        risk = trig - stop
        entry, exit_px, cur_stop, entry_t, exit_t2 = None, None, stop, None, None
        for b in bars[sym][1:]:
            tt = b.t.time()
            if entry is None:
                if tt >= cutoff:
                    break
                if b.h >= trig:
                    if b.o > trig * (1 + max_chase):
                        break                      # gapped past trigger: live Latara skips
                    entry, entry_t = max(trig, b.o) * (1 + slippage), b.t
                    if b.l <= cur_stop:            # same-bar stop: assume the worst
                        exit_px, exit_t2 = cur_stop * (1 - slippage), b.t
                        break
                continue
            if tt >= exit_t:
                exit_px, exit_t2 = b.o * (1 - slippage), b.t
                break
            if b.l <= cur_stop:
                exit_px, exit_t2 = min(cur_stop, b.o) * (1 - slippage), b.t
                break
            if p["breakeven_at_r"] > 0 and b.h >= entry + p["breakeven_at_r"] * risk:
                cur_stop = max(cur_stop, entry)
        if entry is not None:
            if exit_px is None:
                exit_px, exit_t2 = bars[sym][-1].c * (1 - slippage), bars[sym][-1].t
            trades.append({"day": str(day), "symbol": sym, "r": (exit_px - entry) / risk,
                           "ret": exit_px / entry - 1, "slots": p["max_positions"],
                           "entry_t": entry_t, "exit_t": exit_t2})
    return apply_loss_limits(trades, p)


def apply_loss_limits(trades, p):
    """Drop entries that live Latara would have skipped because she had already
    hit her loser count or daily loss limit earlier that day."""
    kept = []
    for t in sorted(trades, key=lambda x: x["entry_t"]):
        done = [k for k in kept if k["exit_t"] <= t["entry_t"]]
        losers = sum(1 for k in done if k["ret"] < 0)
        day_ret = sum(k["ret"] for k in done) / p["max_positions"]
        if losers >= p["max_losers"] or day_ret <= -p["daily_loss_limit"]:
            continue
        kept.append(t)
    return kept


def evaluate(days, data, p) -> dict:
    trades, day_rets = [], []
    for d in days:
        t = simulate_day(d, *data[d], p)
        trades += t
        day_rets.append(sum(x["ret"] for x in t) / p["max_positions"])
    growth = 1.0
    for r in day_rets:
        growth *= 1 + r
    n = len(trades)
    wins = [t for t in trades if t["r"] > 0]
    return {"trades": n, "win_rate": len(wins) / n if n else 0.0,
            "avg_r": sum(t["r"] for t in trades) / n if n else 0.0,
            "return": growth - 1, "green_days": sum(1 for r in day_rets if r > 0),
            "days": len(day_rets)}


def all_rule_sets():
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, combo))


def learn(train_days, test_days, data, current: dict) -> tuple[dict, dict]:
    """Walk-forward choice. Returns (params_to_use, evidence)."""
    scored = []
    for p in all_rule_sets():
        r = evaluate(train_days, data, p)
        if r["trades"] >= MIN_TRAIN_TRADES:
            scored.append((r["return"], p, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    cur_train = evaluate(train_days, data, current)
    cur_test = evaluate(test_days, data, current)
    evidence = {"train_days": len(train_days), "test_days": len(test_days),
                "current_train": cur_train, "current_test": cur_test, "candidates": len(scored),
                "tested": sum(1 for _ in all_rule_sets())}
    if not scored:
        evidence["decision"] = "kept current rules (not enough trades to learn from)"
        return current, evidence
    best_ret, best, best_train = scored[0]
    best_test = evaluate(test_days, data, best)
    evidence.update(best=best, best_train=best_train, best_test=best_test)
    if best == current:
        evidence["decision"] = "kept current rules (they were already the best)"
        return current, evidence
    if (best_test["trades"] >= MIN_TEST_TRADES and best_test["return"] > 0
            and best_test["return"] > cur_test["return"] + MIN_EDGE):
        evidence["decision"] = "ADOPTED new rules (beat current rules on unseen recent weeks)"
        return best, evidence
    evidence["decision"] = "kept current rules (new idea didn't hold up on unseen weeks)"
    return current, evidence


# ---------------------------------------------------------------- data
def build_dataset(dc, tc, symbols, feed_name="sip", warmup=14):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.requests import GetCalendarRequest

    today = now_et().date()
    cal = tc.get_calendar(GetCalendarRequest(start=today - timedelta(days=95), end=today))
    days = [c.date for c in cal if c.date < today]
    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    syms = sorted(set(symbols) | {"SPY"})

    def get(req):
        try:
            return dc.get_stock_bars(req).data
        except Exception as e:
            if feed == DataFeed.SIP:
                log.warning("SIP data failed (%s); using IEX", e)
                req.feed = DataFeed.IEX
                return dc.get_stock_bars(req).data
            raise

    intraday = get(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                    start=datetime.combine(days[0], time(9, 30), ET),
                                    end=datetime.combine(days[-1], time(16, 0), ET), feed=feed))
    daily = get(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame.Day,
                                 start=datetime.combine(days[0] - timedelta(days=40), time(0), ET),
                                 end=datetime.combine(today, time(0), ET), feed=feed))
    by_day: dict = {d: {} for d in days}
    for sym, bs in intraday.items():
        for b in bs:
            t = b.timestamp.astimezone(ET)
            if t.date() in by_day and time(9, 30) <= t.time() < time(16, 0):
                by_day[t.date()].setdefault(sym, []).append(Bar(t, b.open, b.high, b.low, b.close, float(b.volume)))
    dailies = {s: [(b.timestamp.astimezone(ET).date(), DailyBar(b.high, b.low, b.close, b.volume)) for b in bs]
               for s, bs in daily.items()}
    data = {}
    usable = days[warmup:]
    for i, d in enumerate(days):
        if d not in usable:
            continue
        info = {}
        for s, rows in dailies.items():
            prior = [db for (dd, db) in rows if dd < d][-30:]
            if len(prior) >= 7:
                info[s] = (atr(prior, 14), avg_volume(prior, 14), prior[-1].close)
        hist = {}
        for pd in days[max(0, i - 14):i]:
            for s, bs in by_day[pd].items():
                if bs and bs[0].t.time() == time(9, 30):
                    hist.setdefault(s, []).append(bs[0].v)
        spy = by_day[d].get("SPY")
        spy_green = bool(spy and spy[0].t.time() == time(9, 30) and spy[0].c > spy[0].o)
        data[d] = (by_day[d], info, hist, spy_green)
    return usable, data


# ---------------------------------------------------------------- journal
def journal_stats() -> dict:
    if not JOURNAL.exists():
        return {"trades": 0}
    with open(JOURNAL) as f:
        rows = [r for r in csv.DictReader(f) if r.get("state") == "closed" and r.get("mode") != "dry-run"]
    if not rows:
        return {"trades": 0}
    rs = [float(r["r"]) for r in rows]
    pnl = [float(r["pnl"]) for r in rows]
    return {"trades": len(rows), "win_rate": sum(1 for x in rs if x > 0) / len(rs),
            "avg_r": sum(rs) / len(rs), "total_pnl": sum(pnl)}


# ---------------------------------------------------------------- main
def _fmt(r: dict) -> str:
    if not r or not r.get("trades"):
        return "no trades"
    return (f"{r['trades']} trades, win rate {r['win_rate']:.0%}, avg {r['avg_r']:+.2f}R, "
            f"return {r['return']:+.1%}, green days {r['green_days']}/{r['days']}")


def describe(p: dict) -> str:
    bits = [f"stop = {p['atr_stop_frac']:.0%} of ATR",
            f"needs {p['min_rel_volume']:.0f}x normal opening volume",
            f"{p['max_positions']} trade(s)/day"]
    bits.append(f"stop to breakeven at +{p['breakeven_at_r']}R" if p["breakeven_at_r"] else "no breakeven move")
    bits.append("only when SPY opens green" if p["require_spy_green"] else "any market open")
    bits.append(f"gap-up of {p['min_gap']:.0%}+ required" if p["min_gap"] else "no gap requirement")
    bits.append(f"stop for the day after {p['max_losers']} loser(s) or {p['daily_loss_limit']:.0%} down")
    return "; ".join(bits)


def study(cfg):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    from .scanner import CORE_WATCHLIST

    tc = TradingClient(cfg.api_key, cfg.api_secret, paper=not cfg.live)
    dc = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
    brain = load_brain()
    current = clamp(brain.get("params", {}))
    days, data = build_dataset(dc, tc, CORE_WATCHLIST, cfg.daily_feed)
    split = int(len(days) * 0.7)
    chosen, ev = learn(days[:split], days[split:], data, current)
    chosen = clamp(chosen)
    real = journal_stats()

    entry = {"date": str(now_et().date()), "decision": ev["decision"], "params": chosen}
    brain = {"version": 1, "updated": str(now_et()), "params": chosen, "evidence": ev,
             "history": (brain.get("history", []) + [entry])[-12:]}
    BRAIN.write_text(json.dumps(brain, indent=2, default=str) + "\n")

    lines = ["## Latara – weekly study session", "",
             f"**Decision:** {ev['decision']}", "",
             f"**Rules for next week:** {describe(chosen)}", "",
             f"Studied {len(days)} trading days of 5-minute data on {len(CORE_WATCHLIST)} active stocks, "
             f"tested {ev['tested']} rule variations ({ev['candidates']} had enough trades to judge; "
             f"learned on {ev['train_days']} days, "
             f"tested on the {ev['test_days']} most recent days).", "",
             "| | Learning weeks | Unseen recent weeks |", "|---|---|---|",
             f"| Current rules | {_fmt(ev['current_train'])} | {_fmt(ev['current_test'])} |"]
    if ev.get("best"):
        lines.append(f"| Best new idea | {_fmt(ev['best_train'])} | {_fmt(ev['best_test'])} |")
        lines += ["", f"Best new idea was: {describe(ev['best'])}"]
    try:
        from .money import todays_capital
        lines += ["", f"**Capital:** {todays_capital(cfg, JOURNAL)[1]}"]
    except Exception:
        pass
    lines += ["", "### Reality check (her real paper trades so far)"]
    if real["trades"]:
        lines.append(f"{real['trades']} trades, win rate {real['win_rate']:.0%}, avg {real['avg_r']:+.2f}R, "
                     f"total P&L {real['total_pnl']:+,.2f}")
    else:
        lines.append("No real trades yet.")
    lines += ["", "_Replays are estimates: real fills at the open are usually worse. "
                  "Paper vs live is never changed by learning._"]
    report = "\n".join(lines)
    print(report)
    os.makedirs("logs", exist_ok=True)
    Path("logs/study.md").write_text(report + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")
    return chosen, ev
