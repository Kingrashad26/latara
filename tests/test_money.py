import csv
import os

from latara import config
from latara.lab import DEFAULTS, apply_loss_limits
from latara.money import todays_capital


def cfg(monkeypatch, **env):
    for k in list(os.environ):
        if k.startswith("LATARA_"):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.load()


def journal(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "mode", "symbol", "state", "pnl"])
        w.writerows(rows)


def test_capital_compounds_with_her_results(tmp_path, monkeypatch):
    j = tmp_path / "j.csv"
    journal(j, [["2026-12-01", "paper", "A", "closed", "12.5"], ["2026-12-02", "paper", "B", "closed", "-2.5"],
                ["2026-12-02", "paper", "C", "expired", "0"]])
    cap, note = todays_capital(cfg(monkeypatch), j)
    assert cap == 110.0 and "compounding" in note


def test_live_starts_fresh_and_paper_profits_dont_carry_over(tmp_path, monkeypatch):
    j = tmp_path / "j.csv"
    journal(j, [["2026-12-01", "paper", "A", "closed", "500"]])
    c = cfg(monkeypatch, LATARA_MODE="live", LATARA_CONFIRM_LIVE="i-understand-this-is-real-money")
    assert todays_capital(c, j)[0] == 100.0


def test_drawdown_brake_and_ceiling(tmp_path, monkeypatch):
    j = tmp_path / "j.csv"
    journal(j, [["2026-12-01", "paper", "A", "closed", "100"], ["2026-12-02", "paper", "B", "closed", "-80"]])
    cap, note = todays_capital(cfg(monkeypatch), j)      # peak 200 -> now 120 (40% down) -> half size
    assert cap == 60.0 and "brake" in note
    journal(j, [["2026-12-01", "paper", "A", "closed", "50000"]])
    assert todays_capital(cfg(monkeypatch), j)[0] == 10_000.0


def test_your_fixed_amount_wins(tmp_path, monkeypatch):
    j = tmp_path / "j.csv"
    journal(j, [["2026-12-01", "paper", "A", "closed", "100"]])
    assert todays_capital(cfg(monkeypatch, LATARA_MAX_CAPITAL="250"), j)[0] == 250.0


def test_paper_is_still_default(monkeypatch):
    assert cfg(monkeypatch).live is False


def test_replay_respects_loss_limits():
    t = lambda name, ent, ext, ret: {"symbol": name, "entry_t": ent, "exit_t": ext, "ret": ret}
    trades = [t("A", 1, 2, -0.01), t("B", 3, 5, 0.02)]
    assert [x["symbol"] for x in apply_loss_limits(trades, dict(DEFAULTS, max_losers=1))] == ["A"]
    assert [x["symbol"] for x in apply_loss_limits(trades, dict(DEFAULTS, max_losers=2))] == ["A", "B"]
