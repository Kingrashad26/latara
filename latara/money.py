"""How much money Latara trades with: she manages this herself.

Her capital starts at LATARA_STARTING_CAPITAL ($100) and compounds with her
own results. Every closed trade's profit or loss in her journal grows or
shrinks the amount she trades the next day. Paper and live money are tracked
separately, so the day you switch to live she starts fresh at her starting
capital instead of inheriting paper profits.

Automatic limits (all changeable by you with repo Variables):
  * never more than the account's settled cash
  * never more than LATARA_CAPITAL_CEILING ($10,000 by default)
  * drawdown brake: if her capital falls 20%+ below its best level, she trades
    at half size until she recovers
  * if you set LATARA_MAX_CAPITAL, that fixed amount wins and compounding stops
"""
from __future__ import annotations

import csv
from pathlib import Path

DRAWDOWN_BRAKE = 0.20


def capital_from_journal(path: Path, mode: str, starting: float) -> tuple[float, float]:
    """Return (current capital, peak capital) from closed trades in this mode."""
    cap = peak = starting
    if not path.exists():
        return cap, peak
    by_day: dict[str, float] = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r.get("mode") == mode and r.get("state") == "closed":
                by_day[r["date"]] = by_day.get(r["date"], 0.0) + float(r.get("pnl") or 0)
    for d in sorted(by_day):
        cap += by_day[d]
        peak = max(peak, cap)
    return cap, peak


def todays_capital(cfg, journal_path: Path) -> tuple[float, str]:
    if cfg.fixed_capital:
        return cfg.max_capital, f"fixed at ${cfg.max_capital:,.2f} by you (LATARA_MAX_CAPITAL)"
    mode = "live" if cfg.live else "paper"
    cap, peak = capital_from_journal(journal_path, mode, cfg.starting_capital)
    note = f"compounding: started ${cfg.starting_capital:,.2f}, now ${cap:,.2f} (best ${peak:,.2f})"
    use = cap
    if peak > 0 and cap < peak * (1 - DRAWDOWN_BRAKE):
        use = cap * 0.5
        note += f"; drawdown brake on, trading half size (${use:,.2f})"
    if use > cfg.capital_ceiling:
        use = cfg.capital_ceiling
        note += f"; capped at ceiling ${cfg.capital_ceiling:,.2f}"
    return max(0.0, use), note
