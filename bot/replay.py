from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decision import derive_order
from .models import AnalysisResult, MarketSnapshot, utc_now_iso
from .paper import PaperBroker
from .storage import load_json, save_json


def load_replay_state(data_dir: Path) -> dict[str, Any]:
    return load_json(
        data_dir / "replay_state.json",
        {
            "open_positions": [],
            "closed_positions": [],
            "last_opened_at": {},
            "last_closed_at": "",
        },
    )


def save_replay_state(data_dir: Path, state: dict[str, Any]) -> None:
    save_json(data_dir / "replay_state.json", state)


def reconcile_replay_positions(
    data_dir: Path,
    snapshots: dict[str, MarketSnapshot],
    analyses: dict[str, AnalysisResult],
    cfg: dict,
) -> list[dict[str, Any]]:
    state = load_replay_state(data_dir)
    open_positions = state.get("open_positions", [])
    if not open_positions:
        return []

    closed: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    exit_rules = cfg.get("execution", {}).get("paper_exit_rules", {})

    for position in open_positions:
        snapshot = snapshots.get(position.get("market_id", ""))
        if not snapshot:
            remaining.append(position)
            continue

        rules = exit_rules.get(snapshot.market_type) or exit_rules.get("default") or {}
        if not rules:
            remaining.append(position)
            continue

        current_price = float(snapshot.reference_price or snapshot.extra.get("spot_price") or 0.0)
        entry_price = float(position.get("price", 0.0) or 0.0)
        size = float(position.get("size", 0.0) or 0.0)
        notional = float(position.get("notional", entry_price * size) or 0.0)
        if current_price <= 0 or entry_price <= 0 or size <= 0:
            remaining.append(position)
            continue

        direction = 1.0 if position.get("side") == "BUY" else -1.0
        pnl = round((current_price - entry_price) * size * direction, 4)
        pnl_pct = (((current_price - entry_price) / entry_price) * direction) if entry_price > 0 else 0.0
        analysis = analyses.get(snapshot.market_id)
        reason = PaperBroker._close_reason(position, snapshot, analysis, pnl_pct, now, rules)
        if not reason:
            remaining.append(position)
            continue

        outcome = "missed_win" if pnl > 0 else "avoided_loss" if pnl < 0 else "flat"
        closed_row = {
            "market_id": position["market_id"],
            "question": position.get("question", snapshot.question),
            "market_type": position.get("market_type", snapshot.market_type),
            "side": position["side"],
            "entry_price": round(entry_price, 4),
            "exit_price": round(current_price, 4),
            "size": round(size, 4),
            "notional": round(notional, 4),
            "pnl": pnl,
            "opened_at": position.get("ts", ""),
            "closed_at": utc_now_iso(),
            "close_reason": reason,
            "blocked_reason": position.get("blocked_reason", ""),
            "recommendation": position.get("recommendation", ""),
            "confidence": position.get("confidence", 0.0),
            "edge": position.get("edge", 0.0),
            "outcome": outcome,
        }
        closed.append(closed_row)
        state.setdefault("closed_positions", []).append(closed_row)
        state["last_closed_at"] = closed_row["closed_at"]

    if closed or len(remaining) != len(open_positions):
        state["open_positions"] = remaining
        save_replay_state(data_dir, state)
    return closed


def register_blocked_replay(
    data_dir: Path,
    snapshot: MarketSnapshot,
    analysis: AnalysisResult,
    blocked_reason: str,
    cfg: dict,
) -> dict[str, Any] | None:
    if snapshot.market_type != "crypto_spot":
        return None

    intent = derive_order(snapshot, analysis, cfg)
    if not intent:
        return None

    state = load_replay_state(data_dir)
    open_positions = state.setdefault("open_positions", [])
    duplicate_open = any(
        position.get("market_id") == intent.market_id and position.get("side") == intent.side for position in open_positions
    )
    if duplicate_open:
        return None

    ts = utc_now_iso()
    row = {
        "market_id": intent.market_id,
        "market_type": snapshot.market_type,
        "question": snapshot.question,
        "side": intent.side,
        "price": round(intent.price, 4),
        "size": round(intent.size, 4),
        "notional": round(intent.price * intent.size, 4),
        "probability": round(intent.probability, 4),
        "edge": round(intent.edge, 4),
        "confidence": round(intent.confidence, 4),
        "blocked_reason": blocked_reason,
        "recommendation": analysis.recommendation,
        "reasoning": analysis.reasoning,
        "ts": ts,
    }
    open_positions.append(row)
    state.setdefault("last_opened_at", {})[intent.market_id] = ts
    save_replay_state(data_dir, state)
    return row


def replay_summary(data_dir: Path, recent_window: int = 12) -> dict[str, Any]:
    state = load_replay_state(data_dir)
    closed = state.get("closed_positions", [])
    recent_closed = closed[-recent_window:]
    wins = [row for row in recent_closed if float(row.get("pnl", 0.0) or 0.0) > 0]
    losses = [row for row in recent_closed if float(row.get("pnl", 0.0) or 0.0) < 0]
    total = len(closed)
    total_pnls = [float(row.get("pnl", 0.0) or 0.0) for row in closed]
    return {
        "open_count": len(state.get("open_positions", [])),
        "closed_count": total,
        "recent_closed_count": len(recent_closed),
        "recent_missed_wins": len(wins),
        "recent_avoided_losses": len(losses),
        "win_rate": (sum(1 for pnl in total_pnls if pnl > 0) / total) if total else 0.0,
        "average_pnl": (sum(total_pnls) / total) if total else 0.0,
        "latest_closed": closed[-1] if closed else None,
        "recent_closed": list(reversed(recent_closed[-8:])),
    }
