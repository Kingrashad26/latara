import os

from latara import config
from latara.strategy import (Candidate, DailyBar, Plan, atr, build_plan, maybe_breakeven,
                             passes_filters, position_size, rank, relative_volume,
                             should_enter, should_stop_out)


def cfg(**env):
    for k in list(os.environ):
        if k.startswith("LATARA_"):
            del os.environ[k]
    os.environ.update(env)
    return config.load()


def test_paper_is_default_and_live_needs_confirmation():
    assert cfg().live is False
    assert cfg(LATARA_MODE="live").live is False
    assert cfg(LATARA_MODE="live", LATARA_CONFIRM_LIVE="i-understand-this-is-real-money").live is True


def test_atr():
    bars = [DailyBar(11, 9, 10, 1), DailyBar(12, 10, 11, 1), DailyBar(11.5, 10.5, 11, 1)]
    assert abs(atr(bars, 14) - 1.5) < 1e-9   # TRs: 2, 1


def test_relative_volume():
    assert relative_volume(300, [100, 100, 100]) == 3
    assert relative_volume(300, []) == 0


def good(**kw):
    c = Candidate("ABC", atr=2.0, avg_volume=5e6, prev_close=20, or_open=20, or_high=21,
                  or_low=19.9, or_close=20.8, or_volume=50_000, rel_volume=4.0)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_filters():
    c = cfg()
    assert passes_filters(good(), c)[0]
    assert not passes_filters(good(or_close=19.5), c)[0]      # red candle
    assert not passes_filters(good(rel_volume=0.5), c)[0]
    assert not passes_filters(good(prev_close=3), c)[0]
    assert not passes_filters(good(atr=0.2), c)[0]
    assert passes_filters(good(prev_close=21), c)[0]           # gap-down allowed when gap rule off


def test_rank():
    assert [x.symbol for x in rank([good(symbol="A", rel_volume=2), good(symbol="B", rel_volume=9)])] == ["B", "A"]


def test_sizing_small_account():
    # $50 slot, $21 stock -> capital-limited, whole shares (2)
    assert position_size(50, 21.0, 0.2, 2.0) == 2
    # $50 slot, $300 stock -> fractional
    q = position_size(50, 300.0, 1.0, 2.0)
    assert 0 < q < 1 and q * 300 <= 50
    # risk-limited
    assert position_size(10_000, 10.0, 1.0, 5.0) == 5
    assert position_size(0.5, 10.0, 1.0, 5.0) == 0     # under $1 minimum


def test_plan_and_signals():
    c = cfg()
    p = build_plan(good(), c, slot_budget=50, day_budget=100)
    assert p.trigger == 21.01 and p.stop == round(21.01 - 0.2, 2)
    assert p.qty * p.trigger <= 50
    assert not should_enter(p, 20.9, 0.005)[0]
    assert should_enter(p, 21.05, 0.005)[0]
    assert not should_enter(p, 21.5, 0.005)[0]          # chased too far
    p.state, p.entry_price, p.filled_qty = "open", 21.05, 2
    assert should_stop_out(p, 20.80)
    assert not should_stop_out(p, 20.90)
    assert maybe_breakeven(p, 21.6, 2.0)
    p.state, p.exit_price = "closed", 22.05
    assert abs(p.pnl - 2.0) < 1e-9
