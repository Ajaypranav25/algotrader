"""
services/gemini_engine.py — Google Gemini AI analysis engine.

Sends OHLCV candle data to Gemini and receives a strict JSON trading signal.
Uses the modern google-genai SDK targeting gemini-2.5-flash-lite.
"""
import json
import time
import asyncio
from datetime import datetime
from typing import Optional, List

from google import genai
from google.genai import types

from core.config import get_settings
from models.schemas import Candle, GeminiSignal
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("gemini_engine")

# ── Optimized System Prompt ───────────────────────────────────────────────────
# This prompt is carefully engineered to:
# 1. Force strict JSON output — no conversational text
# 2. Ground the model as a disciplined intraday analyst
# 3. Prevent hallucinated price levels
# 4. Enforce conservative risk posture

SYSTEM_PROMPT = """You are ARIA — Algorithmic Risk & Intelligence Analyst — an elite intraday technical analysis engine for Indian equity markets (NSE/BSE).

Your ONLY function is to analyze OHLCV candle data and output a trading signal as a strict JSON object. You do not converse. You do not explain yourself outside the JSON. You output ONLY valid JSON.

## ANALYSIS FRAMEWORK
Apply these technical methods IN ORDER, weighted by confluence:
1. Trend identification (EMA alignment, higher highs/lows structure)
2. Momentum (RSI divergence, MACD crossover if inferrable from candle shape)
3. Volume confirmation (above-average volume on breakouts is mandatory for BUY/SELL)
4. Key support/resistance levels (swing highs/lows from the provided data)
5. Candlestick patterns (engulfing, pin bars, inside bars)
6. Risk/reward ratio — ONLY signal BUY/SELL if R:R >= 1.5

## RISK RULES (NON-NEGOTIABLE)
- Stop loss must be below nearest swing low (for BUY) or above nearest swing high (for SELL)
- Target must be at next meaningful resistance (BUY) or support (SELL)
- If no clear setup exists, signal MUST be HOLD
- Do not chase extended moves — if the candle range is >2% from VWAP, signal HOLD
- Intraday only — all levels must be achievable within the same trading session

## OUTPUT FORMAT
You MUST respond with ONLY this JSON structure and absolutely nothing else — no markdown fences, no preamble, no postamble:

{
  "signal": "BUY" | "SELL" | "HOLD",
  "target_price": <float or null if HOLD>,
  "stop_loss": <float or null if HOLD>,
  "confidence": <float between 0.0 and 1.0>,
  "rationale": "<max 2 sentences: what pattern/confluence triggered this signal>"
}

If you output ANYTHING outside this JSON object, you have failed your function."""

# ── Prompt Template ────────────────────────────────────────────────────────────

def build_analysis_prompt(symbol: str, candles: List[Candle], current_ltp: Optional[float]) -> str:
    """Format candle data into a structured prompt for Gemini."""

    candle_rows = []
    for c in candles[-50:]:   # Max 50 candles to control token usage
        candle_rows.append(
            f"  {{'t':'{c.timestamp}','o':{c.open},'h':{c.high},'l':{c.low},'c':{c.close},'v':{c.volume}}}"
        )

    candle_json = "[\n" + ",\n".join(candle_rows) + "\n]"

    ltp_line = f"Current LTP: ₹{current_ltp}" if current_ltp else "Current LTP: unavailable (use last close)"

    return f"""ANALYSIS REQUEST
================
Symbol: {symbol}
Exchange: NSE
Timeframe: 15-minute candles
Analysis Time: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}
{ltp_line}

OHLCV DATA (chronological, latest last):
{candle_json}

Analyze the above data and output your JSON signal now."""


# ── Gemini Service ────────────────────────────────────────────────────────────

class GeminiEngine:

    def __init__(self):
        self._client: Optional[genai.Client] = None
        self.is_connected: bool = False
        self._call_count: int = 0
        self._last_error: Optional[str] = None

    def initialize(self):
        """Initialize the Gemini client. Called once on startup."""
        try:
            self._client = genai.Client(api_key=settings.gemini_api_key)
            self.is_connected = True
            logger.info(f"✅ Gemini client initialized | Model: {settings.gemini_model}")
        except Exception as e:
            logger.error(f"Gemini initialization failed: {e}")
            self.is_connected = False
            self._last_error = str(e)

    async def analyze(
        self,
        symbol: str,
        candles: List[Candle],
        current_ltp: Optional[float] = None,
    ) -> Optional[GeminiSignal]:
        """
        Send candle data to Gemini and parse the JSON signal response.

        Returns a validated GeminiSignal or None on failure.
        """
        if not self.is_connected or not self._client:
            logger.error("Gemini client not initialized.")
            return None

        if len(candles) < 5:
            logger.warning(f"Insufficient candle data for {symbol} ({len(candles)} candles). Skipping.")
            return None

        prompt = build_analysis_prompt(symbol, candles, current_ltp)
        start_ms = int(time.time() * 1000)

        try:
            logger.info(f"Sending {len(candles)} candles for {symbol} to Gemini...")

            # Run blocking SDK call in thread pool to keep async loop free
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.1,        # Low temperature for deterministic output
                    top_p=0.8,
                    max_output_tokens=512,  # Signal JSON is small
                    response_mime_type="application/json",  # Force JSON mode
                ),
            )

            latency_ms = int(time.time() * 1000) - start_ms
            raw_text = response.text.strip()
            logger.debug(f"Gemini raw response ({latency_ms}ms): {raw_text}")

            # Parse and validate
            signal = self._parse_response(raw_text, symbol)
            if signal:
                self._call_count += 1
                logger.info(
                    f"📊 Gemini | {symbol}: {signal.signal} | "
                    f"Target: {signal.target_price} | SL: {signal.stop_loss} | "
                    f"Confidence: {signal.confidence:.0%} | {latency_ms}ms"
                )

            return signal

        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Gemini API error for {symbol}: {e}", exc_info=True)
            return None

    def _parse_response(self, raw_text: str, symbol: str) -> Optional[GeminiSignal]:
        """Parse and validate Gemini's JSON response."""
        # Strip markdown fences if Gemini wraps despite instructions
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed for {symbol}: {e}\nRaw: {raw_text[:200]}")
            return None

        try:
            signal = GeminiSignal(
                signal=data.get("signal", "HOLD").upper(),
                target_price=data.get("target_price"),
                stop_loss=data.get("stop_loss"),
                confidence=data.get("confidence", 0.5),
                rationale=data.get("rationale", "No rationale provided."),
            )

            # Sanity-check: BUY/SELL must have SL and target
            if signal.signal in ("BUY", "SELL"):
                if not signal.target_price or not signal.stop_loss:
                    logger.warning(
                        f"Gemini returned {signal.signal} for {symbol} without SL/Target. "
                        "Downgrading to HOLD."
                    )
                    signal.signal = "HOLD"

            return signal

        except Exception as e:
            logger.error(f"Signal validation failed for {symbol}: {e}")
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────
gemini_engine = GeminiEngine()
