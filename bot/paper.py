from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AnalysisResult, Fill, MarketSnapshot, OrderIntent, utc_now_iso
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

    def close_positions(
        self,
        snapshots: dict[str, MarketSnapshot],
        analyses: dict[str, AnalysisResult],
        cfg: dict,
    ) -> list[dict]:
        closed: list[dict] = []
        remaining_positions = []
        exit_rules = cfg.get("execution", {}).get("paper_exit_rules", {})
        now = datetime.now(timezone.utc)

        for position in self.state["positions"]:
            snapshot = snapshots.get(position.get("market_id", ""))
            if not snapshot:
                remaining_positions.append(position)
                continue

            rules = exit_rules.get(snapshot.market_type) or exit_rules.get("default") or {}
            if not rules:
                remaining_positions.append(position)
                continue

            current_price = snapshot.yes_price if position.get("side") == "YES" else snapshot.no_price
            if current_price <= 0:
                remaining_positions.append(position)
                continue

            entry_price = float(position.get("price", 0.0) or 0.0)
            size = float(position.get("size", 0.0) or 0.0)
            notional = float(position.get("notional", entry_price * size) or 0.0)
            pnl = round((current_price - entry_price) * size, 4)
            pnl_pct = ((current_price - entry_price) / entry_price) if entry_price > 0 else 0.0
            reason = self._close_reason(position, snapshot, analyses.get(snapshot.market_id), pnl_pct, now, rules)
            if not reason:
                remaining_positions.append(position)
                continue

            proceeds = round(current_price * size, 4)
            closed_row = {
                "market_id": position["market_id"],
                "question": position.get("question", snapshot.question),
                "market_slug": position.get("market_slug", snapshot.extra.get("slug", "")),
                "market_type": position.get("market_type", snapshot.market_type),
                "side": position["side"],
                "winning_side": "",
                "entry_price": round(entry_price, 4),
                "exit_price": round(current_price, 4),
                "size": round(size, 4),
                "notional": round(notional, 4),
                "payout": proceeds,
                "pnl": pnl,
                "opened_at": position.get("ts", ""),
                "settled_at": utc_now_iso(),
                "status": "closed",
                "close_reason": reason,
            }
            self.state["cash"] += proceeds
            self.state["realized_pnl"] += pnl
            self.state.setdefault("last_order_at", {})[position["market_id"]] = utc_now_iso()
            self.state["closed_positions"].append(closed_row)
            closed.append(closed_row)

        self.state["positions"] = remaining_positions
        if closed:
            self.save()
        return closed

    @staticmethod
    def _close_reason(
        position: dict,
        snapshot: MarketSnapshot,
        analysis: AnalysisResult | None,
        pnl_pct: float,
        now: datetime,
        rules: dict,
    ) -> str:
        stop_loss_pct = float(rules.get("stop_loss_pct", 0.0) or 0.0)
        take_profit_pct = float(rules.get("take_profit_pct", 0.0) or 0.0)
        max_hold_minutes = int(rules.get("max_hold_minutes", 0) or 0)
        min_exit_confidence = float(rules.get("min_exit_confidence", 0.0) or 0.0)
        exit_on_opposite_signal = bool(rules.get("exit_on_opposite_signal", False))

        if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
            return "stop_loss"
        if take_profit_pct > 0 and pnl_pct >= take_profit_pct:
            return "take_profit"

        if exit_on_opposite_signal and analysis and analysis.confidence >= min_exit_confidence:
            if position.get("side") == "YES" and analysis.recommendation == "BUY_NO":
                return "opposite_signal"
            if position.get("side") == "NO" and analysis.recommendation == "BUY_YES":
                return "opposite_signal"

        if max_hold_minutes > 0:
            opened_at = position.get("ts", "")
            try:
                opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            except ValueError:
                opened = None
            if opened and (now - opened).total_seconds() >= max_hold_minutes * 60:
                return "max_hold"

        return ""

    def reject_fill(self, intent: OrderIntent, reason: str) -> Fill:
        return Fill(
            market_id=intent.market_id,
            market_type="",
            side=intent.side,
            price=intent.price,
            size=intent.size,
            notional=round(intent.size * intent.price, 4),
            status=f"rejected:{reason}",
            ts=utc_now_iso(),
        )
