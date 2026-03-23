from bot.decision import derive_order
from bot.models import AnalysisResult, MarketSnapshot


BASE_CFG = {
    "venue": {"min_edge": 0.10, "min_confidence": 0.70},
    "execution": {"max_order_notional": 25.0},
    "risk": {"max_single_position_notional": 25.0},
}


def test_buy_yes_order_created():
    snapshot = MarketSnapshot(
        market_id="btc-5m-up",
        market_type="crypto_price",
        question="Will BTC/USD be higher in 5 minutes?",
        yes_price=0.45,
        no_price=0.55,
        reference_symbol="BTCUSD",
    )
    analysis = AnalysisResult(
        probability=0.66,
        edge=0.21,
        recommendation="BUY_YES",
        confidence=0.82,
        reasoning="Momentum improved after the last candle.",
    )

    order = derive_order(snapshot, analysis, BASE_CFG)

    assert order is not None
    assert order.side == "YES"
    assert order.market_id == "btc-5m-up"
    assert order.size > 0


def test_hold_filtered_out():
    snapshot = MarketSnapshot(
        market_id="btc-5m-up",
        market_type="crypto_price",
        question="Will BTC/USD be higher in 5 minutes?",
        yes_price=0.49,
        no_price=0.51,
        reference_symbol="BTCUSD",
    )
    analysis = AnalysisResult(
        probability=0.50,
        edge=0.01,
        recommendation="HOLD",
        confidence=0.55,
        reasoning="No clear edge.",
    )

    assert derive_order(snapshot, analysis, BASE_CFG) is None
