from pathlib import Path

from bot.runner import _adaptive_spot_profile
from bot.runner import _spot_guardrail
from bot.storage import append_csv, save_json
from bot.models import MarketSnapshot


def test_adaptive_spot_profile_relaxes_after_many_blocked_setups(tmp_path: Path):
    cfg = {
        "execution": {
            "adaptive_spot": {
                "enabled": True,
                "recent_window": 12,
                "min_blocked_to_relax": 6,
                "strong_blocked_to_relax": 10,
                "losses_to_tighten": 2,
                "stop_losses_to_tighten": 2,
            },
            "spot_guardrail": {
                "enabled": True,
                "min_momentum_score": 0.25,
                "min_change_1h_pct": 0.0005,
                "min_realized_vol_1h": 0.0007,
                "max_realized_vol_1h": 0.03,
                "require_drift_alignment": True,
            },
        },
        "venue": {
            "paper_overrides": {
                "crypto_spot": {"min_edge": 0.03, "min_confidence": 0.30}
            }
        },
    }
    for idx in range(8):
        append_csv(
            tmp_path / "blocked_spot.csv",
            {
                "ts": f"2026-03-20T18:0{idx}:00+00:00",
                "market_id": f"m{idx}",
                "market_type": "crypto_spot",
                "question": "Will BTC-USD be higher over the next 2 hours?",
                "reason": "1h drift below threshold",
                "reference_price": 100.0,
                "change_5m_pct": 0.001,
                "change_1h_pct": 0.0001,
                "realized_vol_1h": 0.0009,
                "price_change_24h_pct": 2.0,
                "momentum_score": 0.22,
            },
            [
                "ts",
                "market_id",
                "market_type",
                "question",
                "reason",
                "reference_price",
                "change_5m_pct",
                "change_1h_pct",
                "realized_vol_1h",
                "price_change_24h_pct",
                "momentum_score",
            ],
        )
    save_json(tmp_path / "state.json", {"closed_positions": []})

    profile = _adaptive_spot_profile(tmp_path, cfg)

    assert profile["mode"] == "more_active"
    assert profile["level"] == 1
    assert profile["effective_guardrail"]["min_momentum_score"] < 0.25
    assert profile["effective_override"]["min_confidence"] < 0.30


def test_adaptive_spot_profile_tightens_after_losses(tmp_path: Path):
    cfg = {
        "execution": {
            "adaptive_spot": {
                "enabled": True,
                "recent_window": 12,
                "min_blocked_to_relax": 6,
                "strong_blocked_to_relax": 10,
                "losses_to_tighten": 2,
                "stop_losses_to_tighten": 2,
            },
            "spot_guardrail": {
                "enabled": True,
                "min_momentum_score": 0.25,
                "min_change_1h_pct": 0.0005,
                "min_realized_vol_1h": 0.0007,
                "max_realized_vol_1h": 0.03,
                "require_drift_alignment": True,
            },
        },
        "venue": {
            "paper_overrides": {
                "crypto_spot": {"min_edge": 0.03, "min_confidence": 0.30}
            }
        },
    }
    save_json(
        tmp_path / "state.json",
        {
            "closed_positions": [
                {"pnl": -1.5, "close_reason": "stop_loss"},
                {"pnl": -0.8, "close_reason": "stop_loss"},
                {"pnl": 0.2, "close_reason": "take_profit"},
            ]
        },
    )

    profile = _adaptive_spot_profile(tmp_path, cfg)

    assert profile["mode"] == "more_cautious"
    assert profile["level"] == -2
    assert profile["effective_guardrail"]["min_momentum_score"] > 0.25
    assert profile["effective_override"]["min_edge"] > 0.03


def test_spot_guardrail_is_looser_for_bearish_setups():
    cfg = {
        "execution": {
            "spot_guardrail": {
                "enabled": True,
                "min_momentum_score": 0.25,
                "min_change_1h_pct": 0.0005,
                "min_realized_vol_1h": 0.0007,
                "max_realized_vol_1h": 0.03,
                "require_drift_alignment": True,
            }
        }
    }
    snapshot = MarketSnapshot(
        market_id="XRP/USD",
        market_type="crypto_spot",
        question="XRP-USD candlestick setup (2h)",
        yes_price=0.5,
        no_price=0.5,
        reference_symbol="XRPUSD",
        reference_price=1.38,
        change_5m_pct=0.0002,
        extra={
            "change_1h_pct": -0.0004,
            "realized_vol_1h": 0.00065,
            "momentum_score": -0.06,
            "setup_score": -0.21,
        },
    )

    reason = _spot_guardrail(snapshot, cfg)

    assert reason == ""
