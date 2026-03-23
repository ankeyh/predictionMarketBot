from pathlib import Path

from bot.dashboard import build_dashboard_summary, render_dashboard_html
from bot.storage import append_csv, save_json


def test_dashboard_summary_reads_bot_files(tmp_path: Path):
    root = tmp_path
    data_dir = root / "data"
    data_dir.mkdir()
    cfg = {
        "telemetry": {"data_dir": "data"},
        "risk": {"starting_cash": 250.0},
    }

    append_csv(
        data_dir / "signals.csv",
        {
            "market_id": "m1",
            "market_type": "crypto_spot",
            "question": "Missouri St. at Texas Winner?",
            "yes_price": 0.07,
            "no_price": 0.98,
            "reference_price": 100.0,
            "probability": 0.05,
            "edge": -0.02,
            "recommendation": "HOLD",
            "confidence": 0.3,
            "change_5m_pct": 0.01,
            "change_1h_pct": 0.02,
            "realized_vol_1h": 0.03,
            "price_change_24h_pct": 4.5,
            "market_cap_rank": 7,
            "momentum_score": 0.42,
            "reasoning": "No edge.",
        },
        [
            "market_id",
            "market_type",
            "question",
            "yes_price",
            "no_price",
            "reference_price",
            "probability",
            "edge",
            "recommendation",
            "confidence",
            "change_5m_pct",
            "change_1h_pct",
            "realized_vol_1h",
            "price_change_24h_pct",
            "market_cap_rank",
            "momentum_score",
            "reasoning",
        ],
    )
    append_csv(
        data_dir / "orders.csv",
        {
            "market_id": "m1",
            "side": "YES",
            "price": 0.25,
            "size": 10,
            "notional": 2.5,
            "status": "filled-paper",
            "probability": 0.7,
            "edge": 0.1,
            "confidence": 0.8,
            "reasoning": "Test order",
        },
        ["market_id", "side", "price", "size", "notional", "status", "probability", "edge", "confidence", "reasoning"],
    )
    save_json(
        data_dir / "state.json",
        {
            "cash": 247.5,
            "day": "2026-03-20",
            "daily_notional": 2.5,
            "realized_pnl": 0.0,
            "positions": [{"market_id": "m1", "side": "BUY", "price": 95.0, "size": 2, "ts": "2026-03-20T12:00:00+00:00"}],
            "closed_positions": [
                {
                    "market_id": "m1",
                    "question": "Missouri St. at Texas Winner?",
                    "side": "YES",
                    "winning_side": "YES",
                    "payout": 10.0,
                    "pnl": 7.5,
                    "settled_at": "2026-03-20T18:00:00+00:00",
                }
            ],
            "last_order_at": {},
        },
    )

    summary = build_dashboard_summary(root, cfg)

    assert summary["counts"]["signals"] == 1
    assert summary["counts"]["orders"] == 1
    assert summary["counts"]["closed_positions"] == 1
    assert summary["status"]["cash"] == 247.5
    assert summary["status"]["unrealized_pnl"] == 10.0
    assert summary["latest_signal"]["market_id"] == "m1"
    assert summary["latest_spot_signal"]["market_id"] == "m1"
    assert summary["open_positions"][0]["current"] == "100.0"
    assert summary["open_positions"][0]["unrealized_pnl"] == "+10.00"
    assert summary["recent_settlements"][0]["market"] == "Missouri St. at Texas Winner?"
    assert summary["recent_settlements"][0]["reason"] == "Settled Yes"
    assert summary["performance"]["closed_count"] == 1
    assert summary["performance"]["win_rate"] == 1.0


def test_dashboard_html_contains_heading():
    html = render_dashboard_html(
        {
            "status": {
                "paused": False,
                "reason": "",
                "cash": 250.0,
                "daily_notional": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "positions": 0,
                "day": "2026-03-20",
                "latest_cron_line": "",
            },
            "counts": {"signals": 0, "orders": 0, "closed_positions": 0, "recommendations": {}},
            "charts": {"recent_signals": [], "close_reasons": {}},
            "latest_signal": None,
            "latest_spot_signal": None,
            "latest_order": None,
            "recent_signals": [],
            "recent_orders": [],
            "open_positions": [],
            "recent_settlements": [],
            "performance": {
                "closed_count": 0,
                "win_rate": 0.0,
                "average_pnl": 0.0,
                "best_pnl": 0.0,
                "worst_pnl": 0.0,
                "close_reasons": {},
            },
            "discord": {"channel_url": "", "commands": ["prediction bot report"]},
        }
    )
    assert "Prediction Bot" in html
    assert "/api/summary" in html
    assert "Discord workflow" in html
    assert "/action/scan" in html
    assert "Recent closes" in html
    assert "Performance snapshot" in html
    assert "Spot signal context" in html
