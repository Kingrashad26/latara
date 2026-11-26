"""Latara's trading day."""
from __future__ import annotations

import csv
import logging
import os
import time as _time
from datetime import timedelta
from pathlib import Path

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, LimitOrderRequest, StopOrderRequest

from . import scanner
from .config import Config
from .strategy import Plan, build_plan, maybe_breakeven, passes_filters, rank, should_enter, should_stop_out
from .timeutil import at, now_et

log = logging.getLogger("latara")
LOG_DIR = Path(os.getenv("LATARA_LOG_DIR", "logs"))


class Latara:
    def __init__(self, cfg: Config):
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
        self.cfg = cfg
        self.tc = TradingClient(cfg.api_key, cfg.api_secret, paper=not cfg.live)
        self.dc = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
        self.sc = ScreenerClient(cfg.api_key, cfg.api_secret)
        self.plans: list[Plan] = []
        self.losers = 0
        self.halted = ""

    # ---------- helpers ----------
    def _feed(self):
        return DataFeed.SIP if self.cfg.intraday_feed == "sip" else DataFeed.IEX

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            res = self.dc.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbols, feed=self._feed()))
            return {s: float(t.price) for s, t in res.items()}
        except Exception as e:
            log.warning("price fetch failed: %s", e)
            return {}

    def equity(self) -> float:
        return float(self.tc.get_account().equity)

    def past_trading_days(self, today):
        cal = self.tc.get_calendar(GetCalendarRequest(start=today - timedelta(days=40), end=today))
        return [c.date for c in cal if c.date < today]

    def _wait_fill(self, order_id: str, seconds: int = 20):
        deadline = _time.time() + seconds
        o = self.tc.get_order_by_id(order_id)
        while _time.time() < deadline and o.status not in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                                            OrderStatus.REJECTED, OrderStatus.EXPIRED):
            _time.sleep(1)
            o = self.tc.get_order_by_id(order_id)
        return o

    # ---------- orders ----------
    def enter(self, p: Plan, price: float):
        limit = round(min(price, p.trigger * (1 + self.cfg.max_entry_slippage)) + 0.01, 2)
        log.info("ENTRY %s qty=%s limit=%.2f trigger=%.2f stop=%.2f", p.symbol, p.qty, limit, p.trigger, p.stop)
        if self.cfg.dry_run:
            p.state, p.entry_price, p.filled_qty = "open", price, p.qty
            return
        o = self.tc.submit_order(LimitOrderRequest(symbol=p.symbol, qty=p.qty, side=OrderSide.BUY,
                                                   time_in_force=TimeInForce.DAY, limit_price=limit))
        o = self._wait_fill(str(o.id))
        filled = float(o.filled_qty or 0)
        if o.status != OrderStatus.FILLED:
            try:
                self.tc.cancel_order_by_id(str(o.id))
            except APIError:
                pass
            o = self.tc.get_order_by_id(str(o.id))
            filled = float(o.filled_qty or 0)
        if filled <= 0:
            log.info("  not filled, will retry if price is still valid")
            return
        p.state, p.filled_qty, p.entry_price = "open", filled, float(o.filled_avg_price)
        p.events.append(f"bought {filled} @ {p.entry_price:.2f}")
        self.place_stop(p)

    def place_stop(self, p: Plan):
        """Broker-side stop so Latara is protected even if the job dies."""
        if self.cfg.dry_run:
            return
        if p.stop_order_id:
            try:
                self.tc.cancel_order_by_id(p.stop_order_id)
            except APIError:
                pass
            p.stop_order_id = None
        try:
            o = self.tc.submit_order(StopOrderRequest(symbol=p.symbol, qty=p.filled_qty, side=OrderSide.SELL,
                                                      time_in_force=TimeInForce.DAY, stop_price=p.stop))
            p.stop_order_id = str(o.id)
        except APIError as e:
            log.warning("  broker stop rejected (%s) – using software stop only", e)

    def exit(self, p: Plan, reason: str, price: float | None = None):
        log.info("EXIT %s (%s)", p.symbol, reason)
        price = price or p.last_price or None
        if self.cfg.dry_run:
            p.state, p.exit_price = "closed", price or p.entry_price
        else:
            stop_fill = None
            if p.stop_order_id:
                try:
                    so = self.tc.get_order_by_id(p.stop_order_id)
                    if so.status == OrderStatus.FILLED:
                        stop_fill = float(so.filled_avg_price)
                    else:
                        self.tc.cancel_order_by_id(p.stop_order_id)
                        _time.sleep(1)
                except APIError:
                    pass
            if stop_fill is not None:
                p.exit_price = stop_fill
            else:
                try:
                    o = self.tc.close_position(p.symbol)
                    o = self._wait_fill(str(o.id), 30)
                    p.exit_price = float(o.filled_avg_price or price or p.entry_price)
                except APIError as e:
                    log.warning("  close_position: %s", e)
                    p.exit_price = price or p.entry_price
            p.state = "closed"
        p.note = reason
        p.events.append(f"sold @ {p.exit_price:.2f} ({reason})")
        if p.pnl < 0:
            self.losers += 1
        log.info("  P&L %+.2f (%.2fR)", p.pnl, p.r_multiple)

    def position_gone(self, p: Plan) -> bool:
        try:
            self.tc.get_open_position(p.symbol)
            return False
        except APIError:
            return True

    # ---------- the day ----------
    def run(self):
        cfg = self.cfg
        mode = "LIVE MONEY" if cfg.live else "PAPER"
        log.info("Latara waking up – %s mode%s", mode, " (dry run)" if cfg.dry_run else "")
        if not cfg.enabled:
            log.info("LATARA_ENABLED is off. Resting today.")
            return
        start = now_et()
        clock = self.tc.get_clock()
        today = start.date()
        if not clock.is_open:
            nxt = clock.next_open.astimezone(start.tzinfo)
            if nxt.date() != today:
                log.info("Market closed today. Next open %s.", nxt)
                return
            wait = (nxt - start).total_seconds() / 60
            if wait > cfg.max_wait_for_open_minutes:
                log.info("Open is %.0f min away – this is the off-season schedule slot, exiting.", wait)
                return
        if start > at(today, cfg.entry_cutoff):
            log.info("Past the entry cutoff – nothing to do.")
            return

        acct = self.tc.get_account()
        if acct.trading_blocked or acct.account_blocked:
            log.error("Account is blocked from trading.")
            return
        start_equity = float(acct.equity)
        settled = min(float(acct.cash), float(acct.non_marginable_buying_power))
        day_budget = max(0.0, min(settled, cfg.max_capital))
        log.info("Equity $%.2f, settled cash $%.2f, today's budget $%.2f", start_equity, settled, day_budget)
        if day_budget < 5:
            log.info("Not enough settled cash to trade today.")
            return

        bell = clock.next_close.astimezone(start.tzinfo) - timedelta(minutes=10)   # handles half days
        hard_exit = min(at(today, cfg.exit_time), bell, start + timedelta(minutes=cfg.max_runtime_minutes))
        log.info("Latara will be flat by %s ET", hard_exit.strftime("%H:%M"))
        scan_time = at(today, "09:35") + timedelta(seconds=20)
        while now_et() < scan_time:
            _time.sleep(5)

        past = self.past_trading_days(today)
        cands = scanner.scan(self.dc, self.sc, cfg, today, past)
        if cfg.require_spy_green and not scanner.spy_first_bar_green(self.dc, today, cfg.intraday_feed):
            log.info("SPY opened red – market filter says sit out today.")
            return self.finish(start_equity)
        good = []
        for c in rank(cands):
            ok, why = passes_filters(c, cfg)
            log.info("  %-5s relvol=%5.1fx gap=%+.1f%% atr=%.2f -> %s", c.symbol, c.rel_volume,
                     c.gap_pct * 100, c.atr, "IN PLAY" if ok else why)
            if ok:
                good.append(c)
        picks = good[:cfg.max_positions]
        if not picks:
            log.info("No stocks in play met the rules today. Sitting out is a valid trade.")
            return self.finish(start_equity)
        slot = day_budget / len(picks)
        self.plans = [build_plan(c, cfg, slot, day_budget) for c in picks]
        for p in self.plans:
            if p.qty <= 0:
                p.state, p.note = "skipped", "size too small"
            log.info("PLAN %s buy %s above %.2f, stop %.2f (relvol %.1fx)", p.symbol, p.qty, p.trigger, p.stop, p.rel_volume)

        cutoff = at(today, cfg.entry_cutoff)
        while now_et() < hard_exit and not self.halted:
            active = [p for p in self.plans if p.state in ("waiting", "open")]
            if not active:
                break
            px = self.prices([p.symbol for p in active])
            for p in active:
                price = px.get(p.symbol)
                if price is None:
                    continue
                p.last_price = price
                if p.state == "waiting":
                    if now_et() > cutoff:
                        p.state, p.note = "expired", "no breakout before cutoff"
                        continue
                    ok, _ = should_enter(p, price, cfg.max_entry_slippage)
                    if ok:
                        self.enter(p, price)
                elif p.state == "open":
                    if not cfg.dry_run and p.stop_order_id and self.position_gone(p):
                        self.exit(p, "broker stop hit", price)
                    elif should_stop_out(p, price):
                        self.exit(p, "stop", price)
                    elif maybe_breakeven(p, price, cfg.breakeven_at_r):
                        p.stop, p.breakeven_done = round(p.entry_price, 2), True
                        log.info("  %s stop raised to breakeven %.2f", p.symbol, p.stop)
                        self.place_stop(p)
            if not cfg.dry_run:
                eq = self.equity()
                if start_equity - eq >= day_budget * cfg.daily_loss_limit:
                    self.halted = f"daily loss limit hit (down ${start_equity - eq:.2f})"
            if self.losers >= cfg.max_losers:
                self.halted = f"{self.losers} losing trades"
            _time.sleep(cfg.poll_seconds)

        if self.halted:
            log.warning("HALT: %s", self.halted)
        for p in self.plans:
            if p.state == "open":
                self.exit(p, "end of day" if not self.halted else "halt")
            elif p.state == "waiting":
                p.state, p.note = "expired", "session over"
        if not cfg.dry_run:
            try:
                self.tc.cancel_orders()
            except APIError:
                pass
        return self.finish(start_equity)

    def finish(self, start_equity: float):
        realized = sum(p.pnl for p in self.plans)
        try:
            end_equity = self.equity()
        except Exception:
            end_equity = start_equity + realized
        day_pnl = end_equity - start_equity
        goal = self.cfg.daily_goal
        lines = [
            "## Latara – daily report",
            f"- Mode: {'LIVE' if self.cfg.live else 'paper'}",
            f"- Start equity: ${start_equity:,.2f} → end: ${end_equity:,.2f}",
            f"- Day P&L: **{day_pnl:+,.2f}** (goal ${goal:,.0f}: {'MET' if day_pnl >= goal else 'not met'})",
            "", "| Stock | Status | Entry | Exit | Qty | P&L | R |", "|---|---|---|---|---|---|---|",
        ]
        for p in self.plans:
            lines.append(f"| {p.symbol} | {p.state} {p.note} | {p.entry_price:.2f} | {p.exit_price:.2f} | "
                         f"{p.filled_qty} | {p.pnl:+.2f} | {p.r_multiple:+.2f} |")
        report = "\n".join(lines)
        log.info("\n%s", report)
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write(report + "\n")
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"trades-{now_et().date()}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "state", "note", "trigger", "stop", "entry", "exit", "qty", "pnl", "r", "relvol"])
            for p in self.plans:
                w.writerow([p.symbol, p.state, p.note, p.trigger, p.stop, p.entry_price, p.exit_price,
                            p.filled_qty, round(p.pnl, 2), round(p.r_multiple, 2), round(p.rel_volume, 2)])
        return day_pnl
