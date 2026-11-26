"""Simulate a whole trading day against a fake Alpaca to exercise the loop."""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import latara.trader as T
from latara import config
from latara.strategy import Candidate
from latara.timeutil import ET


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def test_full_day(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("LATARA_"):
            del os.environ[k]
    os.environ.update(ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", LATARA_DRY_RUN="true",
                      LATARA_LOG_DIR=str(tmp_path), LATARA_JOURNAL=str(tmp_path / "journal.csv"))
    monkeypatch.setattr(T, "LOG_DIR", tmp_path)
    clk = Clock(datetime(2026, 12, 1, 9, 10, tzinfo=ET))
    monkeypatch.setattr(T, "now_et", clk.now)
    monkeypatch.setattr(T._time, "sleep", clk.sleep)

    cfg = config.load()
    bot = T.Latara.__new__(T.Latara)
    bot.cfg, bot.plans, bot.losers, bot.halted = cfg, [], 0, ""
    open_t = datetime(2026, 12, 1, 9, 30, tzinfo=ET)
    bot.tc = NS(
        get_clock=lambda: NS(is_open=False, next_open=open_t,
                             next_close=datetime(2026, 12, 1, 16, 0, tzinfo=ET)),
        get_account=lambda: NS(trading_blocked=False, account_blocked=False, equity="100",
                               cash="100", non_marginable_buying_power="100"),
        cancel_orders=lambda: None,
    )
    bot.dc = bot.sc = None
    bot.past_trading_days = lambda d: []
    bot.equity = lambda: 100.0

    def fake_scan(*a, **k):
        return [Candidate("WIN", 2.0, 5e6, 20, 20, 21, 19.9, 20.8, 5e4, 5.0),
                Candidate("LOSE", 1.0, 5e6, 10, 10, 10.5, 9.9, 10.4, 5e4, 3.0),
                Candidate("RED", 1.0, 5e6, 10, 10, 10.5, 9.5, 9.6, 5e4, 9.0)]
    monkeypatch.setattr(T.scanner, "scan", fake_scan)

    def prices(syms):
        mins = (clk.t - open_t).total_seconds() / 60
        return {"WIN": 20.9 if mins < 10 else 21.05 + 0.01 * (mins - 10),
                "LOSE": 10.52 if mins < 20 else 10.3}
    bot.prices = prices

    pnl = bot.run()
    by = {p.symbol: p for p in bot.plans}
    assert set(by) == {"WIN", "LOSE"}                # RED candle filtered out
    assert by["WIN"].state == "closed" and by["WIN"].pnl > 0 and by["WIN"].note == "end of day"
    assert by["LOSE"].note == "stop" and by["LOSE"].pnl < 0
    assert clk.t <= datetime(2026, 12, 1, 15, 46, tzinfo=ET)
    assert list(tmp_path.glob("trades-*.csv"))
    assert (tmp_path / "journal.csv").read_text().count("\n") == 3   # header + 2 trades
