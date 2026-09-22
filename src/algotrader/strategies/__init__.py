from algotrader.config import Settings, StrategyName
from algotrader.strategies.base import Strategy
from algotrader.strategies.ema_crossover import EmaCrossoverStrategy


def build_strategy(settings: Settings, name: StrategyName | None = None) -> Strategy:
    chosen = name or settings.strategy
    if chosen is StrategyName.EMA_CROSSOVER:
        return EmaCrossoverStrategy()
    if chosen is StrategyName.GEMINI:
        from algotrader.strategies.gemini import GeminiStrategy

        return GeminiStrategy(settings.gemini_api_key.get_secret_value(), settings.gemini_model,
                              settings.gemini_timeout_seconds)
    raise ValueError(f"unknown strategy {chosen}")


__all__ = ["EmaCrossoverStrategy", "Strategy", "build_strategy"]
