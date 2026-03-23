from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Fill, OrderIntent, utc_now_iso
from .storage import load_json, save_json


class PaperBroker:
    def __init__(self, cfg: dict, data_dir: Path):
        self.cfg = cfg
        self.state_path = data_dir / "state.json"
        self.state = load_json(
            self.state_path,
            {
                "cash": cfg["risk"]["starting_cash"],
                "day": datetime.now(timezone.utc).date().isoformat(),
                "daily_notional": 0.0,
                "realized_pnl": 0.0,
                "positions": [],
                "closed_positions": [],
                "last_order_at": {},
            },
        )
        self.state.setdefault("positions", [])
        self.state.setdefault("closed_positions", [])
        self.state.setdefault("last_order_at", {})
        self.state.setdefault("cash", cfg["risk"]["starting_cash"])
        self.state.setdefault("daily_notional", 0.0)
        self.state.setdefault("realized_pnl", 0.0)
        self.state.setdefault("day", datetime.now(timezone.utc).date().isoformat())

    def save(self) -> None:
        save_json(self.state_path, self.state)

    def reset_day_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["daily_notional"] = 0.0
            self.state["realized_pnl"] = 0.0

    def can_place(self, intent: OrderIntent) -> tuple[bool, str]:
        notional = intent.size * intent.price
        if len(self.state["positions"]) >= self.cfg["risk"]["max_open_positions"]:
            return False, "max open positions reached"
        if notional > self.cfg["risk"]["max_single_position_notional"]:
            return False, "single position notional too large"
        if self.state["daily_notional"] + notional > self.cfg["execution"]["max_daily_notional"]:
            return False, "daily notional limit reached"
        if self.state["cash"] < notional:
            return False, "insufficient cash"
        if self.state["realized_pnl"] <= -abs(self.cfg["risk"]["daily_loss_limit"]):
            return False, "daily loss limit reached"

        if not self.cfg["execution"].get("allow_repeat_market_orders", False):
            for position in self.state["positions"]:
                if position["market_id"] == intent.market_id and position["side"] == intent.side:
                    return False, "existing position already open"

        cooldown_minutes = int(self.cfg["execution"]["cooldown_minutes"])
        last_order_at = self.state.get("last_order_at", {}).get(intent.market_id)
        if last_order_at:
            next_allowed = datetime.fromisoformat(last_order_at) + timedelta(minutes=cooldown_minutes)
            if datetime.now(timezone.utc) < next_allowed:
                return False, "market cooldown active"

        return True, ""

    def apply_fill(self, fill: Fill) -> None:
        self.state["cash"] -= fill.notional
        self.state["daily_notional"] += fill.notional
        self.state["positions"].append(fill.to_dict())
        self.state.setdefault("last_order_at", {})[fill.market_id] = fill.ts
        self.save()

    def settle_positions(self, venue) -> list[dict]:
        closed: list[dict] = []
        remaining_positions = []
        for position in self.state["positions"]:
            settlement = venue.fetch_settlement(position)
            if not settlement:
                remaining_positions.append(position)
                continue

            payout = float(position["size"]) if position["side"] == settlement["winning_side"] else 0.0
            pnl = round(payout - float(position["notional"]), 4)
            self.state["cash"] += payout
            self.state["realized_pnl"] += pnl
            closed_row = {
                "market_id": position["market_id"],
                "question": settlement.get("question", position.get("question", position["market_id"])),
                "market_slug": settlement.get("market_slug", position.get("market_slug", "")),
                "side": position["side"],
                "winning_side": settlement["winning_side"],
                "entry_price": position["price"],
                "size": position["size"],
                "notional": position["notional"],
                "payout": round(payout, 4),
                "pnl": pnl,
                "opened_at": position.get("ts", ""),
                "settled_at": settlement.get("settled_at", utc_now_iso()),
                "status": "settled",
            }
            closed.append(closed_row)
            self.state["closed_positions"].append(closed_row)

        self.state["positions"] = remaining_positions
        if closed:
            self.save()
        return closed

    def hydrate_positions(self, venue) -> None:
        updated = False
        hydrated_positions = []
        for position in self.state["positions"]:
            hydrated = venue.hydrate_position(position)
            if hydrated != position:
                updated = True
            hydrated_positions.append(hydrated)
        if updated:
            self.state["positions"] = hydrated_positions
            self.save()

    def reject_fill(self, intent: OrderIntent, reason: str) -> Fill:
        return Fill(
            market_id=intent.market_id,
            side=intent.side,
            price=intent.price,
            size=intent.size,
            notional=round(intent.size * intent.price, 4),
            status=f"rejected:{reason}",
            ts=utc_now_iso(),
        )
