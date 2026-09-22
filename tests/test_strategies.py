import asyncio
from types import SimpleNamespace

from algotrader.domain import SignalAction
from algotrader.strategies.ema_crossover import EmaCrossoverStrategy
from algotrader.strategies.gemini import GeminiStrategy, parse_llm_response
from algotrader.strategies.indicators import atr, ema
from tests.conftest import INST, candles_from_closes


def test_ema_matches_manual():
    assert ema([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
    assert ema([1], 3) == []


def test_atr_needs_period_plus_one():
    bars = candles_from_closes([100] * 5)
    assert atr(bars, 5) is None
    assert atr(bars, 4) is not None


async def test_ema_crossover_buy_levels():
    closes = [100 - i * 0.5 for i in range(30)] + [90 + i * 1.5 for i in range(1, 8)]
    s = EmaCrossoverStrategy()
    signals = []
    for n in range(s.warmup, len(closes) + 1):
        signals.append(await s.generate(INST, candles_from_closes(closes[:n]), None))
    buys = [x for x in signals if x.action is SignalAction.BUY]
    assert len(buys) == 1
    b = buys[0]
    assert b.stop_loss < b.reference_price < b.target
    assert abs((b.target - b.reference_price) - 2 * (b.reference_price - b.stop_loss)) < 0.02


async def test_ema_crossover_holds_during_warmup():
    s = EmaCrossoverStrategy()
    sig = await s.generate(INST, candles_from_closes([100] * 5), None)
    assert sig.action is SignalAction.HOLD


def test_parse_llm_response_handles_fences_and_case():
    p = parse_llm_response('```json\n{"signal":"buy","target_price":110,"stop_loss":95,"confidence":0.7,'
                           '"rationale":"x"}\n```')
    assert p.signal == "BUY" and p.target_price == 110


class FakeModels:
    def __init__(self, text=None, delay=0.0, exc=None):
        self.text, self.delay, self.exc = text, delay, exc

    async def generate_content(self, **kw):
        await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return SimpleNamespace(text=self.text)


def gemini(**kw):
    client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels(**kw)))
    return GeminiStrategy("", "m", timeout=0.05, client=client)


BARS = candles_from_closes([100, 101, 102, 101, 103, 104])


async def test_gemini_valid_signal():
    s = gemini(text='{"signal":"BUY","target_price":110,"stop_loss":100,"confidence":0.8,"rationale":"r"}')
    sig = await s.generate(INST, BARS, 104.0)
    assert sig.action is SignalAction.BUY and sig.reference_price == 104.0


async def test_gemini_degrades_to_hold_on_bad_output():
    for text in ["not json", '{"signal":"MAYBE"}', '{"signal":"BUY","confidence":0.9}',
                 '{"signal":"SELL","target_price":-1,"stop_loss":5}']:
        sig = await gemini(text=text).generate(INST, BARS, 104.0)
        assert sig.action is SignalAction.HOLD, text


async def test_gemini_timeout_and_error_hold():
    assert (await gemini(text="{}", delay=1.0).generate(INST, BARS, None)).rationale == "model timeout"
    assert (await gemini(exc=RuntimeError("quota")).generate(INST, BARS, None)).rationale == "model error"
