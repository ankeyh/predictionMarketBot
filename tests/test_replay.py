from pathlib import Path

from bot.models import AnalysisResult, MarketSnapshot
from bot.replay import reconcile_replay_positions, register_blocked_replay, replay_summary


def _cfg() -> dict:
    return {
        "venue": {
            "min_edge": 0.10,
            "min_confidence": 0.70,
            "paper_overrides": {
                "crypto_spot": {"min_edge": 0.03, "min_confidence": 0.30}
            },
        },
        "risk": {
            "max_single_position_notional": 25.0,
        },
        "execution": {
            "mode": "paper",
            "max_order_notional": 25.0,
            "paper_exit_rules": {
                "crypto_spot": {
                    "take_profit_pct": 0.04,
                    "stop_loss_pct": 0.025,
                    "max_hold_minutes": 120,
                    "exit_on_opposite_signal": True,
                    "min_exit_confidence": 0.40,
                }
            }
        },
    }


def test_register_blocked_replay_opens_shadow_trade(tmp_path: Path):
    snapshot = MarketSnapshot(
        market_id="XRP/USD",
        market_type="crypto_spot",
        question="XRP-USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="XRPUSD",
        reference_price=1.0,
        change_5m_pct=0.001,
        extra={"spot_price": 1.0, "setup_score": 0.42, "momentum_score": 0.33},
    )
    analysis = AnalysisResult(
        probability=0.74,
        edge=0.18,
        recommendation="BUY_YES",
        confidence=0.62,
        reasoning="Blocked but otherwise tradable.",
    )

    row = register_blocked_replay(tmp_path, snapshot, analysis, "volatility too low", _cfg())

    assert row is not None
    assert row["market_id"] == "XRP/USD"
    assert row["side"] == "BUY"
    assert replay_summary(tmp_path)["open_count"] == 1


def test_replay_position_closes_and_counts_as_missed_win(tmp_path: Path):
    snapshot = MarketSnapshot(
        market_id="XRP/USD",
        market_type="crypto_spot",
        question="XRP-USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="XRPUSD",
        reference_price=1.0,
        change_5m_pct=0.001,
        extra={"spot_price": 1.0, "setup_score": 0.42, "momentum_score": 0.33},
    )
    analysis = AnalysisResult(
        probability=0.74,
        edge=0.18,
        recommendation="BUY_YES",
        confidence=0.62,
        reasoning="Blocked but otherwise tradable.",
    )
    register_blocked_replay(tmp_path, snapshot, analysis, "volatility too low", _cfg())

    moved_snapshot = MarketSnapshot(
        market_id="XRP/USD",
        market_type="crypto_spot",
        question="XRP-USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="XRPUSD",
        reference_price=1.05,
        change_5m_pct=0.001,
        extra={"spot_price": 1.05, "setup_score": 0.30, "momentum_score": 0.25},
    )

    closed = reconcile_replay_positions(
        tmp_path,
        {"XRP/USD": moved_snapshot},
        {"XRP/USD": analysis},
        _cfg(),
    )

    assert len(closed) == 1
    assert closed[0]["close_reason"] == "take_profit"
    assert closed[0]["outcome"] == "missed_win"
    summary = replay_summary(tmp_path)
    assert summary["closed_count"] == 1
    assert summary["recent_missed_wins"] == 1
