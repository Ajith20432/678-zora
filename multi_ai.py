"""
multi_ai.py — connects several LLM APIs as independent "AI expert" opinions
on the current technical signal. Their consensus is folded into the
strategy as one more expert (alongside the 15m/1h/4h technical experts —
see strategy.py / brain.py), and the auto-learning brain learns over time
how much to trust it, exactly like any other factor.

Four things distinguish this from a naive "ask five chatbots and average
the votes" integration:

1. STRUCTURED OUTPUT, not regex-parsed prose. Every provider is asked for
   a JSON verdict {"lean", "confidence", "note"} using its own native
   mechanism — Anthropic tool-calling (tool_choice forces the call),
   OpenAI/Groq response_format=json_object, Gemini responseSchema. A
   legacy LEAN:/NOTE: text parser is kept as a last-resort fallback for a
   model that ignores the instruction, so a malformed response degrades
   gracefully instead of crashing the cycle.

2. AGENTIC TOOL USE. Anthropic and the OpenAI-compatible providers (Groq,
   OpenRouter, OpenAI) are given a real `get_price_series` tool and can
   call it — against live market data, if an exchange handle was passed
   in — before answering, in a loop bounded to MAX_TOOL_ROUNDS so it can't
   stall the trading loop. Gemini's function-calling wire format differs
   enough that it's structured-JSON-only here, not agentic; that's a
   deliberate scope line, documented rather than silently absent.

3. LLM-AS-JUDGE. When 2+ providers answer, their individual verdicts are
   handed to one "judge" provider (AI_JUDGE_PROVIDER in .env, or whichever
   answered if unset) to weigh and synthesize into one final call, rather
   than just averaging votes. If the judge is unavailable or its own call
   fails, this falls back to confidence-weighted vote averaging — the
   judge is a refinement, never a single point of failure.

4. RESILIENCE. Each provider gets one immediate retry on a transient
   error. A provider that keeps failing gets a cooldown (a simple circuit
   breaker) so a dead/rate-limited endpoint isn't hammered every poll
   cycle; a 429 response's own Retry-After header sets the cooldown
   precisely when the provider gives one.

Every provider is optional: leave its API key blank in .env and it's
skipped entirely — the bot runs fine on pure technical analysis with zero
AI providers configured. Nothing in this file can execute a trade or touch
risk.py's limits; it only ever produces one more opinion for the strategy
layer to weigh, and the get_price_series tool is read-only market data.

Providers wired up here (check each one's own pricing/docs page before
relying on it — free tiers and rate limits change without much notice):
  - Groq        (GROQ_API_KEY)        console.groq.com        — no card, fast, rate-limited
  - OpenRouter  (OPENROUTER_API_KEY)  openrouter.ai/keys      — "openrouter/free" auto-picks a free model
  - Gemini      (GEMINI_API_KEY)      aistudio.google.com/apikey — no card, rate-limited
Optional paid extras, off unless keyed:
  - Anthropic   (ANTHROPIC_API_KEY)
  - OpenAI      (OPENAI_API_KEY)

All calls are plain HTTPS + JSON via `requests` — no extra heavy SDKs to
install on a phone.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time

import requests

from config import CFG
import ai_guard
from data import fetch_ohlcv_df

TIMEOUT = 20                # seconds per provider HTTP call
MAX_TOOL_ROUNDS = 2
AI_MIN_CONFIDENCE = CFG.ai_min_confidence  # AI_MIN_CONFIDENCE in .env (was hard-coded, so the setting was ignored)

# Groq model discovery/fallback cache. This prevents a deprecated model from
# taking the whole AI panel down after a provider-side model retirement.
_GROQ_MODEL_CACHE: dict[str, object] = {"models": set(), "ts": 0.0}
_GROQ_CACHE_TTL = 600.0
_GROQ_LOCK = threading.Lock()          # bounded agentic loop — at most this many tool calls before forcing an answer

# ---------------- resilience: retry + circuit breaker ----------------
COOLDOWN_AFTER_FAILURES = 3      # consecutive failures before a provider gets skipped for a while
BASE_COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 300

_circuit: dict[str, dict] = {}   # provider name -> {"failures": int, "cooldown_until": epoch seconds}


class _RateLimited(Exception):
    """Raised internally when a provider returns HTTP 429; carries its Retry-After if given."""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(f"rate limited (retry_after={retry_after})")


def _is_cooling_down(name: str) -> bool:
    state = _circuit.get(name)
    return bool(state and time.time() < state.get("cooldown_until", 0))


def _record_success(name: str) -> None:
    _circuit[name] = {"failures": 0, "cooldown_until": 0}


def _record_failure(name: str, retry_after: float | None = None) -> None:
    state = _circuit.setdefault(name, {"failures": 0, "cooldown_until": 0})
    state["failures"] += 1
    if retry_after is not None:
        state["cooldown_until"] = time.time() + retry_after
    elif state["failures"] >= COOLDOWN_AFTER_FAILURES:
        cooldown = min(MAX_COOLDOWN_SECONDS,
                        BASE_COOLDOWN_SECONDS * (2 ** (state["failures"] - COOLDOWN_AFTER_FAILURES)))
        state["cooldown_until"] = time.time() + cooldown


def _retry_after_seconds(response) -> float | None:
    try:
        val = response.headers.get("Retry-After")
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ---------------- shared verdict schema + prompt ----------------
VERDICT_SCHEMA_DESCRIPTION = (
    'Respond with a single JSON object, and nothing else, of the exact shape: '
    '{"lean": "BUY" | "SELL" | "HOLD", "confidence": <integer 0-100>, "note": "<one short sentence>"}.'
)


def _base_prompt(symbol: str, signal, headlines: list[str] | None = None, market_context: str | None = None) -> str:
    headline_block = ""
    if headlines:
        headline_block = "\nRecent relevant headlines:\n" + "\n".join(f"- {h}" for h in headlines[:5]) + "\n"
    context_block = f"\nBTC market context (context only; ETH/USDT remains the only traded pair):\n{market_context}\n" if market_context else ""
    tool_hint = (
        "\nIf a get_price_series tool is available to you and checking another timeframe would "
        "genuinely change your answer, you may call it once or twice before answering. "
    )
    return (
        f"You are one of several independent reviewers of a rule-based crypto trading signal. "
        f"Symbol: {symbol}. Proposed action: {signal.action} (confidence {signal.confidence:.0f}/100). "
        f"Contributing factors: {', '.join(signal.reasons)}.{headline_block}"
        f"{context_block}{tool_hint}\n"
        f"{VERDICT_SCHEMA_DESCRIPTION} Do not give financial advice — the note is just a brief "
        f"sanity-check flag for anything the rule-based system might be missing."
    )


def _extract_json_object(text: str) -> dict | None:
    """Find the first valid JSON object in text, tolerating stray prose a model adds around it."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _parse_legacy_text(text: str) -> tuple[str, int, str]:
    """Old LEAN:/NOTE: text format — a last-resort fallback for a model that ignores the JSON instruction."""
    lean_match = re.search(r"LEAN:\s*(BUY|SELL|HOLD)", text, re.IGNORECASE)
    note_match = re.search(r"NOTE:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    lean = lean_match.group(1).upper() if lean_match else "HOLD"
    note = note_match.group(1).strip().splitlines()[0] if note_match else (text or "").strip()[:160]
    return lean, 50, note


def parse_verdict(text: str) -> tuple[str, int, str]:
    """
    Returns (lean, confidence, note). Tries structured JSON first — the
    expected path for every provider here — and falls back to the legacy
    text format rather than crashing or silently mis-scoring an
    uncooperative model as a confident HOLD.
    """
    obj = _extract_json_object(text)
    if isinstance(obj, dict):
        lean = str(obj.get("lean", "HOLD")).upper()
        if lean not in ("BUY", "SELL", "HOLD"):
            lean = "HOLD"
        try:
            confidence = int(max(0, min(100, float(obj.get("confidence", 50)))))
        except (TypeError, ValueError):
            confidence = 50
        note = str(obj.get("note", "")).strip()[:200] or "(no note given)"
        return lean, confidence, note
    return _parse_legacy_text(text)


# ---------------- agentic tool: models can request more live market context ----------------
_TOOL_NAME = "get_price_series"
_TOOL_DESCRIPTION = "Fetch recent closing prices for a timeframe not already given, for more context."

_OPENAI_STYLE_TOOL = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"timeframe": {"type": "string", "description": "e.g. '5m', '30m', '1d'"}},
            "required": ["timeframe"],
        },
    },
}

_ANTHROPIC_PRICE_TOOL = {
    "name": _TOOL_NAME,
    "description": _TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {"timeframe": {"type": "string", "description": "e.g. '5m', '30m', '1d'"}},
        "required": ["timeframe"],
    },
}

_ANTHROPIC_VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Submit your final structured review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lean": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "note": {"type": "string"},
        },
        "required": ["lean", "confidence", "note"],
    },
}


def _execute_tool(symbol: str, timeframe: str, exchange) -> str:
    """
    Runs get_price_series for real against live market data if an exchange
    handle was given; otherwise reports that live data isn't available in
    this context rather than fabricating numbers. Never raises — a bad
    tool call degrades to an error string the model can read and recover
    from, same as a real tool-use API would return.
    """
    if exchange is None:
        return json.dumps({"error": "no live exchange handle available in this context"})
    try:
        df = fetch_ohlcv_df(exchange, symbol, timeframe, limit=20)
        closes = [round(c, 4) for c in df["close"].tolist()[-10:]]
        return json.dumps({"timeframe": timeframe, "last_10_closes": closes})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------- OpenAI-compatible caller (Groq, OpenRouter, OpenAI) ----------------
def _call_openai_style(
    url: str, api_key: str, model: str, prompt: str,
    symbol: str, exchange, use_json_mode: bool, enable_tools: bool,
) -> str:
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        allow_tools = enable_tools and CFG.ai_tool_use_enabled and round_num < MAX_TOOL_ROUNDS
        body = {"model": model, "messages": messages, "max_tokens": 300}
        if allow_tools:
            body["tools"] = [_OPENAI_STYLE_TOOL]
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}

        r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=TIMEOUT)
        if r.status_code == 429:
            raise _RateLimited(_retry_after_seconds(r))
        r.raise_for_status()
        message = r.json()["choices"][0]["message"]
        tool_calls = message.get("tool_calls")

        if tool_calls and allow_tools:
            messages.append(message)
            for call in tool_calls:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _execute_tool(symbol, args.get("timeframe", "1h"), exchange)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            continue  # ask again with the tool result now in context

        return message.get("content") or ""
    return ""


def _groq_models() -> set[str]:
    """Return currently available Groq model IDs, cached for a few minutes."""
    now = time.time()
    with _GROQ_LOCK:
        if now - float(_GROQ_MODEL_CACHE["ts"]) < _GROQ_CACHE_TTL:
            return set(_GROQ_MODEL_CACHE["models"])
    r = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {CFG.groq_api_key}"},
        timeout=10,
    )
    if r.status_code == 429:
        raise _RateLimited(_retry_after_seconds(r))
    r.raise_for_status()
    models = {str(x.get("id")) for x in r.json().get("data", []) if x.get("id")}
    with _GROQ_LOCK:
        _GROQ_MODEL_CACHE["models"] = models
        _GROQ_MODEL_CACHE["ts"] = now
    return models


def groq_healthcheck() -> tuple[bool, str]:
    """Non-trading diagnostic: verify key access and configured model availability."""
    if not CFG.groq_api_key:
        return False, "GROQ_API_KEY is not configured"
    try:
        models = _groq_models()
        if CFG.groq_model in models:
            return True, f"OK: {CFG.groq_model} is available"
        fallbacks = [m for m in CFG.groq_fallback_models if m in models]
        if fallbacks:
            return False, f"Configured model unavailable: {CFG.groq_model}; fallback available: {fallbacks[0]}"
        return False, f"Configured model unavailable: {CFG.groq_model}; no configured fallback is currently available"
    except Exception as e:
        return False, f"Groq API/model check failed: {e}"


def groq_probe() -> tuple[bool, str]:
    """Make one tiny real Groq completion request; intended for `ai-test`, never trading."""
    if not CFG.groq_api_key:
        return False, "GROQ_API_KEY is not configured"
    prompt = (
        'Return exactly one JSON object: {"lean":"HOLD","confidence":100,"note":"connection ok"}. '
        'Do not add markdown or extra text.'
    )
    try:
        models = _groq_models()
        candidates = [CFG.groq_model] + [m for m in CFG.groq_fallback_models if m != CFG.groq_model]
        model = next((m for m in candidates if not models or m in models), None)
        if not model:
            return False, f"No configured Groq model is currently available: {candidates}"
        text = _call_openai_style(
            "https://api.groq.com/openai/v1/chat/completions",
            CFG.groq_api_key, model, prompt, "TEST/USDT", None,
            use_json_mode=True, enable_tools=False,
        )
        lean, confidence, note = parse_verdict(text)
        return True, f"PASS: {model} replied with {lean} {confidence}% — {note}"
    except Exception as e:
        return False, f"Groq completion test failed: {e}"


def _call_groq(prompt: str, symbol: str, exchange) -> str:
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    models = [CFG.groq_model]
    if CFG.groq_auto_fallback:
        models.extend(m for m in CFG.groq_fallback_models if m not in models)

    last_error = None
    for model in models:
        try:
            # Do not silently use a retired model when the models endpoint can tell us first.
            try:
                available = _groq_models()
                if available and model not in available:
                    last_error = RuntimeError(f"Groq model unavailable: {model}")
                    continue
            except _RateLimited:
                raise
            except Exception:
                # Model discovery is diagnostic only; the completion endpoint remains authoritative.
                pass
            return _call_openai_style(
                endpoint, CFG.groq_api_key, model, prompt, symbol, exchange,
                use_json_mode=True, enable_tools=True,
            )
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = getattr(e.response, "text", "")[:500]
            last_error = RuntimeError(f"Groq {model} HTTP {status}: {body}")
            if not CFG.groq_auto_fallback or status not in (400, 404, 422):
                raise last_error from e
        except Exception as e:
            last_error = e
            # A provider-side model retirement often surfaces as a 4xx. Other failures
            # should use the normal provider retry/circuit-breaker path rather than hopping models.
            raise
    raise last_error or RuntimeError("No usable Groq model configured")


def _call_openrouter(prompt: str, symbol: str, exchange) -> str:
    # No response_format here: OpenRouter routes "openrouter/free" to whichever free model is
    # available, and not all of them support json_object mode — the prompt instruction plus
    # parse_verdict's legacy-text fallback carry the structure requirement instead.
    return _call_openai_style(
        "https://openrouter.ai/api/v1/chat/completions", CFG.openrouter_api_key, CFG.openrouter_model,
        prompt, symbol, exchange, use_json_mode=False, enable_tools=True,
    )


def _call_openai(prompt: str, symbol: str, exchange) -> str:
    return _call_openai_style(
        "https://api.openai.com/v1/chat/completions", CFG.openai_api_key, CFG.openai_model,
        prompt, symbol, exchange, use_json_mode=True, enable_tools=True,
    )


# ---------------- Gemini: structured JSON via responseSchema (no agentic loop) ----------------
def _call_gemini(prompt: str, symbol: str, exchange) -> str:
    # Key goes in a header, NOT the URL: requests puts the full URL in HTTPError text, and those
    # error strings are logged and forwarded to Telegram — the key would leak into both.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CFG.gemini_model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "lean": {"type": "STRING", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence": {"type": "INTEGER"},
                    "note": {"type": "STRING"},
                },
                "required": ["lean", "confidence", "note"],
            },
        },
    }
    r = requests.post(url, headers={"x-goog-api-key": CFG.gemini_api_key}, json=body, timeout=TIMEOUT)
    if r.status_code == 429:
        raise _RateLimited(_retry_after_seconds(r))
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# ---------------- Anthropic: native tool-calling, forced structured verdict ----------------
def _call_anthropic(prompt: str, symbol: str, exchange) -> str:
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        force_verdict = round_num >= MAX_TOOL_ROUNDS
        if force_verdict:
            tools = [_ANTHROPIC_VERDICT_TOOL]
        elif CFG.ai_tool_use_enabled:
            tools = [_ANTHROPIC_PRICE_TOOL, _ANTHROPIC_VERDICT_TOOL]
        else:
            tools = [_ANTHROPIC_VERDICT_TOOL]

        body = {"model": CFG.anthropic_model, "max_tokens": 500, "messages": messages, "tools": tools}
        if force_verdict:
            body["tool_choice"] = {"type": "tool", "name": "submit_verdict"}

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CFG.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body, timeout=TIMEOUT,
        )
        if r.status_code == 429:
            raise _RateLimited(_retry_after_seconds(r))
        r.raise_for_status()
        content_blocks = r.json().get("content", [])

        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        verdict_block = next((b for b in tool_use_blocks if b["name"] == "submit_verdict"), None)
        if verdict_block:
            return json.dumps(verdict_block["input"])

        price_blocks = [b for b in tool_use_blocks if b["name"] == _TOOL_NAME]
        if price_blocks and not force_verdict:
            messages.append({"role": "assistant", "content": content_blocks})
            tool_results = [
                {"type": "tool_result", "tool_use_id": b["id"],
                 "content": _execute_tool(symbol, b["input"].get("timeframe", "1h"), exchange)}
                for b in price_blocks
            ]
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool use at all (shouldn't normally happen since a tool is required) — degrade gracefully.
        text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        return "\n".join(text_blocks)
    return ""


def _configured_providers() -> list[tuple[str, object]]:
    """Every provider with a key set, minus any currently cooling down after repeated failures."""
    candidates = [
        ("groq", CFG.groq_api_key, _call_groq),
        ("openrouter", CFG.openrouter_api_key, _call_openrouter),
        ("gemini", CFG.gemini_api_key, _call_gemini),
        ("anthropic", CFG.anthropic_api_key, _call_anthropic),
        ("openai", CFG.openai_api_key, _call_openai),
    ]
    return [(name, fn) for name, key, fn in candidates if key and not _is_cooling_down(name)]


def _call_with_retry(name: str, fn, prompt: str, symbol: str, exchange):
    """One provider call with a single immediate retry on a transient error, then circuit-breaker bookkeeping."""
    last_err: Exception | None = None
    for _attempt in range(2):
        try:
            started = time.monotonic()
            ai_guard.record_call()
            text = fn(prompt, symbol, exchange)
            latency_ms = int((time.monotonic() - started) * 1000)
            lean, confidence, note = parse_verdict(text)
            if confidence < AI_MIN_CONFIDENCE:
                lean = "HOLD"
                note = f"abstained: confidence {confidence}% below safety floor {AI_MIN_CONFIDENCE}%"
            _record_success(name)
            ai_guard.record_available()
            return lean, confidence, f"{note} [{latency_ms}ms]", None
        except _RateLimited as e:
            _record_failure(name, retry_after=e.retry_after or BASE_COOLDOWN_SECONDS)
            ai_guard.record_unavailable(f"{name} rate limited" + (f"; retry after {e.retry_after:.0f}s" if e.retry_after else ""))
            suffix = f" (retry after {e.retry_after:.0f}s)" if e.retry_after else ""
            return None, None, None, f"rate limited{suffix}"
        except Exception as e:
            last_err = e
            continue  # one immediate retry for a transient hiccup
    _record_failure(name)
    ai_guard.record_unavailable(f"{name} unavailable: {last_err}")
    return None, None, None, str(last_err)


def _synthesize_with_judge(symbol: str, signal, verdicts: list[dict]) -> tuple[float | None, str]:
    """
    Hands every individual verdict to one judge provider and asks it to
    weigh them into a single synthesized call, instead of plain vote
    averaging. Returns (None, "") — never raises — if there's nothing to
    synthesize (fewer than 2 verdicts) or no judge could be reached, so the
    caller falls back to confidence-weighted averaging.
    """
    if not CFG.ai_judge_enabled or len(verdicts) < 2:
        return None, ""

    available = dict(_configured_providers())
    judge_name = CFG.ai_judge_provider if CFG.ai_judge_provider in available else next((n for n in available if n not in {v["name"] for v in verdicts}), next(iter(available), None))
    if judge_name is None:
        return None, ""
    caller = available[judge_name]

    summary = "\n".join(f"- {v['name']}: {v['lean']} {v['confidence']}% — {v['note']}" for v in verdicts)
    prompt = (
        f"Several independent reviewers assessed a proposed {signal.action} on {symbol}:\n{summary}\n\n"
        f"Weigh their opinions (note any strong disagreement) and give ONE final synthesized call. "
        f"{VERDICT_SCHEMA_DESCRIPTION}"
    )

    lean, confidence, note, err = _call_with_retry(judge_name, caller, prompt, symbol, None)
    if err:
        return None, ""

    direction = {"BUY": 1, "SELL": -1, "HOLD": 0}
    score = direction[lean] * confidence
    return score, f"judge ({judge_name}): {lean} {confidence}% — {note} [synthesized from {len(verdicts)} experts]"


def ai_safety_status() -> dict:
    st = ai_guard.status()
    st["providers"] = [n for n, _ in _configured_providers()]
    return st


def get_ai_consensus(
    symbol: str, signal, headlines: list[str] | None = None, exchange=None, market_context: str | None = None,
) -> tuple[float | None, list[str]]:
    """
    Queries every configured, non-cooling-down provider in parallel, each
    with a structured verdict, real tool access where supported, and a
    single retry on transient failure. Successful verdicts are then handed
    to a judge provider for synthesis; if that's unavailable, falls back to
    confidence-weighted vote averaging.

    headlines: optional recent news headlines (from news_feed.py), given to
               every provider as extra context.
    exchange:  optional ccxt exchange handle so the get_price_series tool
               can fetch real data; omit (e.g. for backtest/learn, which
               never call this at all) and the tool degrades gracefully.

    Returns (consensus_score, notes):
      consensus_score — -100..100, or None if no provider answered.
      notes           — human-readable strings for logging: the judge's
                         synthesis (if any) first, then each provider's own
                         verdict or error/cooldown/rate-limit reason.
    """
    providers = _configured_providers()
    if not providers:
        return None, ["no AI providers configured (or all currently cooling down after repeated "
                       "failures) — running technical-only"]

    prompt = _base_prompt(symbol, signal, headlines, market_context)
    notes: list[str] = []
    verdicts: list[dict] = []

    def run(name, fn):
        lean, confidence, note, err = _call_with_retry(name, fn, prompt, symbol, exchange)
        return name, lean, confidence, note, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(providers), max(1, CFG.ai_max_concurrent_providers))) as ex:
        futures = [ex.submit(run, name, fn) for name, fn in providers]
        for fut in concurrent.futures.as_completed(futures):
            name, lean, confidence, note, err = fut.result()
            if err:
                notes.append(f"{name}: error — {err}")
                continue
            notes.append(f"{name}: {lean} {confidence}% — {note}")
            verdicts.append({"name": name, "lean": lean, "confidence": confidence, "note": note})

    if not verdicts:
        ai_guard.record_unavailable("all configured AI providers unavailable or rate-limited")
        return None, notes

    judge_score, judge_note = _synthesize_with_judge(symbol, signal, verdicts)
    if judge_score is not None:
        notes.insert(0, judge_note)
        return judge_score, notes

    direction = {"BUY": 1, "SELL": -1, "HOLD": 0}
    total = sum(direction[v["lean"]] * (v["confidence"] / 100) for v in verdicts)
    consensus_score = 100 * (total / len(verdicts))
    return consensus_score, notes
