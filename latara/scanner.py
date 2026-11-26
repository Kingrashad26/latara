"""Find today's Stocks in Play."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .strategy import Candidate, DailyBar, atr, avg_volume, relative_volume
from .timeutil import ET

log = logging.getLogger("latara.scanner")

# Liquid, historically active names that are often "in play". The live
# screener (most-active + top gainers) is merged in every morning.
CORE_WATCHLIST = [
    "TSLA", "NVDA", "AMD", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "NFLX", "AVGO",
    "PLTR", "SOFI", "COIN", "MSTR", "HOOD", "MARA", "RIOT", "SMCI", "ARM", "MU",
    "INTC", "BA", "UBER", "SHOP", "SNAP", "RIVN", "LCID", "NIO", "F", "AAL",
    "CCL", "DKNG", "RBLX", "AFRM", "UPST", "CVNA", "ROKU", "SQ", "PYPL", "DIS",
]


def _feed(name: str) -> DataFeed:
    return DataFeed.SIP if name.lower() == "sip" else DataFeed.IEX


def _clean(sym: str) -> bool:
    return sym.isalpha() and 1 <= len(sym) <= 5


def candidate_symbols(screener: ScreenerClient) -> list[str]:
    syms: list[str] = []
    try:
        ma = screener.get_most_actives(MostActivesRequest(top=50))
        syms += [s.symbol for s in ma.most_actives]
    except Exception as e:  # screener is best-effort
        log.warning("most-actives screener failed: %s", e)
    try:
        mv = screener.get_market_movers(MarketMoversRequest(top=25))
        syms += [m.symbol for m in mv.gainers]
    except Exception as e:
        log.warning("movers screener failed: %s", e)
    syms += CORE_WATCHLIST
    seen, out = set(), []
    for s in syms:
        if _clean(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _bars(client, symbols, timeframe, start, end, feed):
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=timeframe,
                           start=start, end=end, feed=feed)
    return client.get_stock_bars(req).data


def load_daily(client: StockHistoricalDataClient, symbols: list[str], today: date,
               cfg) -> dict[str, Candidate]:
    """ATR, average volume, previous close — from completed daily bars."""
    start = datetime.combine(today - timedelta(days=cfg.lookback_days * 2 + 10), time(0), ET)
    end = datetime.combine(today, time(0), ET)      # yesterday and earlier only
    try:
        data = _bars(client, symbols, TimeFrame.Day, start, end, _feed(cfg.daily_feed))
        volume_ok = True
    except Exception as e:
        log.warning("daily bars on %s feed failed (%s); falling back to IEX", cfg.daily_feed, e)
        data = _bars(client, symbols, TimeFrame.Day, start, end, DataFeed.IEX)
        volume_ok = False   # IEX volume is a small slice of the market
    out = {}
    for sym, bars in data.items():
        db = [DailyBar(b.high, b.low, b.close, b.volume) for b in bars]
        if len(db) < cfg.lookback_days // 2:
            continue
        out[sym] = Candidate(
            symbol=sym,
            atr=atr(db, cfg.lookback_days),
            avg_volume=avg_volume(db, cfg.lookback_days) if volume_ok else float("inf"),
            prev_close=db[-1].close,
        )
    return out


def first5_volume_history(client, symbols: list[str], past_days: list[date], feed) -> dict[str, list[float]]:
    """Volume of the 9:30-9:35 bar for each of the past trading days."""
    hist: dict[str, list[float]] = {s: [] for s in symbols}
    for d in past_days:
        start = datetime.combine(d, time(9, 30), ET)
        try:
            data = _bars(client, symbols, TimeFrame(5, TimeFrameUnit.Minute), start,
                         start + timedelta(minutes=5), feed)
        except Exception as e:
            log.warning("history %s failed: %s", d, e)
            continue
        for sym, bars in data.items():
            if bars:
                hist.setdefault(sym, []).append(float(bars[0].volume))
    return hist


def opening_range(client, symbols: list[str], today: date, feed) -> dict:
    start = datetime.combine(today, time(9, 30), ET)
    data = _bars(client, symbols, TimeFrame(5, TimeFrameUnit.Minute), start,
                 start + timedelta(minutes=5), feed)
    return {s: b[0] for s, b in data.items() if b}


def scan(data_client, screener, cfg, today: date, past_days: list[date]) -> list[Candidate]:
    syms = candidate_symbols(screener)
    log.info("scanning %d symbols", len(syms))
    daily = load_daily(data_client, syms, today, cfg)
    pre = [s for s, c in daily.items()
           if cfg.min_price <= c.prev_close <= cfg.max_price
           and c.avg_volume >= cfg.min_avg_volume and c.atr >= cfg.min_atr]
    log.info("%d pass price/liquidity/ATR filters", len(pre))
    if not pre:
        return []
    feed = _feed(cfg.intraday_feed)
    hist = first5_volume_history(data_client, pre, past_days[-cfg.lookback_days:], feed)
    orb = opening_range(data_client, pre, today, feed)
    out = []
    for s in pre:
        bar = orb.get(s)
        if bar is None:
            continue
        c = daily[s]
        c.or_open, c.or_high, c.or_low, c.or_close = bar.open, bar.high, bar.low, bar.close
        c.or_volume = float(bar.volume)
        c.rel_volume = relative_volume(c.or_volume, hist.get(s, []))
        out.append(c)
    return out


def spy_first_bar_green(data_client, today: date, feed_name: str) -> bool:
    bars = opening_range(data_client, ["SPY"], today, _feed(feed_name))
    b = bars.get("SPY")
    return bool(b and b.close > b.open)
