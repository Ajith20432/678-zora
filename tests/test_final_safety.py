from datetime import datetime, timezone

import bot
import config
import db
from config import CFG
from risk import RiskEngine


def test_daily_loss_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "db_path", str(tmp_path / "paper.db"))
    monkeypatch.setattr(CFG, "starting_capital", 1000.0)
    monkeypatch.setattr(CFG, "max_daily_loss_pct", 5.0)
    monkeypatch.setattr(CFG, "max_drawdown_pct", 50.0)
    conn = db.connect(mode="paper")
    db.log_equity(conn, 1000.0)
    db.log_equity(conn, 940.0)
    today = datetime.now(timezone.utc).date().isoformat()
    db.save_risk_state(conn, daily_date=today, daily_start=1000.0, peak_equity=1000.0,
                       kill_switch_tripped=True, drawdown_tripped=False)
    _, risk, _ = bot.restore_state(conn, ("ETH/USDT",), mode="paper")
    assert risk.daily_start == 1000.0
    assert risk.kill_switch_tripped
    conn.close()


def test_daily_reset_clears_daily_lock_but_not_drawdown_lock(monkeypatch):
    monkeypatch.setattr(CFG, "max_daily_loss_pct", 5.0)
    monkeypatch.setattr(CFG, "max_drawdown_pct", 10.0)
    r = RiskEngine(1000)
    r.update_equity(940)
    assert r.kill_switch_tripped
    assert r._daily_loss_tripped and not r._drawdown_tripped
    r.reset_daily()
    assert not r.kill_switch_tripped
    r.update_equity(800)
    assert r._drawdown_tripped
    r.reset_daily()
    assert r.kill_switch_tripped


def test_legacy_db_without_risk_state_uses_first_equity_of_today(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "db_path", str(tmp_path / "legacy.db"))
    monkeypatch.setattr(CFG, "starting_capital", 1000.0)
    monkeypatch.setattr(CFG, "max_daily_loss_pct", 5.0)
    monkeypatch.setattr(CFG, "max_drawdown_pct", 50.0)
    conn = db.connect(mode="paper")
    now = datetime.now(timezone.utc).timestamp()
    conn.execute("INSERT INTO equity_curve(ts,equity) VALUES(?,?)", (now - 10, 1000.0))
    conn.execute("INSERT INTO equity_curve(ts,equity) VALUES(?,?)", (now, 940.0))
    conn.commit()
    _, risk, _ = bot.restore_state(conn, ("ETH/USDT",), mode="paper")
    assert risk.daily_start == 1000.0
    assert risk.kill_switch_tripped


def test_live_and_paper_databases_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "db_path", str(tmp_path / "zora.db"))
    monkeypatch.setattr(CFG, "live_db_path", "")
    paper = db.connect(mode="paper")
    live = db.connect(mode="live")
    db.save_open_position(paper, {"symbol":"ETH/USDT","action":"BUY","entry":100,"size":1,
                                 "stop":95,"take":110,"mode":"paper"})
    db.save_open_position(live, {"symbol":"ETH/USDT","action":"BUY","entry":200,"size":1,
                                 "stop":190,"take":220,"mode":"live"})
    assert db.load_open_positions(paper)[0]["mode"] == "paper"
    assert db.load_open_positions(live)[0]["mode"] == "live"
    assert db.resolve_db_path("paper") != db.resolve_db_path("live")
