from datetime import date, datetime, time, timedelta

from latara import config, lab
from latara.lab import Bar, DEFAULTS, clamp, evaluate, learn, simulate_day
from latara.timeutil import ET


def day_bars(d, path, first=(20.0, 21.0, 19.9, 20.95), vol=50_000):
    """5-min bars: opening bar `first`, then closes following `path`."""
    t0 = datetime.combine(d, time(9, 30), ET)
    bars = [Bar(t0, *first, vol)]
    prev = first[3]
    for i, px in enumerate(path, 1):
        bars.append(Bar(t0 + timedelta(minutes=5 * i), prev, max(prev, px) + 0.02, min(prev, px) - 0.02, px, 10_000))
        prev = px
    while len(bars) < 78:
        bars.append(Bar(t0 + timedelta(minutes=5 * len(bars)), prev, prev + 0.01, prev - 0.01, prev, 5_000))
    return bars


INFO = {"WIN": (2.0, 5e6, 20.0), "LOSE": (2.0, 5e6, 20.0)}
HIST = {"WIN": [10_000] * 14, "LOSE": [10_000] * 14}


def test_clamp_keeps_values_in_bounds():
    p = clamp({"atr_stop_frac": 9, "min_rel_volume": -3, "max_positions": 50, "require_spy_green": 1})
    assert p["atr_stop_frac"] == 0.20 and p["min_rel_volume"] == 1.0 and p["max_positions"] == 2
    assert p["require_spy_green"] is True


def test_winner_and_loser_replay():
    d = date(2026, 12, 1)
    bars = {"WIN": day_bars(d, [21.2, 21.6, 22.0, 22.5]), "LOSE": day_bars(d, [21.1, 20.5, 20.0])}
    tr = {t["symbol"]: t for t in simulate_day(d, bars, INFO, HIST, True, dict(DEFAULTS))}
    assert tr["WIN"]["r"] > 0 and tr["LOSE"]["r"] < -0.9


def test_filters_spy_and_gap():
    d = date(2026, 12, 1)
    bars = {"WIN": day_bars(d, [21.2, 22])}
    assert simulate_day(d, bars, INFO, HIST, False, dict(DEFAULTS, require_spy_green=True)) == []
    assert simulate_day(d, bars, INFO, HIST, True, dict(DEFAULTS, min_gap=0.02)) == []   # 0% gap
    down = {"WIN": (2.0, 5e6, 21.0)}                       # opened ~5% below yesterday's close
    assert len(simulate_day(d, bars, down, HIST, True, dict(DEFAULTS))) == 1   # gap filter off -> still trades


def test_learning_only_adopts_what_holds_up_out_of_sample():
    # Every day: tight stops get shaken out, wide stops ride the trend -> wider stop should win everywhere
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(30)]
    data = {}
    for d in days:
        bars = {"WIN": day_bars(d, [21.1, 20.85, 21.4, 21.9, 22.5, 23.0])}
        data[d] = (bars, INFO, HIST, True)
    cur = dict(DEFAULTS, atr_stop_frac=0.05)
    chosen, ev = learn(days[:21], days[21:], data, cur)
    assert chosen["atr_stop_frac"] > 0.05 and ev["decision"].startswith("ADOPTED")
    # if the recent unseen weeks reverse, Latara keeps her rules
    data2 = dict(data)
    for d in days[21:]:
        data2[d] = ({"WIN": day_bars(d, [21.1, 20.0, 19.0])}, INFO, HIST, True)
    chosen2, ev2 = learn(days[:21], days[21:], data2, cur)
    assert chosen2 == cur and ev2["decision"].startswith("kept")


def test_config_reads_brain(tmp_path, monkeypatch):
    import json, os
    for k in list(os.environ):
        if k.startswith("LATARA_"):
            monkeypatch.delenv(k)
    assert config.load().atr_stop_frac == 0.10          # shipped brain.json
    monkeypatch.setenv("LATARA_ATR_STOP_FRAC", "0.15")  # repo variable overrides brain
    assert config.load().atr_stop_frac == 0.15
