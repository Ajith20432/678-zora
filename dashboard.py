"""ZORA read-only dashboard backed by the real SQLite database.

Two interchangeable front ends serve the same JSON: the FastAPI app (`python bot.py dashboard`, the
production interface) and a tiny stdlib server (make_server / start_background) that runs inside the
trading process on Termux without extra dependencies. No HTTP endpoint can place a trade; the only
write path is the optional, secret-protected TradingView webhook, which can only cast one expiring vote.
"""
# NOTE: no `from __future__ import annotations` here: FastAPI resolves route annotations (e.g. Request) at def time.
import json
import logging
import mimetypes
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import db
import tv_signal
from config import CFG
from metrics import compute_metrics

logger = logging.getLogger("zora.dashboard")
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "0"

SHARED: dict = {}
_LOCK = threading.Lock()


def publish(**values):
    """Called by the trading loop to expose live (non-persisted) state such as the latest signal."""
    with _LOCK:
        SHARED.update(values)


def _shared_copy():
    with _LOCK:
        return dict(SHARED)


def _open_readonly(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _finite(v):
    return None if isinstance(v, float) and v in (float("inf"), float("-inf")) else v


def _ai_safety_status():
    try:
        import multi_ai
        return multi_ai.ai_safety_status()
    except Exception as e:
        return {"configured": False, "entry_blocked": False, "reason": f"unavailable: {e}"}


def snapshot_state(conn, brain_path="brain_state.json", shared=None):
    closed = db.recent_closed_pnls(conn, 500)
    equity = db.equity_series(conn, 500)
    try:
        positions = db.load_open_positions(conn)
    except sqlite3.DatabaseError:
        positions = []
    try:
        with open(brain_path) as f:
            brain = json.load(f)
    except (OSError, ValueError):
        brain = {}
    metrics = {k: _finite(v) for k, v in compute_metrics(closed, [e for _, e in equity] or None, CFG.starting_capital).items()}
    metrics["peak_equity"] = db.peak_equity(conn)
    return {
        "generated": time.time(),
        "summary": db.summary(conn),
        "metrics": metrics,
        "equity": equity,
        "positions": [{k: p.get(k) for k in ("symbol", "action", "entry", "size", "stop", "take", "mode")} for p in positions],
        "trades": db.recent_trades(conn, 20),
        "decisions": db.recent_decisions(conn, 15),
        "brain": {"expert_weights": brain.get("expert_weights", {}), "factor_weights": brain.get("factor_weights", {}),
                  "trades_learned_from": brain.get("trades_learned_from", 0)},
        "live": shared or {},
        "ai_safety": _ai_safety_status(),
        "config": {
            "symbol": CFG.symbol, "symbols": list(CFG.symbols), "exchange": CFG.exchange_id,
            "supported_exchanges": list(CFG.supported_exchanges), "risk_per_trade_pct": CFG.risk_per_trade_pct,
            "max_exposure_pct": CFG.max_exposure_pct, "max_position_notional_pct": CFG.max_position_notional_pct,
            "max_drawdown_pct": CFG.max_drawdown_pct, "max_daily_loss_pct": CFG.max_daily_loss_pct,
            "atr_stop_mult": CFG.atr_stop_mult, "atr_take_mult": CFG.atr_take_mult,
            "trailing_stop_enabled": CFG.trailing_stop_enabled, "trail_atr_mult": CFG.trail_atr_mult,
            "breakeven_at_r": CFG.breakeven_at_r, "stale_data_max_candles": CFG.stale_data_max_candles,
            "max_spread_bps": CFG.max_spread_bps, "min_quote_volume": CFG.min_quote_volume,
            "degradation_min_profit_factor": CFG.degradation_min_profit_factor,
            "degradation_window": CFG.degradation_window, "fee_rate": CFG.fee_rate,
            "slippage_bps": CFG.slippage_bps, "native_protection_required": CFG.native_protection_required,
            "reconcile_on_start": CFG.reconcile_on_start, "mode": "live" if CFG.live_allowed() else "paper"
        },
    }


def _state_json():
    mode = "live" if CFG.live_allowed() else "paper"
    conn = _open_readonly(str(db.resolve_db_path(mode)))
    try:
        return snapshot_state(conn, os.getenv("BRAIN_STATE_PATH", "brain_state.json"), _shared_copy())
    finally:
        conn.close()

 
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_web")
try:
    with open(os.path.join(DASHBOARD_DIR, "index.html"), encoding="utf-8") as _f:
        PAGE = _f.read()
except OSError:
    PAGE = "<h1>ZORA dashboard unavailable</h1>"


def _asset_path(path: str):
    rel = path.lstrip("/") or "index.html"
    if rel not in {"index.html", "styles.css", "app.js"}:
        return None
    base = os.path.abspath(DASHBOARD_DIR)
    full = os.path.abspath(os.path.join(base, rel))
    if os.path.commonpath([full, base]) != base:
        return None
    return full


def build_app():
    """Create the FastAPI app lazily so installs without fastapi (e.g. Termux) can still use everything else."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

    app = FastAPI(title="ZORA Dashboard", version=VERSION, docs_url="/docs", redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(os.path.join(DASHBOARD_DIR, "index.html"), media_type="text/html")

    @app.get("/styles.css")
    def styles():
        return FileResponse(os.path.join(DASHBOARD_DIR, "styles.css"), media_type="text/css")

    @app.get("/app.js")
    def app_js():
        return FileResponse(os.path.join(DASHBOARD_DIR, "app.js"), media_type="application/javascript")

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "zora-dashboard", "time": time.time(), "exchange": CFG.exchange_id}

    @app.get("/api/state")
    def state():
        try:
            return JSONResponse(_state_json(), headers={"Cache-Control": "no-store"})
        except (OSError, sqlite3.Error) as e:
            raise HTTPException(503, f"database unavailable: {e}") from e

    @app.get("/api/exchanges")
    def exchanges():
        from exchange_manager import MANAGER
        out = {}
        for eid in CFG.supported_exchanges:
            try:
                out[eid] = MANAGER.capabilities(eid)
            except Exception as e:
                out[eid] = {"id": eid, "error": str(e)}
        return out

    @app.get("/api/config")
    def config_view():
        return {"exchange": CFG.exchange_id, "symbols": CFG.symbols, "paper_mode": not CFG.live_allowed(),
                "native_protection_required": CFG.native_protection_required}

    @app.post("/webhook/tradingview")
    async def tradingview(request: Request):
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > tv_signal.MAX_BODY:
            raise HTTPException(413, "payload too large")
        chunks, total = [], 0
        async for chunk in request.stream():           # chunked uploads have no Content-Length: count bytes as read
            total += len(chunk)
            if total > tv_signal.MAX_BODY:
                raise HTTPException(413, "payload too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            score, note = tv_signal.handle_payload(raw)
        except tv_signal.WebhookError as e:
            raise HTTPException(e.status, str(e)) from e
        return {"ok": True, "score": score, "note": note}

    return app


class _Handler(BaseHTTPRequestHandler):
    """Stdlib read-only server used inside the trading process."""

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/styles.css", "/app.js"):
            asset = _asset_path(path)
            if not asset or not os.path.isfile(asset):
                self._send(404, b"Not found", "text/plain")
                return
            with open(asset, "rb") as f:
                body = f.read()
            ctype = mimetypes.guess_type(asset)[0] or "application/octet-stream"
            self._send(200, body, ctype)
        elif path == "/api/state":
            try:
                self._send(200, json.dumps(_state_json()).encode(), "application/json")
            except Exception as e:
                self._send(503, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/webhook/tradingview":
            self._send(405, b"Method Not Allowed", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > tv_signal.MAX_BODY:       # never read an attacker-sized body into memory
            self._send(413, b'{"error":"payload too large"}', "application/json")
            return
        raw = self.rfile.read(n)
        try:
            score, note = tv_signal.handle_payload(raw)
            self._send(200, json.dumps({"ok": True, "score": score, "note": note}).encode(), "application/json")
        except tv_signal.WebhookError as e:
            self._send(e.status, json.dumps({"error": str(e)}).encode(), "application/json")

    def _reject(self):
        self._send(405, b"Method Not Allowed", "text/plain")

    do_PUT = do_DELETE = do_PATCH = _reject


def make_server(host, port):
    return ThreadingHTTPServer((host, port), _Handler)


def start_background():
    if not CFG.dashboard_port:
        return None
    server = make_server(CFG.dashboard_host, CFG.dashboard_port)
    threading.Thread(target=server.serve_forever, name="zora-dashboard", daemon=True).start()
    return server


def run_fastapi(host=None, port=None):
    try:
        import uvicorn
    except ImportError as e:
        raise SystemExit("The FastAPI dashboard needs: pip install fastapi uvicorn") from e
    uvicorn.run(build_app(), host=host or CFG.dashboard_host, port=port or CFG.dashboard_port or 8787, log_level="info")


def serve_forever(host=None, port=None):
    return run_fastapi(host, port)
