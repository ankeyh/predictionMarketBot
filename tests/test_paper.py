from pathlib import Path

from bot.models import Fill, OrderIntent
from bot.paper import PaperBroker


def test_paper_broker_applies_fill(tmp_path: Path):
    cfg = {
        "risk": {
            "starting_cash": 100.0,
            "max_open_positions": 2,
            "max_single_position_notional": 25.0,
            "daily_loss_limit": 20.0,
        },
        "execution": {
            "max_daily_notional": 50.0,
            "cooldown_minutes": 15,
            "allow_repeat_market_orders": False,
        },
    }
    broker = PaperBroker(cfg, tmp_path)
    intent = OrderIntent(
        market_id="btc-5m-up",
        side="YES",
        price=0.5,
        size=20.0,
        probability=0.7,
        edge=0.2,
        confidence=0.8,
        reasoning="Test order",
    )
    allowed, reason = broker.can_place(intent)
    assert allowed is True
    assert reason == ""

    fill = Fill(
        market_id=intent.market_id,
        market_type="polymarket",
        side=intent.side,
        price=intent.price,
        size=intent.size,
        notional=10.0,
        status="filled-paper",
        ts="2026-03-20T12:00:00+00:00",
    )
    broker.apply_fill(fill)

    assert broker.state["cash"] == 90.0
    assert broker.state["daily_notional"] == 10.0
    assert len(broker.state["positions"]) == 1


def test_paper_broker_rejects_repeat_market_order(tmp_path: Path):
    cfg = {
        "risk": {
            "starting_cash": 100.0,
            "max_open_positions": 2,
            "max_single_position_notional": 25.0,
            "daily_loss_limit": 20.0,
        },
        "execution": {
            "max_daily_notional": 50.0,
            "cooldown_minutes": 15,
            "allow_repeat_market_orders": False,
        },
    }
    broker = PaperBroker(cfg, tmp_path)
    fill = Fill(
        market_id="btc-5m-up",
        market_type="polymarket",
        side="YES",
        price=0.5,
        size=20.0,
        notional=10.0,
        status="filled-paper",
        ts="2026-03-20T12:00:00+00:00",
    )
    broker.apply_fill(fill)

    intent = OrderIntent(
        market_id="btc-5m-up",
        side="YES",
        price=0.5,
        size=20.0,
        probability=0.7,
        edge=0.2,
        confidence=0.8,
        reasoning="Test order",
    )

    allowed, reason = broker.can_place(intent)
    assert allowed is False
    assert reason == "existing position already open"


class _ResolvedVenue:
    def fetch_settlement(self, position):
        return {
            "market_id": position["market_id"],
            "question": "Will BTC hit $1m?",
            "winning_side": "NO",
            "settled_at": "2026-03-21T14:00:00+00:00",
            "market_slug": "will-btc-hit-1m",
        }


def test_paper_broker_settles_winning_position(tmp_path: Path):
    cfg = {
        "risk": {
            "starting_cash": 100.0,
            "max_open_positions": 2,
            "max_single_position_notional": 25.0,
            "daily_loss_limit": 20.0,
        },
        "execution": {
            "max_daily_notional": 50.0,
            "cooldown_minutes": 15,
            "allow_repeat_market_orders": False,
        },
    }
    broker = PaperBroker(cfg, tmp_path)
    broker.apply_fill(
        Fill(
            market_id="btc-1m",
            market_type="polymarket",
            side="NO",
            price=0.4,
            size=10.0,
            notional=4.0,
            status="filled-paper",
            ts="2026-03-20T12:00:00+00:00",
            question="Will BTC hit $1m?",
            market_slug="will-btc-hit-1m",
        )
    )

    closed = broker.settle_positions(_ResolvedVenue())

    assert len(closed) == 1
    assert closed[0]["winning_side"] == "NO"
    assert broker.state["cash"] == 106.0
    assert broker.state["realized_pnl"] == 6.0
    assert broker.state["positions"] == []


def test_paper_broker_closes_position_on_take_profit(tmp_path: Path):
    cfg = {
        "risk": {
            "starting_cash": 100.0,
            "max_open_positions": 2,
            "max_single_position_notional": 25.0,
            "daily_loss_limit": 20.0,
        },
        "execution": {
            "max_daily_notional": 50.0,
            "cooldown_minutes": 15,
            "allow_repeat_market_orders": False,
            "paper_exit_rules": {
                "crypto_price": {
                    "take_profit_pct": 0.10,
                    "stop_loss_pct": 0.05,
                    "max_hold_minutes": 0,
                    "exit_on_opposite_signal": False,
                    "min_exit_confidence": 0.0,
                }
            },
        },
    }
    broker = PaperBroker(cfg, tmp_path)
    broker.apply_fill(
        Fill(
            market_id="btc-range",
            market_type="crypto_price",
            side="YES",
            price=0.40,
            size=10.0,
            notional=4.0,
            status="filled-paper",
            ts="2026-03-20T12:00:00+00:00",
            question="Will BTC close above $80k?",
        )
    )

    closed = broker.close_positions(
        {
            "btc-range": type(
                "Snap",
                (),
                {
                    "market_id": "btc-range",
                    "market_type": "crypto_price",
                    "question": "Will BTC close above $80k?",
                    "yes_price": 0.48,
                    "no_price": 0.52,
                    "extra": {},
                },
            )()
        },
        {},
        cfg,
    )

    assert len(closed) == 1
    assert closed[0]["close_reason"] == "take_profit"
    assert broker.state["positions"] == []
    assert broker.state["cash"] == 100.8
    assert broker.state["realized_pnl"] == 0.8


def test_paper_broker_closes_crypto_spot_position_on_take_profit(tmp_path: Path):
    cfg = {
        "risk": {
            "starting_cash": 1000.0,
            "max_open_positions": 2,
            "max_single_position_notional": 250.0,
            "daily_loss_limit": 50.0,
        },
        "execution": {
            "max_daily_notional": 500.0,
            "cooldown_minutes": 15,
            "allow_repeat_market_orders": False,
            "paper_exit_rules": {
                "crypto_spot": {
                    "take_profit_pct": 0.03,
                    "stop_loss_pct": 0.02,
                    "max_hold_minutes": 0,
                    "exit_on_opposite_signal": False,
                    "min_exit_confidence": 0.0,
                }
            },
        },
    }
    broker = PaperBroker(cfg, tmp_path)
    broker.apply_fill(
        Fill(
            market_id="BTC/USD",
            market_type="alpaca",
            side="BUY",
            price=100.0,
            size=1.0,
            notional=100.0,
            status="paper-alpaca-sim",
            ts="2026-03-20T12:00:00+00:00",
            question="Will BTC-USD be higher over the next 4 hours?",
        )
    )

    closed = broker.close_positions(
        {
            "BTC/USD": type(
                "Snap",
                (),
                {
                    "market_id": "BTC/USD",
                    "market_type": "crypto_spot",
                    "question": "Will BTC-USD be higher over the next 4 hours?",
                    "reference_price": 104.0,
                    "extra": {"spot_price": 104.0},
                },
            )()
        },
        {},
        cfg,
    )

    assert len(closed) == 1
    assert closed[0]["close_reason"] == "take_profit"
    assert closed[0]["pnl"] == 4.0
    assert broker.state["cash"] == 1004.0
