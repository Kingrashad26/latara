"""Backup flatten + performance scorecard."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

from .config import Config
from .timeutil import ET, at, now_et

log = logging.getLogger("latara")


def _client(cfg: Config) -> TradingClient:
    if not cfg.api_key:
        raise SystemExit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    return TradingClient(cfg.api_key, cfg.api_secret, paper=not cfg.live)


def flatten(cfg: Config):
    """Safety net run near the close: cancel orders and close any position
    left open (e.g. if the main job crashed). Only acts during the last
    minutes of a live session, so it never queues orders after hours."""
    tc = _client(cfg)
    clock = tc.get_clock()
    now = now_et()
    if not clock.is_open:
        log.info("Market closed – nothing to flatten.")
        return
    close = clock.next_close.astimezone(ET)
    if (close - now).total_seconds() > 30 * 60:
        log.info("More than 30 min to the close (%s) – off-season schedule slot, exiting.", close)
        return
    positions = tc.get_all_positions()
    log.info("Backup flatten: %d open position(s).", len(positions))
    if positions or tc.get_orders():
        tc.close_all_positions(cancel_orders=True)


def scorecard(cfg: Config):
    tc = _client(cfg)
    h = tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
    rows = [(datetime.fromtimestamp(t, ET).date(), pl or 0.0, eq or 0.0)
            for t, pl, eq in zip(h.timestamp, h.profit_loss, h.equity)]
    rows = [r for r in rows if r[2] > 0]
    goal = cfg.daily_goal
    lines = ["## Latara – 1-month scorecard", "",
             "| Day | P&L | Equity | Goal |", "|---|---|---|---|"]
    for d, pl, eq in rows:
        lines.append(f"| {d} | {pl:+,.2f} | {eq:,.2f} | {'✅' if pl >= goal else '—'} |")
    pls = [r[1] for r in rows]
    if pls:
        wins = [p for p in pls if p > 0]
        lines += ["",
                  f"- Trading days: {len(pls)} · green days: {len(wins)} ({len(wins)/len(pls):.0%})",
                  f"- Total P&L: {sum(pls):+,.2f} · avg/day: {sum(pls)/len(pls):+,.2f}",
                  f"- Days that hit ${goal:,.0f}: {sum(1 for p in pls if p >= goal)}",
                  f"- Worst day: {min(pls):+,.2f} · best day: {max(pls):+,.2f}"]
    report = "\n".join(lines)
    print(report)
    os.makedirs("logs", exist_ok=True)
    with open("logs/scorecard.md", "w") as f:
        f.write(report + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")
