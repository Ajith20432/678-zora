"""End-to-end paper-mode tests for bot.Trader using a fake exchange and canned candles (no network)."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import bot
import config
import data
import db
import guards
from brain import AdaptiveBrain
from config import CFG


def _frame(closes, last_ts=None):
    closes = np.asarray(closes, float)
    n = len(closes)
    end = last_ts or pd.Timestamp.now(tz="UTC").floor("15min")
    ts = pd.date_range(end=end, periods=n, freq="15min")
    return pd.DataFrame({"ts": ts, "open": closes, "high": closes * 1.001, "low": closes * 0.999,
                         "close": closes, "volume": 100.0})


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(bot, "BRAIN", AdaptiveBrain(state_path=tmp_path / "brain.json"))
    for k, v in dict(symbols=("ETH/USDT",), timeframes=("15m",), btc_context_symbol="", starting_capital=1000.0,
                     risk_per_trade_pct=1.0, max_exposure_pct=50.0, max_daily_loss_pct=50.0, max_drawdown_pct=90.0,
                     max_position_notional_pct=100.0, atr_stop_mult=2.0, atr_take_mult=3.0, trailing_stop_enabled=False,
                     fear_greed_enabled=False, ml_enabled=False, telegram_bot_token="", telegram_chat_id="",
                     telegram_heartbeat_minutes=0, slippage_bps=0.0, latency_bps=0.0, fee_rate=0.0,
                     groq_api_key="", openrouter_api_key="", gemini_api_key="", anthropic_api_key="", openai_api_key="",
                     degradation_window=20, tv_signal_path=str(tmp_path / "tv.json")).items():
        monkeypatch.setattr(CFG, k, v)
    import news_feed
    monkeypatch.setattr(news_feed, "news_sentiment", lambda s: (0, []))


class FakeFeed:
    """Replaces data.fetch_multi_timeframe with a controllable price path."""
    def __init__(self, closes):
        self.closes = list(closes)

    def __call__(self, exchange, symbol, tfs):
        return {tf: _frame(self.closes) for tf in tfs}


def _trader(monkeypatch, feed, action="BUY"):
    monkeypatch.setattr(data, "fetch_multi_timeframe", feed)
    import strategy
    monkeypatch.setattr(strategy, "generate_signal",
                        lambda mtf, **k: SimpleNamespace(action=action, confidence=80, reasons=["t"], expert_scores={"15m": 80},
                                                         factor_scores_by_tf={"15m": {"trend": 30}}))
    return bot.Trader(live=False, exchange=object())


def _wave(n=120, base=100.0):
    return [base + 1.5 * np.sin(i / 3.0) for i in range(n)]


def test_paper_cycle_opens_a_position_with_stop_and_take_and_persists_it(monkeypatch):
    t = _trader(monkeypatch, FakeFeed(_wave()))
    t.cycle()
    p = t.positions["ETH/USDT"]
    assert p and p["stop"] < p["entry"] < p["take"]
    assert db.load_open_positions(t.conn)[0]["symbol"] == "ETH/USDT"
    assert t.risk.open_exposure == CFG.risk_per_trade_pct


def test_stop_hit_closes_the_position_updates_equity_and_frees_exposure(monkeypatch):
    feed = FakeFeed(_wave())
    t = _trader(monkeypatch, feed)
    t.cycle()
    p = t.positions["ETH/USDT"]
    feed.closes = feed.closes[:-1] + [p["stop"] * 0.98]      # price gaps through the stop
    t.paused = True                                          # don't re-enter right away
    t.cycle()
    assert t.positions["ETH/USDT"] is None
    assert t.equity < 1000.0
    assert t.risk.open_exposure == 0
    assert db.load_open_positions(t.conn) == []
    assert db.summary(t.conn)["closed_trades"] == 1


def test_take_profit_closes_with_a_gain(monkeypatch):
    feed = FakeFeed(_wave())
    t = _trader(monkeypatch, feed)
    t.cycle()
    p = t.positions["ETH/USDT"]
    feed.closes = feed.closes[:-1] + [p["take"] * 1.01]
    t.paused = True
    t.cycle()
    assert t.equity > 1000.0 and t.positions["ETH/USDT"] is None


def test_paused_blocks_entries_and_logs_the_reason(monkeypatch):
    t = _trader(monkeypatch, FakeFeed(_wave()))
    t.paused = True
    t.cycle()
    assert t.positions["ETH/USDT"] is None
    assert "paused" in db.recent_decisions(t.conn)[-1]["reason"]


def test_stale_data_skips_the_cycle_entirely(monkeypatch):
    old = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta(hours=6)
    monkeypatch.setattr(data, "fetch_multi_timeframe", lambda e, s, tfs: {tf: _frame(_wave(), old) for tf in tfs})
    t = bot.Trader(live=False, exchange=object())
    t.cycle()
    assert t.positions["ETH/USDT"] is None and db.recent_decisions(t.conn) == []


def test_kill_switch_command_blocks_entries(monkeypatch):
    t = _trader(monkeypatch, FakeFeed(_wave()))
    assert "KILL" in t.handle_command("/kill")
    t.cycle()
    assert t.positions["ETH/USDT"] is None


def test_pause_resume_and_unknown_commands(monkeypatch):
    t = _trader(monkeypatch, FakeFeed(_wave()))
    t.handle_command("/pause")
    assert t.paused
    t.handle_command("/resume")
    assert not t.paused
    assert "unknown" in t.handle_command("/nope")
    assert "no open positions" in t.handle_command("/positions")


def test_restart_restores_the_open_position_and_keeps_managing_it(monkeypatch):
    feed = FakeFeed(_wave())
    t = _trader(monkeypatch, feed)
    t.cycle()
    t2 = bot.Trader(live=False, exchange=object())
    assert t2.positions["ETH/USDT"]["entry"] == t.positions["ETH/USDT"]["entry"]
    assert t2.risk.open_exposure == CFG.risk_per_trade_pct


def test_correlated_sizing_and_degradation_guard_helpers():
    assert guards.correlation_regime(0.1) == "broken"
    assert bot.entry_blockers(signal_action="BUY", paused=False, live=False, stale=None, quality=None, degraded=None) == []


class _LiveEx:
    """Records calls; stands in for an authenticated exchange with native protection support."""
    id = "fake"
    options = {"createMarketBuyOrderRequiresPrice": False}
    has = {"fetchBalance": True}

    def __init__(self, statuses=None):
        self.orders, self.cancelled, self.statuses = [], [], statuses or {}

    def market(self, symbol):
        return {"spot": True, "limits": {"amount": {"min": 0.0001}}}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.4f}"

    def featureValue(self, *a):
        return True

    def fetch_open_orders(self, symbol):
        return []

    def fetch_balance(self):
        return {"total": {"ETH": 0.0}}

    def fetch_ticker(self, symbol):
        return {"bid": 99.99, "ask": 100.01, "quoteVolume": 5e9}

    def create_order(self, symbol, kind, side, amount, price, params):
        self.orders.append((side, amount, params))
        return {"id": f"o{len(self.orders)}", "average": 100.0}

    def fetch_order(self, oid, symbol):
        return {"status": self.statuses.get(oid, "open")}

    def cancel_order(self, oid, symbol):
        self.cancelled.append(oid)


def test_live_entry_arms_native_protection_and_a_native_fill_cancels_the_sibling_order(monkeypatch):
    from execution import LiveExecutor
    monkeypatch.setattr(config.CFG, "native_protection_required", True)
    monkeypatch.setattr(config.CFG, "reconcile_on_start", False)
    feed = FakeFeed(_wave())
    monkeypatch.setattr(data, "fetch_multi_timeframe", feed)
    import strategy
    monkeypatch.setattr(strategy, "generate_signal",
                        lambda mtf, **k: SimpleNamespace(action="BUY", confidence=80, reasons=["t"], expert_scores={"15m": 80},
                                                         factor_scores_by_tf={"15m": {"trend": 30}}))
    ex = _LiveEx()
    t = bot.Trader(live=True, exchange=ex, executor=LiveExecutor())
    t.cycle()
    p = t.positions["ETH/USDT"]
    assert p and set(p["protection"]) == {"stop", "take"} and len(ex.orders) == 3   # entry + stop + take
    ex.statuses[p["protection"]["stop"]["id"]] = "closed"                           # exchange filled the stop
    t.paused = True
    t.cycle()
    assert t.positions["ETH/USDT"] is None
    assert ex.cancelled == [p["protection"]["take"]["id"]]                          # the other leg was cancelled


def test_live_entry_is_refused_when_native_protection_is_required_but_unsupported(monkeypatch):
    from execution import LiveExecutor
    monkeypatch.setattr(config.CFG, "native_protection_required", True)
    monkeypatch.setattr(data, "fetch_multi_timeframe", FakeFeed(_wave()))
    import strategy
    monkeypatch.setattr(strategy, "generate_signal",
                        lambda mtf, **k: SimpleNamespace(action="BUY", confidence=80, reasons=["t"], expert_scores={"15m": 80},
                                                         factor_scores_by_tf={"15m": {"trend": 30}}))
    ex = _LiveEx()
    ex.featureValue = lambda *a: False
    t = bot.Trader(live=True, exchange=ex, executor=LiveExecutor())
    t.cycle()
    assert t.positions["ETH/USDT"] is None and ex.orders == []
