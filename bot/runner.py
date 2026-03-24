from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .alerts import send_telegram_alert
from .analyzer import build_analyzer
from .config import load_config, load_env_file
from .control import load_control_state, save_control_state
from .dashboard import serve_dashboard
from .decision import derive_order
from .discord_router import parse_discord_command
from .kalshi_check import check_kalshi, format_check
from .paper import PaperBroker
from .storage import append_csv, ensure_dir, save_json
from .venues import build_venue


def _spot_guardrail(snapshot, cfg: dict) -> str:
    guard = cfg.get("execution", {}).get("spot_guardrail", {})
    if not guard.get("enabled", True):
        return ""
    if snapshot.market_type != "crypto_spot":
        return ""

    momentum_score = float(snapshot.extra.get("momentum_score", 0.0) or 0.0)
    change_5m = float(snapshot.change_5m_pct or 0.0)
    change_1h = float(snapshot.extra.get("change_1h_pct", 0.0) or 0.0)
    realized_vol = float(snapshot.extra.get("realized_vol_1h", 0.0) or 0.0)

    if momentum_score < float(guard.get("min_momentum_score", 0.0) or 0.0):
        return "momentum below threshold"
    if change_1h < float(guard.get("min_change_1h_pct", 0.0) or 0.0):
        return "1h drift below threshold"
    if realized_vol < float(guard.get("min_realized_vol_1h", 0.0) or 0.0):
        return "volatility too low"
    if realized_vol > float(guard.get("max_realized_vol_1h", 1.0) or 1.0):
        return "volatility too high"
    if guard.get("require_drift_alignment", True) and change_5m * change_1h < 0:
        return "5m and 1h drift conflict"
    return ""


def process_once(root: Path, cfg: dict) -> int:
    data_dir = root / cfg["telemetry"]["data_dir"]
    ensure_dir(data_dir)
    control = load_control_state(data_dir)
    if control["paused"]:
        print(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "status": "paused",
                    "reason": control["reason"],
                }
            )
        )
        return 0

    venue = build_venue(cfg)
    analyzer = build_analyzer(cfg, root)
    broker = PaperBroker(cfg, data_dir)
    broker.reset_day_if_needed()
    broker.hydrate_positions(venue)
    settled_positions = broker.settle_positions(venue)
    for closed in settled_positions:
        append_csv(
            data_dir / "settlements.csv",
            closed,
            [
                "market_id",
                "question",
                "market_slug",
                "side",
                "winning_side",
                "entry_price",
                "size",
                "notional",
                "payout",
                "pnl",
                "opened_at",
                "settled_at",
                "status",
            ],
        )
        send_telegram_alert(
            f"Prediction market settled\n"
            f"Market: {closed['question']}\n"
            f"Position: {closed['side']}\n"
            f"Winner: {closed['winning_side']}\n"
            f"PnL: {closed['pnl']:.2f}"
        )

    markets = venue.load_markets(cfg)
    snapshot_by_id = {snapshot.market_id: snapshot for snapshot in markets}
    analyses = {}
    signal_count = 0
    blocked_spot_rows = []

    for snapshot in markets:
        blocked_reason = _spot_guardrail(snapshot, cfg)
        if blocked_reason:
            blocked_row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": snapshot.market_id,
                "market_type": snapshot.market_type,
                "question": snapshot.question,
                "reason": blocked_reason,
                "reference_price": snapshot.reference_price,
                "change_5m_pct": snapshot.change_5m_pct,
                "change_1h_pct": snapshot.extra.get("change_1h_pct", ""),
                "realized_vol_1h": snapshot.extra.get("realized_vol_1h", ""),
                "price_change_24h_pct": snapshot.extra.get("price_change_24h_pct", ""),
                "momentum_score": snapshot.extra.get("momentum_score", ""),
            }
            blocked_spot_rows.append(blocked_row)
            append_csv(
                data_dir / "blocked_spot.csv",
                blocked_row,
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
            continue
        analysis = analyzer.analyze(snapshot)
        analyses[snapshot.market_id] = analysis
        signal_row = {
            "market_id": snapshot.market_id,
            "market_type": snapshot.market_type,
            "question": snapshot.question,
            "yes_price": snapshot.yes_price,
            "no_price": snapshot.no_price,
            "reference_price": snapshot.reference_price,
            "probability": round(analysis.probability, 4),
            "edge": round(analysis.edge, 4),
            "recommendation": analysis.recommendation,
            "confidence": round(analysis.confidence, 4),
            "change_5m_pct": snapshot.change_5m_pct,
            "change_1h_pct": snapshot.extra.get("change_1h_pct", ""),
            "realized_vol_1h": snapshot.extra.get("realized_vol_1h", ""),
            "price_change_24h_pct": snapshot.extra.get("price_change_24h_pct", ""),
            "market_cap_rank": snapshot.extra.get("market_cap_rank", ""),
            "momentum_score": snapshot.extra.get("momentum_score", ""),
            "reasoning": analysis.reasoning,
        }
        append_csv(
            data_dir / "signals.csv",
            signal_row,
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

        if analysis.recommendation != "HOLD":
            send_telegram_alert(
                f"Prediction market signal\n"
                f"Market: {snapshot.question}\n"
                f"Recommendation: {analysis.recommendation}\n"
                f"Edge: {analysis.edge:.2f} Conf: {analysis.confidence:.2f}\n"
                f"Reason: {analysis.reasoning}"
            )

    closed_positions = broker.close_positions(snapshot_by_id, analyses, cfg)
    for closed in closed_positions:
        append_csv(
            data_dir / "closures.csv",
            closed,
            [
                "market_id",
                "question",
                "market_slug",
                "market_type",
                "side",
                "winning_side",
                "entry_price",
                "exit_price",
                "size",
                "notional",
                "payout",
                "pnl",
                "opened_at",
                "settled_at",
                "status",
                "close_reason",
            ],
        )
        send_telegram_alert(
            f"Prediction market position closed\n"
            f"Market: {closed['question']}\n"
            f"Position: {closed['side']}\n"
            f"Reason: {closed['close_reason']}\n"
            f"PnL: {closed['pnl']:.2f}"
        )

    closed_market_ids = {row["market_id"] for row in closed_positions}
    for snapshot in markets:
        if snapshot.market_id in closed_market_ids:
            continue
        analysis = analyses.get(snapshot.market_id)
        if not analysis:
            continue
        order = derive_order(snapshot, analysis, cfg)
        if not order or not cfg["execution"]["enabled"]:
            continue

        signal_count += 1
        allowed, reason = broker.can_place(order)
        if allowed:
            fill = venue.execute(order, cfg["execution"]["mode"])
            fill.market_type = snapshot.market_type
            fill.question = snapshot.question
            fill.market_slug = str(snapshot.extra.get("slug", ""))
            fill.end_date_iso = str(snapshot.extra.get("end_date_iso", ""))
            broker.apply_fill(fill)
        else:
            fill = broker.reject_fill(order, reason)

        append_csv(
            data_dir / "orders.csv",
            {
                "market_id": fill.market_id,
                "side": fill.side,
                "price": fill.price,
                "size": fill.size,
                "notional": fill.notional,
                "status": fill.status,
                "probability": order.probability,
                "edge": order.edge,
                "confidence": order.confidence,
                "reasoning": order.reasoning,
            },
            ["market_id", "side", "price", "size", "notional", "status", "probability", "edge", "confidence", "reasoning"],
        )
        send_telegram_alert(
            f"Prediction market scan\n"
            f"Market: {snapshot.question}\n"
            f"Signal: {order.side} @ {order.price:.2f}\n"
            f"Edge: {order.edge:.2f} Conf: {order.confidence:.2f}\n"
            f"Status: {fill.status}"
        )

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "markets_scanned": len(markets),
        "signals_emitted": signal_count,
        "blocked_spot_markets": len(blocked_spot_rows),
    }
    save_json(data_dir / "last_scan.json", payload)
    print(json.dumps(payload))

    return signal_count


def _load_json_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    import csv

    with open(path, "r", encoding="utf-8") as handle:
        rows = []
        for row in csv.DictReader(handle):
            if None in row:
                row.pop(None, None)
            rows.append(row)
        return rows


def _report_performance(root: Path, cfg: dict) -> dict:
    data_dir = root / cfg["telemetry"]["data_dir"]
    state = PaperBroker(cfg, data_dir).state
    closed_positions = state.get("closed_positions", [])
    total = len(closed_positions)
    if total == 0:
        return {
            "closed_count": 0,
            "win_rate": 0.0,
            "average_pnl": 0.0,
            "best_pnl": 0.0,
            "worst_pnl": 0.0,
        }
    pnls = [float(row.get("pnl", 0.0) or 0.0) for row in closed_positions]
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": total,
        "win_rate": wins / total,
        "average_pnl": sum(pnls) / total,
        "best_pnl": max(pnls),
        "worst_pnl": min(pnls),
    }


def command_status(root: Path, cfg: dict) -> None:
    print(format_status(root, cfg))


def format_status(root: Path, cfg: dict) -> str:
    data_dir = root / cfg["telemetry"]["data_dir"]
    control = load_control_state(data_dir)
    broker = PaperBroker(cfg, data_dir)
    payload = {
        "paused": control["paused"],
        "reason": control["reason"],
        "cash": broker.state["cash"],
        "daily_notional": broker.state["daily_notional"],
        "positions": len(broker.state["positions"]),
    }
    return json.dumps(payload, indent=2)


def command_report(root: Path, cfg: dict) -> None:
    print(format_report(root, cfg))


def format_report(root: Path, cfg: dict) -> str:
    data_dir = root / cfg["telemetry"]["data_dir"]
    signals = _load_json_rows(data_dir / "signals.csv")
    orders = _load_json_rows(data_dir / "orders.csv")
    performance = _report_performance(root, cfg)
    report = {
        "signal_count": len(signals),
        "order_count": len(orders),
        "latest_signal": signals[-1] if signals else None,
        "latest_order": orders[-1] if orders else None,
    }
    latest_signal = report["latest_signal"]
    latest_order = report["latest_order"]

    lines = [
        "Prediction Bot Report",
        f"Signals logged: {report['signal_count']}",
        f"Orders logged: {report['order_count']}",
        f"Closed trades: {performance['closed_count']}",
        f"Win rate: {performance['win_rate'] * 100:.0f}%",
        f"Average PnL: {performance['average_pnl']:.2f}",
    ]
    if latest_signal:
        lines.extend(
            [
                "",
                "Latest signal:",
                f"  Market: {latest_signal.get('question', 'n/a')}",
                f"  Type: {latest_signal.get('market_type', 'n/a')}",
                f"  Recommendation: {latest_signal.get('recommendation', 'n/a')}",
                f"  Probability: {latest_signal.get('probability', 'n/a')}",
                f"  Edge: {latest_signal.get('edge', 'n/a')}",
                f"  Confidence: {latest_signal.get('confidence', 'n/a')}",
                f"  Reasoning: {latest_signal.get('reasoning', 'n/a')}",
            ]
        )
    else:
        lines.extend(["", "Latest signal: none yet"])

    if latest_order:
        lines.extend(
            [
                "",
                "Latest order:",
                f"  Market: {latest_order.get('market_id', 'n/a')}",
                f"  Side: {latest_order.get('side', 'n/a')}",
                f"  Status: {latest_order.get('status', 'n/a')}",
                f"  Notional: {latest_order.get('notional', 'n/a')}",
            ]
        )
    else:
        lines.extend(["", "Latest order: none yet"])

    return "\n".join(lines)


def command_pause(root: Path, cfg: dict, reason: str) -> None:
    print(format_pause(root, cfg, reason))


def format_pause(root: Path, cfg: dict, reason: str) -> str:
    data_dir = root / cfg["telemetry"]["data_dir"]
    state = save_control_state(data_dir, paused=True, reason=reason)
    send_telegram_alert(f"Prediction bot paused. Reason: {state['reason'] or 'manual pause'}")
    return json.dumps(state, indent=2)


def command_resume(root: Path, cfg: dict) -> None:
    print(format_resume(root, cfg))


def format_resume(root: Path, cfg: dict) -> str:
    data_dir = root / cfg["telemetry"]["data_dir"]
    state = save_control_state(data_dir, paused=False, reason="")
    send_telegram_alert("Prediction bot resumed.")
    return json.dumps(state, indent=2)


def command_check_kalshi(cfg: dict, require_auth: bool) -> None:
    base_url = cfg["venue"].get("kalshi_base_url") or "https://demo-api.kalshi.co/trade-api/v2"
    print(format_check(check_kalshi(base_url=base_url, require_auth=require_auth)))


def command_discord(root: Path, cfg: dict, text: str) -> None:
    parsed = parse_discord_command(text)
    if not parsed:
        print(
            "Unknown Discord command. Try: prediction bot report, prediction bot status, "
            "scan markets now, pause prediction bot, resume prediction bot"
        )
        return

    if parsed.action == "report":
        print(format_report(root, cfg))
    elif parsed.action == "status":
        print(format_status(root, cfg))
    elif parsed.action == "scan":
        process_once(root, cfg)
        print()
        print(format_report(root, cfg))
    elif parsed.action == "pause":
        print(format_pause(root, cfg, parsed.reason))
    elif parsed.action == "resume":
        print(format_resume(root, cfg))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--once", action="store_true")

    subparsers.add_parser("status")
    subparsers.add_parser("report")
    dashboard_parser = subparsers.add_parser("serve-dashboard")
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=int, default=8008)
    pause_parser = subparsers.add_parser("pause")
    pause_parser.add_argument("--reason", default="manual pause")
    subparsers.add_parser("resume")
    check_parser = subparsers.add_parser("check-kalshi")
    check_parser.add_argument("--auth", action="store_true")
    discord_parser = subparsers.add_parser("discord-command")
    discord_parser.add_argument("--text", required=True)

    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_env_file(root / ".env")
    cfg = load_config(root / args.config)
    if args.command == "scan":
        process_once(root, cfg)
    elif args.command == "status":
        command_status(root, cfg)
    elif args.command == "report":
        command_report(root, cfg)
    elif args.command == "serve-dashboard":
        serve_dashboard(root, cfg, host=args.host, port=args.port)
    elif args.command == "pause":
        command_pause(root, cfg, args.reason)
    elif args.command == "resume":
        command_resume(root, cfg)
    elif args.command == "check-kalshi":
        command_check_kalshi(cfg, args.auth)
    elif args.command == "discord-command":
        command_discord(root, cfg, args.text)
