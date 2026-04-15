from bot.analyzer import CandleAnalyzer, RoutingAnalyzer
from bot.models import MarketSnapshot


def test_candle_analyzer_creates_buy_signal_for_bullish_setup():
    analyzer = CandleAnalyzer()
    snapshot = MarketSnapshot(
        market_id="BTC/USD",
        market_type="crypto_spot",
        question="BTC/USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="BTCUSD",
        reference_price=105000.0,
        change_5m_pct=0.004,
        extra={
            "setup_score": 0.46,
            "change_1h_pct": 0.012,
            "change_15m_pct": 0.006,
            "change_4h_pct": 0.021,
            "price_change_24h_pct": 5.5,
            "ema_fast_9": 104200.0,
            "ema_slow_21": 103400.0,
            "ema_spread_pct": 0.0077,
            "ema_15m_spread_pct": 0.005,
            "ema_1h_spread_pct": 0.011,
            "rsi_14": 59.0,
            "atr_pct": 0.011,
            "candle_bias": 0.52,
            "breakout_pct": 0.004,
        },
    )

    result = analyzer.analyze(snapshot)

    assert result.recommendation == "BUY_YES"
    assert result.edge > 0
    assert result.confidence >= 0.3
    assert "setup score" in result.reasoning


def test_candle_analyzer_holds_weak_setup():
    analyzer = CandleAnalyzer()
    snapshot = MarketSnapshot(
        market_id="XRP/USD",
        market_type="crypto_spot",
        question="XRP/USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="XRPUSD",
        reference_price=1.2,
        change_5m_pct=-0.001,
        extra={
            "setup_score": 0.08,
            "change_1h_pct": -0.002,
            "change_15m_pct": -0.001,
            "change_4h_pct": -0.008,
            "price_change_24h_pct": 0.3,
            "ema_fast_9": 1.19,
            "ema_slow_21": 1.21,
            "ema_spread_pct": -0.004,
            "ema_15m_spread_pct": -0.003,
            "ema_1h_spread_pct": -0.006,
            "rsi_14": 43.0,
            "atr_pct": 0.032,
            "candle_bias": -0.25,
            "breakout_pct": -0.01,
        },
    )

    result = analyzer.analyze(snapshot)

    assert result.recommendation == "HOLD"


def test_candle_analyzer_creates_bearish_signal_for_short_setup():
    analyzer = CandleAnalyzer()
    snapshot = MarketSnapshot(
        market_id="SOL/USD",
        market_type="crypto_spot",
        question="SOL-USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="SOLUSD",
        reference_price=90.0,
        change_5m_pct=-0.004,
        extra={
            "setup_score": -0.46,
            "change_1h_pct": -0.012,
            "change_15m_pct": -0.006,
            "change_4h_pct": -0.021,
            "price_change_24h_pct": -5.5,
            "ema_fast_9": 89.4,
            "ema_slow_21": 90.8,
            "ema_spread_pct": -0.015,
            "ema_15m_spread_pct": -0.011,
            "ema_1h_spread_pct": -0.019,
            "rsi_14": 44.0,
            "atr_pct": 0.011,
            "candle_bias": -0.42,
            "breakout_pct": -0.006,
        },
    )

    result = analyzer.analyze(snapshot)

    assert result.recommendation == "BUY_NO"


def test_candle_analyzer_blocks_bullish_setup_on_strong_macro_risk_off():
    analyzer = CandleAnalyzer()
    snapshot = MarketSnapshot(
        market_id="BTC/USD",
        market_type="crypto_spot",
        question="BTC/USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="BTCUSD",
        reference_price=105000.0,
        change_5m_pct=0.004,
        extra={
            "setup_score": 0.46,
            "change_1h_pct": 0.012,
            "change_15m_pct": 0.006,
            "change_4h_pct": 0.021,
            "price_change_24h_pct": 5.5,
            "ema_fast_9": 104200.0,
            "ema_slow_21": 103400.0,
            "ema_spread_pct": 0.0077,
            "ema_15m_spread_pct": 0.005,
            "ema_1h_spread_pct": 0.011,
            "rsi_14": 59.0,
            "atr_pct": 0.011,
            "candle_bias": 0.52,
            "breakout_pct": 0.004,
            "macro_regime_score": -0.62,
            "macro_market_score": -0.51,
            "macro_news_score": -0.40,
            "macro_mode": "risk_off",
        },
    )

    result = analyzer.analyze(snapshot)

    assert result.recommendation == "HOLD"


def test_routing_analyzer_uses_prediction_analyzer_for_sports():
    class StubAnalyzer:
        def __init__(self, recommendation: str):
            self.recommendation = recommendation

        def analyze(self, snapshot):
            return type(
                "Result",
                (),
                {
                    "probability": 0.6,
                    "edge": 0.2,
                    "recommendation": self.recommendation,
                    "confidence": 0.5,
                    "reasoning": "stub",
                },
            )()

    analyzer = RoutingAnalyzer(
        spot_analyzer=StubAnalyzer("BUY_YES"),
        default_analyzer=StubAnalyzer("HOLD"),
        prediction_analyzer=StubAnalyzer("BUY_NO"),
    )
    snapshot = MarketSnapshot(
        market_id="ipl-1",
        market_type="sports",
        question="Mumbai Indians vs Chennai Super Kings winner?",
        yes_price=0.52,
        no_price=0.48,
        reference_symbol="SPORTS",
    )

    result = analyzer.analyze(snapshot)

    assert result.recommendation == "BUY_NO"
