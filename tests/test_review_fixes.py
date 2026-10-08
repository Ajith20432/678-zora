"""Regression tests for the v1.2.2 review fixes. Each one fails on the v1.2.1 code it guards against."""
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import bot
import ccxt
import dashboard
import execution
import guards
import news_feed
from config import CFG
from execution import LiveExecutor
from multi_ai import parse_verdict
from risk import RiskEngine


class _LostReply:
    """Exchange where the first market order is filled but the reply is lost (network error)."""
    def __init__(self):
        self.sent = []
    def market(self, s):
        return {"spot": True, "limits": {"amount": {"min": 0.0}}}
    def amount_to_precision(self, s, a):
        return a
    def create_order(self, symbol, typ, side, amount, price, params):
        self.sent.append((side, amount))
        raise ccxt.NetworkError("read timeout")


def test_market_order_is_never_resent_after_a_network_error(monkeypatch):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)
    ex = _LostReply()
    lx = LiveExecutor(telegram_alert=lambda m: None)
    assert lx.open(ex, "ETH/USDT", "BUY", 0.5, 2000.0) is None
    assert len(ex.sent) == 1          # v1.2.1 sent it twice: a double fill
    assert lx.unknown_state is True


class _Deriv:
    has = {"fetchPositions": True}
    def market(self, s):
        return {"spot": False}
    def fetch_open_orders(self, s):
        return []
    def fetch_positions(self, syms):
        return [{"contracts": 0.0, "contractSize": 1.0}, {"contracts": 2.0, "contractSize": 1.0}]


def test_reconcile_reports_the_real_open_position_not_a_flat_one():
    r = LiveExecutor().reconcile(_Deriv(), "ETH/USDT:USDT")
    assert r["safe"] is True
    assert r["position"]["contracts"] == 2.0


def test_verdict_that_is_not_an_object_is_handled_not_crashing():
    assert parse_verdict("42")[0] == "HOLD"
    assert parse_verdict('["BUY"]')[0] == "HOLD"
    assert parse_verdict('{"lean":"BUY","confidence":70,"note":"ok"}') == ("BUY", 70, "ok")


def test_forming_candle_is_dropped_and_closed_candle_kept():
    now = 1_000_000.0
    period = 900
    base = pd.Timestamp(now - 100, unit="s", tz="UTC")  # opened 100s ago: still forming on a 15m chart
    df = pd.DataFrame({"ts": [base - pd.Timedelta(seconds=period), base]})
    out = guards.drop_forming_candle(df, "15m", now=now)
    assert len(out) == 1
    closed = pd.DataFrame({"ts": [pd.Timestamp(now - 2000, unit="s", tz="UTC")]})
    assert len(guards.drop_forming_candle(closed, "15m", now=now)) == 1


def test_total_notional_cap_stops_hidden_leverage(monkeypatch):
    monkeypatch.setattr(CFG, "max_exposure_pct", 1000.0)     # isolate the notional check
    monkeypatch.setattr(CFG, "max_total_notional_pct", 100.0)
    monkeypatch.setattr(CFG, "risk_per_trade_pct", 0.5)
    engine = RiskEngine(1000.0)
    approved = 0
    for _ in range(10):
        d = engine.evaluate("BUY", 100.0, 1.0)     # 25% of equity notional each
        if d.approved:
            engine.add_exposure(d.exposure_pct, d.notional_pct)
            approved += 1
    assert approved == 4                            # v1.2.1 approved 6 here (150% notional)
    assert engine.open_notional_pct <= 100.0 + 1e-6
    assert "notional" in engine.evaluate("BUY", 100.0, 1.0).reason


def test_remove_exposure_releases_notional():
    engine = RiskEngine(1000.0)
    engine.add_exposure(0.5, 25.0)
    engine.remove_exposure(0.5, 25.0)
    assert engine.open_notional_pct == 0.0


def test_restart_recomputes_notional_from_positions():
    risk = RiskEngine(1000.0)
    bot.sync_exposure(risk, {"ETH/USDT": {"size": 2.0, "entry": 100.0, "exposure_pct": 0.5}})
    assert risk.open_notional_pct == pytest.approx(20.0)


def test_dashboard_receives_live_prices(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard, "publish", lambda **kw: captured.update(kw))
    fake = SimpleNamespace(mode="paper", paused=False, equity=1000.0, last_signal={}, btc_note="n/a",
                           risk=SimpleNamespace(kill_switch_tripped=False, open_exposure=0.0))
    bot.Trader._publish(fake, {"ETH/USDT": 2123.45})
    assert captured["prices"] == {"ETH/USDT": 2123.45}


def test_news_feed_backs_off_when_all_feeds_fail(monkeypatch):
    calls = []
    monkeypatch.setattr(news_feed, "_fetch_headlines", lambda: calls.append(1) or [])
    monkeypatch.setitem(news_feed._cache, "headlines", [])
    monkeypatch.setitem(news_feed._cache, "failed_at", 0.0)
    news_feed.get_headlines()
    news_feed.get_headlines()
    news_feed.get_headlines()
    assert len(calls) == 1            # v1.2.1 retried both feeds on every cycle


def test_dashboard_container_keeps_trading_state_on_a_volume():
    text = open("docker-compose.yml").read()
    assert "DB_PATH: /data/zora.db" in text
    assert "name: zora_data" in text
