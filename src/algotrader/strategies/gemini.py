"""
LLM strategy backed by Google Gemini.

Treat model output as untrusted input: it is schema-validated, levels must be
on the correct side of price, and any failure (timeout, bad JSON, missing
levels) degrades to HOLD. The risk manager applies its own independent checks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from algotrader.config import Instrument
from algotrader.domain import Candle, Signal, SignalAction
from algotrader.strategies.base import Strategy

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an intraday technical-analysis engine for Indian equities (NSE).
Analyse the OHLCV candles and respond with ONE JSON object and nothing else.

Method, in order of weight:
1. Trend: EMA alignment, higher-highs/higher-lows or the reverse.
2. Momentum: RSI/MACD behaviour inferred from the closes.
3. Volume: breakouts need above-average volume.
4. Support/resistance from swing highs/lows in the data.
5. Candlestick patterns (engulfing, pin bar, inside bar).

Rules:
- BUY/SELL only when reward:risk >= 1.5, otherwise HOLD.
- BUY stop below the nearest swing low; SELL stop above the nearest swing high.
- Targets must be reachable within today's session.
- Price extended > 2% from VWAP -> HOLD.
- No clear setup -> HOLD.

Schema:
{"signal": "BUY"|"SELL"|"HOLD", "target_price": number|null, "stop_loss": number|null,
 "confidence": number between 0 and 1, "rationale": "at most two sentences"}"""


class LlmSignal(BaseModel):
    signal: Literal["BUY", "SELL", "HOLD"]
    target_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    confidence: float = Field(default=0.0, ge=0, le=1)
    rationale: str = Field(default="", max_length=1000)


def build_prompt(instrument: Instrument, candles: Sequence[Candle], ltp: float | None, max_candles: int = 60) -> str:
    rows = [
        {"t": c.timestamp.strftime("%Y-%m-%d %H:%M"), "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume}
        for c in candles[-max_candles:]
    ]
    price = ltp if ltp else candles[-1].close
    return (
        f"Symbol: {instrument.symbol} ({instrument.exchange})\n"
        f"Current price: {price}\n"
        f"Candles (oldest first, completed bars only):\n{json.dumps(rows, separators=(',', ':'))}\n"
        "Respond with the JSON signal."
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_llm_response(text: str) -> LlmSignal:
    cleaned = _FENCE.sub("", text.strip()).strip()
    data: Any = json.loads(cleaned)
    if isinstance(data, dict) and isinstance(data.get("signal"), str):
        data["signal"] = data["signal"].strip().upper()
    return LlmSignal.model_validate(data)


class GeminiStrategy(Strategy):
    name = "gemini"
    warmup = 5

    def __init__(self, api_key: str, model: str, timeout: float = 30.0, client: Any = None) -> None:
        if client is None:
            if not api_key:
                raise ValueError("GEMINI_API_KEY is required for the gemini strategy")
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client
        self._model = model
        self._timeout = timeout
        self.last_latency_ms: int | None = None

    async def _call_model(self, prompt: str) -> str:
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,
                max_output_tokens=512,
                response_mime_type="application/json",
            ),
        )
        return str(response.text or "")

    async def generate(self, instrument: Instrument, candles: Sequence[Candle], ltp: float | None) -> Signal:
        if len(candles) < self.warmup:
            return self.hold(instrument, f"insufficient candles ({len(candles)})")
        price = ltp or candles[-1].close
        started = time.perf_counter()
        try:
            text = await asyncio.wait_for(self._call_model(build_prompt(instrument, candles, ltp)), self._timeout)
        except TimeoutError:
            logger.warning("Gemini timed out after %.0fs for %s", self._timeout, instrument.symbol)
            return self.hold(instrument, "model timeout", price)
        except Exception as exc:
            logger.error("Gemini call failed for %s: %s", instrument.symbol, exc)
            return self.hold(instrument, "model error", price)
        finally:
            self.last_latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            parsed = parse_llm_response(text)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("unparseable Gemini output for %s: %s | %.200s", instrument.symbol, exc, text)
            return self.hold(instrument, "invalid model output", price)

        action = SignalAction(parsed.signal)
        if action is not SignalAction.HOLD and (parsed.stop_loss is None or parsed.target_price is None):
            return self.hold(instrument, f"model returned {action.value} without stop/target", price)

        return Signal(
            symbol=instrument.symbol,
            action=action,
            strategy=self.name,
            stop_loss=parsed.stop_loss,
            target=parsed.target_price,
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            reference_price=price,
        )
