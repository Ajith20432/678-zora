"""risk.py — the independent risk engine. It can veto any signal; nothing upstream can override it.

Exposure units: every open position counts as one *risk budget* (RISK_PER_TRADE_PCT, scaled by any
size multiplier), and MAX_EXPOSURE_PCT caps the sum across all symbols. Hidden leverage is prevented
separately by MAX_POSITION_NOTIONAL_PCT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config import CFG


@dataclass
class Decision:
    approved: bool
    reason: str
    position_size: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    exposure_pct: float = 0.0   # risk budget this trade consumes; pass it back to remove_exposure() on close
    notional_pct: float = 0.0   # position value as % of equity; pass it back to remove_exposure() on close


class RiskEngine:
    def __init__(self, equity: float):
        self.equity = float(equity)
        self.peak_equity = self.equity
        self.daily_start = self.equity
        self.open_exposure = 0.0
        self.open_notional_pct = 0.0
        self._daily_loss_tripped = False
        self._drawdown_tripped = False

    @property
    def kill_switch_tripped(self) -> bool:
        return self._daily_loss_tripped or self._drawdown_tripped

    @kill_switch_tripped.setter
    def kill_switch_tripped(self, value: bool) -> None:
        # Backward-compatible setter: a restored legacy kill state is treated as a hard/drawdown lock.
        self._drawdown_tripped = bool(value)

    def update_equity(self, equity: float) -> None:
        self.equity = float(equity)
        self.peak_equity = max(self.peak_equity, self.equity)
        if self.equity <= 0:
            self._drawdown_tripped = True
            return
        drawdown = (self.peak_equity - self.equity) / self.peak_equity * 100 if self.peak_equity > 0 else 0.0
        daily_loss = (self.daily_start - self.equity) / self.daily_start * 100 if self.daily_start > 0 else 0.0
        if drawdown >= CFG.max_drawdown_pct or daily_loss >= CFG.max_daily_loss_pct:
            if drawdown >= CFG.max_drawdown_pct:
                self._drawdown_tripped = True
            if daily_loss >= CFG.max_daily_loss_pct:
                self._daily_loss_tripped = True

    def reset_daily(self) -> None:
        self.daily_start = self.equity
        # Daily loss is a per-session/day guard; it must clear only at the next UTC day.
        self._daily_loss_tripped = False

    def restore_trip_state(self, *, daily_loss_tripped=False, drawdown_tripped=False) -> None:
        self._daily_loss_tripped = bool(daily_loss_tripped)
        self._drawdown_tripped = bool(drawdown_tripped)

    def add_exposure(self, pct: float, notional_pct: float = 0.0) -> None:
        self.open_exposure = max(0.0, self.open_exposure + float(pct))
        self.open_notional_pct = max(0.0, self.open_notional_pct + float(notional_pct))

    def remove_exposure(self, pct: float, notional_pct: float = 0.0) -> None:
        self.open_exposure = max(0.0, self.open_exposure - float(pct))
        self.open_notional_pct = max(0.0, self.open_notional_pct - float(notional_pct))

    def evaluate(self, action: str, entry: float, atr: float, size_multiplier: float = 1.0) -> Decision:
        if action not in ("BUY", "SELL"):
            return Decision(False, "no actionable signal")
        if self.kill_switch_tripped:
            return Decision(False, "kill switch is tripped")
        try:
            entry, atr = float(entry), float(atr)
        except (TypeError, ValueError):
            return Decision(False, "invalid price or ATR")
        if not (math.isfinite(entry) and math.isfinite(atr)) or entry <= 0 or atr <= 0:
            return Decision(False, "invalid price or ATR")
        if self.equity <= 0:
            return Decision(False, "no equity")

        mult = min(1.0, max(0.0, float(size_multiplier)))
        stop_dist = atr * CFG.atr_stop_mult
        stop = entry - stop_dist if action == "BUY" else entry + stop_dist
        take = entry + atr * CFG.atr_take_mult if action == "BUY" else entry - atr * CFG.atr_take_mult
        if action == "BUY" and stop <= 0:
            return Decision(False, "stop distance larger than price")

        size = (self.equity * CFG.risk_per_trade_pct / 100) / stop_dist
        size = min(size, self.equity * CFG.max_position_notional_pct / 100 / entry)   # no hidden leverage
        size *= mult
        if size <= 0:
            return Decision(False, "position size is zero")

        added = CFG.risk_per_trade_pct * mult
        if self.open_exposure + added > CFG.max_exposure_pct + 1e-9:
            return Decision(False, "max exposure limit reached")
        notional = size * entry / self.equity * 100
        if self.open_notional_pct + notional > CFG.max_total_notional_pct + 1e-9:
            return Decision(False, "total notional limit reached (no leverage)")
        return Decision(True, "approved", size, stop, take, added, notional)


def manage_stop(p: dict, price: float, atr: float) -> float | None:
    """Breakeven then ATR-trail. Returns a new (tighter) stop or None. Never loosens a stop."""
    if not CFG.trailing_stop_enabled:
        return None
    try:
        entry = float(p["entry"])
        action = p["action"]
        current = float(p.get("stop") or p.get("initial_stop"))
        initial = float(p.get("initial_stop") or current)
        price, atr = float(price), float(atr)
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(price) and math.isfinite(atr)) or atr <= 0:
        return None
    r = abs(entry - initial)
    if r <= 0:
        return None
    reached = CFG.breakeven_at_r * r
    if action == "BUY":
        if price - entry < reached:
            return None
        candidate = max(entry, price - atr * CFG.trail_atr_mult)
        return candidate if candidate > current else None
    if entry - price < reached:
        return None
    candidate = min(entry, price + atr * CFG.trail_atr_mult)
    return candidate if candidate < current else None
