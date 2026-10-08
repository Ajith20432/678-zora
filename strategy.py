"""
strategy.py — multi-timeframe, multi-factor signal engine.

Several independent experts are scored separately, then combined using
weights supplied by brain.py's auto-learning brain — which adjusts those
weights over time based on which experts have actually been right:
  - three technical experts: 15m / 1h / 4h candles, each scored on
    trend / momentum / rsi / mean-reversion / volume / chart patterns
  - one optional "ai" expert: the consensus of whichever free/paid LLM
    APIs are configured in .env (see multi_ai.py)
  - one optional "news" expert: keyword-sentiment lean across recent
    headlines mentioning the traded coin (see news_feed.py)

This keeps the "multiple specialist agents + coordinator" idea from the
original design, but as transparent, auditable code — the AI panel is
just one more voice the brain learns to weigh, never a special override.

Every function here takes an optional `brain` parameter, defaulting to the
production BRAIN singleton — pass an isolated AdaptiveBrain(state_path=...)
in tests so they never read or write the real brain_state.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from brain import BRAIN, AdaptiveBrain
from indicators import enrich
from patterns import detect_patterns


@dataclass
class Signal:
    action: str        # "BUY" | "SELL" | "HOLD"
    confidence: float  # 0-100
    reasons: list[str]
    expert_scores: dict[str, float] = field(default_factory=dict)        # e.g. {"15m":.., "ai":..}
    factor_scores_by_tf: dict[str, dict[str, float]] = field(default_factory=dict)  # technical timeframes only
    pattern_notes_by_tf: dict[str, list[str]] = field(default_factory=dict)  # detected chart patterns, for logging


def score_timeframe(df: pd.DataFrame, brain: AdaptiveBrain = BRAIN) -> tuple[float, dict[str, float], list[str]]:
    """Return (total_score, factor_contributions, pattern_notes) for one timeframe's latest candle."""
    d = enrich(df)
    last = d.iloc[-1]
    fw = brain.factor_weights
    c: dict[str, float] = {}

    # Trend (EMA stack)
    if last["ema9"] > last["ema21"] > last["ema50"]:
        c["trend"] = fw["trend"]
    elif last["ema9"] < last["ema21"] < last["ema50"]:
        c["trend"] = -fw["trend"]
    else:
        c["trend"] = 0

    # Momentum (MACD histogram)
    c["momentum"] = fw["momentum"] if last["macd_hist"] > 0 else -fw["momentum"]

    # RSI — avoid chasing extremes, reward room to run
    if last["rsi14"] < 30:
        c["rsi"] = fw["rsi"]      # oversold, possible bounce
    elif last["rsi14"] > 70:
        c["rsi"] = -fw["rsi"]     # overbought, possible pullback
    else:
        c["rsi"] = 0

    # Mean reversion vs Bollinger bands
    if last["close"] < last["bb_lower"]:
        c["meanrev"] = fw["meanrev"]
    elif last["close"] > last["bb_upper"]:
        c["meanrev"] = -fw["meanrev"]
    else:
        c["meanrev"] = 0

    # Volume confirmation — amplifies whichever direction is already leading
    lean = 1 if sum(c.values()) >= 0 else -1
    c["volume"] = fw["volume"] * lean if last["vol_spike"] > 1.5 else 0

    # Chart patterns — candlestick + swing-based (see patterns.py). Raw score is -100..100;
    # scale it into this factor's own learned weight, same as every other factor here.
    pattern_raw, pattern_notes = detect_patterns(df)
    c["pattern"] = fw["pattern"] * (pattern_raw / 100)

    total = max(-100.0, min(100.0, sum(c.values())))
    return total, c, pattern_notes


def combine(
    scores: dict[str, float],
    factor_scores_by_tf: dict[str, dict[str, float]],
    brain: AdaptiveBrain = BRAIN,
) -> Signal:
    """
    scores: {expert_name: score}. Weights come from the auto-learning brain —
    a 15m blip (or a shaky AI opinion) shouldn't override a 4h downtrend
    unless it has actually proven more reliable lately.
    """
    weights = brain.expert_weights
    total_weight = sum(weights.get(name, 0.3) for name in scores)
    weighted = sum(scores[name] * weights.get(name, 0.3) for name in scores) / total_weight

    reasons = [f"{name}:{scores[name]:+.0f}" for name in scores]

    if weighted >= 25:
        action = "BUY"
    elif weighted <= -25:
        action = "SELL"
    else:
        action = "HOLD"

    return Signal(action, min(100.0, abs(weighted)), reasons, scores, factor_scores_by_tf)


def generate_signal(
    mtf_data: dict[str, pd.DataFrame],
    ai_score: float | None = None,
    news_score: float | None = None,
    brain: AdaptiveBrain = BRAIN,
    fng_score: float | None = None,
    ml_score: float | None = None,
    tv_score: float | None = None,
) -> Signal:
    """
    mtf_data: {timeframe: OHLCV DataFrame}
    ai_score: optional -100..100 consensus from multi_ai.get_ai_consensus().
    news_score: optional -100..100 keyword-sentiment lean from news_feed.news_sentiment().
              Both default to None to run purely on technical experts — this is what
              backtest/learn always do, since there's no way to ask an LLM or replay a
              news feed as it stood at a past candle.
    fng_score: optional -100..100 contrarian lean from fear_greed.py (same rule: live loop only).
    ml_score: optional -100..100 from ml_model.py (numpy logistic-regression expert; 0 when it has no proven edge).
    tv_score: optional -100..100 from tv_signal.py (a TradingView alert, expires after TV_SIGNAL_TTL_MINUTES).
    """
    scores: dict[str, float] = {}
    factor_scores_by_tf: dict[str, dict[str, float]] = {}
    pattern_notes_by_tf: dict[str, list[str]] = {}
    for tf, df in mtf_data.items():
        if len(df) <= 60:
            continue
        s, factors, pattern_notes = score_timeframe(df, brain=brain)
        scores[tf] = s
        factor_scores_by_tf[tf] = factors
        if pattern_notes:
            pattern_notes_by_tf[tf] = pattern_notes

    if ai_score is not None:
        scores["ai"] = max(-100.0, min(100.0, ai_score))
    if news_score is not None:
        scores["news"] = max(-100.0, min(100.0, news_score))

    if fng_score is not None:
        scores["fng"] = max(-100.0, min(100.0, fng_score))

    if ml_score is not None:
        scores["ml"] = max(-100.0, min(100.0, ml_score))
    if tv_score is not None:
        scores["tv"] = max(-100.0, min(100.0, tv_score))

    if not scores:
        return Signal("HOLD", 0, ["insufficient data"])
    signal = combine(scores, factor_scores_by_tf, brain=brain)
    signal.pattern_notes_by_tf = pattern_notes_by_tf
    return signal
