"""
config.py — loads settings from .env
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    exchange_id: str = os.getenv("EXCHANGE_ID", "htx")
    supported_exchanges: tuple[str, ...] = ("binance", "bybit", "okx", "htx")
    symbol: str = os.getenv("SYMBOL", "ETH/USDT")          # used by backtest/learn by default
    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "ETH/USDT")).split(",") if s.strip()
    ))                                                       # traded together by `run` / `live`
    timeframes: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("TIMEFRAMES", "15m,1h,4h").split(",") if s.strip()
    ))
    # ETH/USDT is the only traded market. BTC/USDT is context-only: its trend
    # is supplied to the AI panel so ETH decisions can respect BTC regime moves.
    btc_context_symbol: str = os.getenv("BTC_CONTEXT_SYMBOL", "BTC/USDT")

    # capital / risk
    starting_capital: float = _f("STARTING_CAPITAL", 1000)
    risk_per_trade_pct: float = _f("RISK_PER_TRADE_PCT", 0.5)     # % of equity risked per trade
    max_exposure_pct: float = _f("MAX_EXPOSURE_PCT", 30)          # RISK BUDGET: sum of risk-per-trade % across open positions (not notional)
    max_daily_loss_pct: float = _f("MAX_DAILY_LOSS_PCT", 3)       # kill switch
    max_drawdown_pct: float = _f("MAX_DRAWDOWN_PCT", 15)          # kill switch from peak equity
    atr_stop_mult: float = _f("ATR_STOP_MULT", 2.0)
    atr_take_mult: float = _f("ATR_TAKE_MULT", 3.5)
    max_position_notional_pct: float = _f("MAX_POSITION_NOTIONAL_PCT", 30)  # one position may never exceed this % of equity
    max_total_notional_pct: float = _f("MAX_TOTAL_NOTIONAL_PCT", 100)       # all open positions together: 100 = no leverage
    allow_shorts: bool = _b("ALLOW_SHORTS", False)                # live SELL entries (shorts) are refused unless explicitly enabled; paper always simulates them

    # position management (paper / software-protected positions only; native exchange orders are never modified)
    trailing_stop_enabled: bool = _b("TRAILING_STOP_ENABLED", True)
    breakeven_at_r: float = _f("BREAKEVEN_AT_R", 1.0)             # move stop to entry once profit reaches this many R
    trail_atr_mult: float = _f("TRAIL_ATR_MULT", 2.0)             # after breakeven, trail the stop this many ATR behind price

    # market / data safety guards
    stale_data_max_candles: float = _f("STALE_DATA_MAX_CANDLES", 2.5)  # refuse to trade on data older than this many candle periods
    max_spread_bps: float = _f("MAX_SPREAD_BPS", 15.0)            # live entries refused when bid/ask spread is wider
    min_quote_volume: float = _f("MIN_QUOTE_VOLUME", 1_000_000)   # 24h quote volume floor for live entries
    degradation_window: int = _i("DEGRADATION_WINDOW", 20)        # closed trades considered by the degradation guard
    degradation_min_profit_factor: float = _f("DEGRADATION_MIN_PF", 0.7)  # pause entries if rolling profit factor falls below this
    correlation_window: int = _i("CORRELATION_WINDOW", 50)        # candles of BTC/ETH returns used for the correlation read
    correlation_break_threshold: float = _f("CORRELATION_BREAK", 0.3)  # below this the BTC/ETH link is "broken": entries are sized down
    correlation_size_mult: float = _f("CORRELATION_SIZE_MULT", 0.5)
    fear_greed_enabled: bool = _b("FEAR_GREED_ENABLED", True)

    # dashboard (read-only, local by default)
    dashboard_port: int = _i("DASHBOARD_PORT", 0)                 # 0 = off
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")

    # execution realism / safety
    fee_rate: float = _f("FEE_RATE", 0.001)  # 0.10% per side default; override with your HTX tier
    slippage_bps: float = _f("SLIPPAGE_BPS", 5.0)
    execution_latency_ms: int = _i("EXECUTION_LATENCY_MS", 250)
    latency_bps: float = _f("LATENCY_BPS", 1.0)
    native_protection_required: bool = _b("NATIVE_PROTECTION_REQUIRED", True)
    reconcile_on_start: bool = _b("RECONCILE_ON_START", True)
    paper_validation_days: int = _i("PAPER_VALIDATION_DAYS", 30)

    # extra experts (optional; each is just one more vote the brain learns to weigh — never a trade trigger)
    ml_enabled: bool = _b("ML_ENABLED", True)
    ml_min_holdout_accuracy: float = _f("ML_MIN_HOLDOUT_ACCURACY", 0.53)  # below this the model has no proven edge -> score 0
    tv_webhook_secret: str = os.getenv("TV_WEBHOOK_SECRET", "")           # blank = TradingView webhook disabled
    tv_signal_ttl_minutes: int = _i("TV_SIGNAL_TTL_MINUTES", 60)
    tv_signal_path: str = os.getenv("TV_SIGNAL_PATH", "tv_signal.json")

    # DCA / grid modes (historical paper simulation). Fixed order sizes + hard caps — never martingale.
    dca_base_order_pct: float = _f("DCA_BASE_ORDER_PCT", 5.0)       # % of equity per buy
    dca_interval_hours: float = _f("DCA_INTERVAL_HOURS", 24.0)
    dca_dip_pct: float = _f("DCA_DIP_PCT", 3.0)                    # extra safety buy per this % below avg entry
    dca_max_safety_orders: int = _i("DCA_MAX_SAFETY_ORDERS", 3)
    dca_take_profit_pct: float = _f("DCA_TAKE_PROFIT_PCT", 4.0)
    dca_stop_loss_pct: float = _f("DCA_STOP_LOSS_PCT", 15.0)       # a DCA cycle is closed at this loss from average entry
    grid_levels: int = _i("GRID_LEVELS", 10)
    grid_range_pct: float = _f("GRID_RANGE_PCT", 8.0)               # +/- around the start price
    grid_capital_pct: float = _f("GRID_CAPITAL_PCT", 20.0)          # % of equity the grid may use
    grid_stop_pct: float = _f("GRID_STOP_PCT", 3.0)                 # price this far below the grid floor liquidates and stops it

    # loop
    poll_seconds: int = _i("POLL_SECONDS", 60)

    # mode — LIVE_TRADING must be explicitly and deliberately enabled.
    live_trading: bool = _b("LIVE_TRADING", False)
    i_understand_the_risk: bool = _b("I_UNDERSTAND_THE_RISK", False)

    # exchange API (only needed for LIVE_TRADING)
    api_key: str = os.getenv("EXCHANGE_API_KEY", "")
    api_secret: str = os.getenv("EXCHANGE_API_SECRET", "")
    api_password: str = os.getenv("EXCHANGE_API_PASSWORD", "")

    # ---- Multi-AI expert panel (all optional, advisory-only — see multi_ai.py) ----
    # Leave any key blank to skip that provider entirely; nothing breaks.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    groq_fallback_models: tuple[str, ...] = field(default_factory=lambda: tuple(
        m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b").split(",") if m.strip()
    ))
    groq_auto_fallback: bool = _b("GROQ_AUTO_FALLBACK", True)

    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "openrouter/free")

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

    # optional paid extras — off unless keyed
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # judge: which provider synthesizes the individual AI verdicts into one call.
    # Blank = auto-pick whichever configured provider answers first each cycle.
    ai_judge_provider: str = os.getenv("AI_JUDGE_PROVIDER", "")
    # agentic tool use: lets Anthropic/Groq/OpenRouter/OpenAI call get_price_series
    # themselves before answering. Off saves tokens/latency at the cost of less context.
    ai_tool_use_enabled: bool = _b("AI_TOOL_USE_ENABLED", True)
    ai_max_concurrent_providers: int = _i("AI_MAX_CONCURRENT_PROVIDERS", 3)
    ai_judge_enabled: bool = _b("AI_JUDGE_ENABLED", True)
    ai_cycle_min_seconds: int = _i("AI_CYCLE_MIN_SECONDS", 45)
    ai_min_confidence: int = _i("AI_MIN_CONFIDENCE", 35)
    # AI safety: when at least one provider is configured, exhausted/rate-limited
    # AI capacity blocks NEW entries. Existing positions remain managed by risk/SL/TP.
    ai_entry_block_on_unavailable: bool = _b("AI_ENTRY_BLOCK_ON_UNAVAILABLE", True)
    ai_provider_cooldown_seconds: int = _i("AI_PROVIDER_COOLDOWN_SECONDS", 60)
    ai_daily_call_limit: int = _i("AI_DAILY_CALL_LIMIT", 0)  # 0 = disabled
    ai_minute_call_limit: int = _i("AI_MINUTE_CALL_LIMIT", 0)  # 0 = provider/API limit not known

    db_path: str = os.getenv("DB_PATH", "zora.db")
    # Trading state is isolated by mode. PAPER keeps the historical DB_PATH; LIVE uses a separate DB.
    paper_db_path: str = os.getenv("PAPER_DB_PATH", "")
    live_db_path: str = os.getenv("LIVE_DB_PATH", "")

    # FastAPI dashboard
    dashboard_reload_seconds: int = _i("DASHBOARD_RELOAD_SECONDS", 10)

    # ---- Telegram alerts + remote control (both optional) ----
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_admin_chat_ids: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "").split(",") if s.strip()
    ))
    telegram_heartbeat_minutes: int = _i("TELEGRAM_HEARTBEAT_MINUTES", 15)

    def live_allowed(self) -> bool:
        return self.live_trading and self.i_understand_the_risk and bool(self.api_key) and bool(self.api_secret)

    def validate(self) -> list[str]:
        """Return human-readable problems with the current settings (empty list = fine)."""
        problems: list[str] = []
        if self.exchange_id not in self.supported_exchanges:
            problems.append(f"EXCHANGE_ID={self.exchange_id} unsupported; use {', '.join(self.supported_exchanges)}")
        if not 0 < self.risk_per_trade_pct <= 5:
            problems.append(f"RISK_PER_TRADE_PCT={self.risk_per_trade_pct} should be in (0, 5]")
        if not 0 < self.max_exposure_pct <= 100:
            problems.append(f"MAX_EXPOSURE_PCT={self.max_exposure_pct} should be in (0, 100]")
        if not 0 < self.max_total_notional_pct <= 100:
            problems.append(f"MAX_TOTAL_NOTIONAL_PCT={self.max_total_notional_pct} should be in (0, 100] (100 = no leverage)")
        if self.max_exposure_pct < self.risk_per_trade_pct:
            problems.append("MAX_EXPOSURE_PCT is smaller than RISK_PER_TRADE_PCT: no trade could ever be approved")
        if self.atr_stop_mult <= 0 or self.atr_take_mult <= 0:
            problems.append("ATR_STOP_MULT and ATR_TAKE_MULT must be positive")
        elif self.atr_take_mult < self.atr_stop_mult:
            problems.append("ATR_TAKE_MULT < ATR_STOP_MULT: reward/risk below 1, strategy needs a very high win rate")
        if self.starting_capital <= 0:
            problems.append("STARTING_CAPITAL must be positive")
        if self.poll_seconds < 5:
            problems.append("POLL_SECONDS below 5 will hit exchange rate limits")
        if self.fee_rate < 0 or self.slippage_bps < 0:
            problems.append("FEE_RATE and SLIPPAGE_BPS cannot be negative")
        if not self.timeframes:
            problems.append("TIMEFRAMES is empty")
        if self.live_trading and not self.live_allowed():
            problems.append("LIVE_TRADING is on but I_UNDERSTAND_THE_RISK / API keys are missing: live stays locked")
        if self.dashboard_port and self.dashboard_host not in ("127.0.0.1", "localhost"):
            problems.append("DASHBOARD_HOST is not local: the dashboard has no login, anyone on the network can read your trades")
        if self.tv_webhook_secret and len(self.tv_webhook_secret) < 16:
            problems.append("TV_WEBHOOK_SECRET is shorter than 16 characters: use a long random string")
        if self.tv_webhook_secret and not self.dashboard_port:
            problems.append("TV_WEBHOOK_SECRET is set but DASHBOARD_PORT is 0: the webhook is served by the dashboard server")
        if not 0 < self.dca_base_order_pct * (1 + self.dca_max_safety_orders) <= 100:
            problems.append("DCA_BASE_ORDER_PCT * (1 + DCA_MAX_SAFETY_ORDERS) must be in (0, 100]% of equity")
        if self.grid_levels < 2 or self.grid_range_pct <= 0:
            problems.append("GRID_LEVELS must be >= 2 and GRID_RANGE_PCT > 0")
        return problems


CFG = Config()
