from __future__ import annotations

import argparse
import copy
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
from .macro import load_macro_context
from .paper import PaperBroker
from .storage import append_csv, ensure_dir, load_json, save_json
from .venues import build_venue


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _adaptive_spot_profile(data_dir: Path, cfg: dict) -> dict:
    adaptive_cfg = cfg.get("execution", {}).get("adaptive_spot", {})
    enabled = adaptive_cfg.get("enabled", True)
    base_guard = copy.deepcopy(cfg.get("execution", {}).get("spot_guardrail", {}))
    base_override = copy.deepcopy(cfg.get("venue", {}).get("paper_overrides", {}).get("crypto_spot", {}))
    profile = {
        "enabled": enabled,
        "mode": "neutral",
        "level": 0,
        "reasons": [],
        "recent_blocked": 0,
        "recent_losses": 0,
        "recent_closed": 0,
        "effective_guardrail": base_guard,
        "effective_override": base_override,
    }
    if not enabled:
        return profile

    blocked_rows = _load_json_rows(data_dir / "blocked_spot.csv")
    recent_window = int(adaptive_cfg.get("recent_window", 12) or 12)
    recent_blocked = blocked_rows[-recent_window:]
    state = load_json(data_dir / "state.json", {"closed_positions": []})
    recent_closed = state.get("closed_positions", [])[-recent_window:]
    recent_losses = [row for row in recent_closed if float(row.get("pnl", 0.0) or 0.0) <= 0]
    stop_losses = [row for row in recent_closed if row.get("close_reason") == "stop_loss"]
    drift_conflicts = [row for row in recent_blocked if "drift conflict" in str(row.get("reason", ""))]

    relax_score = 0
    if len(recent_blocked) >= int(adaptive_cfg.get("min_blocked_to_relax", 6) or 6):
        relax_score += 1
    if len(recent_blocked) >= int(adaptive_cfg.get("strong_blocked_to_relax", 10) or 10):
        relax_score += 1

    tighten_score = 0
    if len(recent_losses) >= int(adaptive_cfg.get("losses_to_tighten", 2) or 2):
        tighten_score += 1
    if len(stop_losses) >= int(adaptive_cfg.get("stop_losses_to_tighten", 2) or 2):
        tighten_score += 1

    level = relax_score - tighten_score
    profile["recent_blocked"] = len(recent_blocked)
    profile["recent_losses"] = len(recent_losses)
    profile["recent_closed"] = len(recent_closed)
    profile["level"] = level

    effective_guard = copy.deepcopy(base_guard)
    effective_override = copy.deepcopy(base_override)
    if level > 0:
        profile["mode"] = "more_active"
        profile["reasons"].append(f"relaxed after {len(recent_blocked)} blocked spot setups")
        effective_guard["min_momentum_score"] = round(
            _clamp(float(base_guard.get("min_momentum_score", 0.25)) - (0.06 * level), 0.10, 0.60), 4
        )
        effective_guard["min_change_1h_pct"] = round(
            _clamp(float(base_guard.get("min_change_1h_pct", 0.0005)) - (0.00025 * level), 0.0, 0.01), 6
        )
        effective_guard["min_realized_vol_1h"] = round(
            _clamp(float(base_guard.get("min_realized_vol_1h", 0.0007)) - (0.0002 * level), 0.0002, 0.05), 6
        )
        effective_override["min_confidence"] = round(
            _clamp(float(base_override.get("min_confidence", 0.30)) - (0.04 * level), 0.20, 0.70), 4
        )
        effective_override["min_edge"] = round(
            _clamp(float(base_override.get("min_edge", 0.03)) - (0.008 * level), 0.015, 0.20), 4
        )
        if len(drift_conflicts) >= max(1, len(recent_blocked) // 2):
            effective_guard["require_drift_alignment"] = False
            profile["reasons"].append("temporarily ignoring drift-alignment conflicts")
    elif level < 0:
        profile["mode"] = "more_cautious"
        profile["reasons"].append(f"tightened after {len(recent_losses)} recent losing closes")
        tighten = abs(level)
        effective_guard["min_momentum_score"] = round(
            _clamp(float(base_guard.get("min_momentum_score", 0.25)) + (0.05 * tighten), 0.10, 0.80), 4
        )
        effective_guard["min_change_1h_pct"] = round(
            _clamp(float(base_guard.get("min_change_1h_pct", 0.0005)) + (0.00025 * tighten), 0.0, 0.01), 6
        )
        effective_override["min_confidence"] = round(
            _clamp(float(base_override.get("min_confidence", 0.30)) + (0.03 * tighten), 0.20, 0.80), 4
        )
        effective_override["min_edge"] = round(
            _clamp(float(base_override.get("min_edge", 0.03)) + (0.008 * tighten), 0.015, 0.25), 4
        )

    profile["effective_guardrail"] = effective_guard
    profile["effective_override"] = effective_override
    save_json(data_dir / "adaptive_profile.json", profile)
    return profile


def _spot_guardrail(snapshot, cfg: dict) -> str:
    guard = cfg.get("execution", {}).get("spot_guardrail", {})
    if not guard.get("enabled", True):
        return ""
    if snapshot.market_type != "crypto_spot":
        return ""

    momentum_score = float(snapshot.extra.get("momentum_score", 0.0) or 0.0)
    setup_score = float(snapshot.extra.get("setup_score", 0.0) or 0.0)
    bearish_setup = setup_score < 0 or momentum_score < 0
    score_basis = abs(setup_score) if snapshot.extra.get("setup_score") not in ("", None) else abs(momentum_score)
    change_5m = float(snapshot.change_5m_pct or 0.0)
    change_1h = float(snapshot.extra.get("change_1h_pct", 0.0) or 0.0)
    realized_vol = float(snapshot.extra.get("realized_vol_1h", 0.0) or 0.0)
    macro_score = float(snapshot.extra.get("macro_regime_score", 0.0) or 0.0)
    min_score = float(guard.get("min_momentum_score", 0.0) or 0.0)
    min_change_1h = float(guard.get("min_change_1h_pct", 0.0) or 0.0)
    min_vol_1h = float(guard.get("min_realized_vol_1h", 0.0) or 0.0)

    if bearish_setup:
        min_score = max(0.10, min_score - 0.07)
        min_change_1h = max(0.0, min_change_1h - 0.0002)
        min_vol_1h = max(0.0003, min_vol_1h - 0.00015)

    if score_basis < min_score:
        return "setup score below threshold"
    if abs(change_1h) < min_change_1h:
        return "1h drift below threshold"
    if realized_vol < min_vol_1h:
        return "volatility too low"
    if realized_vol > float(guard.get("max_realized_vol_1h", 1.0) or 1.0):
        return "volatility too high"
    if not bearish_setup and macro_score <= -0.40:
        return "macro regime conflict"
    if bearish_setup and macro_score >= 0.40:
        return "macro regime conflict"
    if guard.get("require_drift_alignment", True) and not bearish_setup and change_5m * change_1h < 0:
        return "5m and 1h drift conflict"
    return ""


def process_once(root: Path, cfg: dict) -> int:
    data_dir = root / cfg["telemetry"]["data_dir"]
    ensure_dir(data_dir)
    effective_cfg = copy.deepcopy(cfg)
    effective_cfg.setdefault("runtime_context", {})["macro"] = load_macro_context(data_dir, effective_cfg)
    adaptive_profile = _adaptive_spot_profile(data_dir, effective_cfg)
    effective_cfg.setdefault("execution", {})["spot_guardrail"] = adaptive_profile["effective_guardrail"]
    effective_cfg.setdefault("venue", {}).setdefault("paper_overrides", {})["crypto_spot"] = adaptive_profile["effective_override"]
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

    venue = build_venue(effective_cfg)
    analyzer = build_analyzer(effective_cfg, root)
    broker = PaperBroker(effective_cfg, data_dir)
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

    markets = venue.load_markets(effective_cfg)
    snapshot_by_id = {snapshot.market_id: snapshot for snapshot in markets}
    analyses = {}
    signal_count = 0
    blocked_spot_rows = []
    latest_setups = []

    for snapshot in markets:
        blocked_reason = _spot_guardrail(snapshot, effective_cfg)
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
                "change_15m_pct": snapshot.extra.get("change_15m_pct", ""),
                "change_4h_pct": snapshot.extra.get("change_4h_pct", ""),
                "realized_vol_1h": snapshot.extra.get("realized_vol_1h", ""),
                "price_change_24h_pct": snapshot.extra.get("price_change_24h_pct", ""),
                "setup_score": snapshot.extra.get("setup_score", ""),
                "momentum_score": snapshot.extra.get("momentum_score", ""),
            }
            blocked_spot_rows.append(blocked_row)
            latest_setups.append(
                {
                    "market": snapshot.question,
                    "symbol": snapshot.market_id,
                    "recommendation": "BLOCKED",
                    "reason": blocked_reason,
                    "setup_score": snapshot.extra.get("setup_score", ""),
                    "momentum_score": snapshot.extra.get("momentum_score", ""),
                    "confidence": "",
                    "edge": "",
                }
            )
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
                    "change_15m_pct",
                    "change_4h_pct",
                    "realized_vol_1h",
                    "price_change_24h_pct",
                    "setup_score",
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
            "change_15m_pct": snapshot.extra.get("change_15m_pct", ""),
            "change_4h_pct": snapshot.extra.get("change_4h_pct", ""),
            "realized_vol_1h": snapshot.extra.get("realized_vol_1h", ""),
            "ema_spread_pct": snapshot.extra.get("ema_spread_pct", ""),
            "ema_15m_spread_pct": snapshot.extra.get("ema_15m_spread_pct", ""),
            "ema_1h_spread_pct": snapshot.extra.get("ema_1h_spread_pct", ""),
            "rsi_14": snapshot.extra.get("rsi_14", ""),
            "atr_pct": snapshot.extra.get("atr_pct", ""),
            "candle_bias": snapshot.extra.get("candle_bias", ""),
            "breakout_pct": snapshot.extra.get("breakout_pct", ""),
            "setup_score": snapshot.extra.get("setup_score", ""),
            "price_change_24h_pct": snapshot.extra.get("price_change_24h_pct", ""),
            "market_cap_rank": snapshot.extra.get("market_cap_rank", ""),
            "momentum_score": snapshot.extra.get("momentum_score", ""),
            "macro_mode": snapshot.extra.get("macro_mode", ""),
            "macro_regime_score": snapshot.extra.get("macro_regime_score", ""),
            "macro_market_score": snapshot.extra.get("macro_market_score", ""),
            "macro_news_score": snapshot.extra.get("macro_news_score", ""),
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
                "change_15m_pct",
                "change_4h_pct",
                "realized_vol_1h",
                "ema_spread_pct",
                "ema_15m_spread_pct",
                "ema_1h_spread_pct",
                "rsi_14",
                "atr_pct",
                "candle_bias",
                "breakout_pct",
                "setup_score",
                "price_change_24h_pct",
                "market_cap_rank",
                "momentum_score",
                "macro_mode",
                "macro_regime_score",
                "macro_market_score",
                "macro_news_score",
                "reasoning",
            ],
        )
        latest_setups.append(
            {
                "market": snapshot.question,
                "symbol": snapshot.market_id,
                "recommendation": analysis.recommendation,
                "reason": analysis.reasoning,
                "setup_score": snapshot.extra.get("setup_score", ""),
                "momentum_score": snapshot.extra.get("momentum_score", ""),
                "confidence": round(analysis.confidence, 4),
                "edge": round(analysis.edge, 4),
            }
        )

        if analysis.recommendation != "HOLD":
            send_telegram_alert(
                f"Prediction market signal\n"
                f"Market: {snapshot.question}\n"
                f"Recommendation: {analysis.recommendation}\n"
                f"Edge: {analysis.edge:.2f} Conf: {analysis.confidence:.2f}\n"
                f"Reason: {analysis.reasoning}"
            )

    closed_positions = broker.close_positions(snapshot_by_id, analyses, effective_cfg)
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
        order = derive_order(snapshot, analysis, effective_cfg)
        if not order or not effective_cfg["execution"]["enabled"]:
            continue

        signal_count += 1
        allowed, reason = broker.can_place(order)
        if allowed:
            fill = venue.execute(order, effective_cfg["execution"]["mode"])
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

    latest_setups.sort(
        key=lambda row: (
            -float(row.get("setup_score") or 0.0),
            -float(row.get("confidence") or 0.0),
        )
    )
    save_json(
        data_dir / "latest_setups.json",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows": latest_setups[:12],
        },
    )
    updated_profile = _adaptive_spot_profile(data_dir, effective_cfg)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "markets_scanned": len(markets),
        "signals_emitted": signal_count,
        "blocked_spot_markets": len(blocked_spot_rows),
        "adaptive_mode": updated_profile["mode"],
        "adaptive_level": updated_profile["level"],
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
    adaptive = load_json(
        data_dir / "adaptive_profile.json",
        {"mode": "neutral", "level": 0, "recent_blocked": 0, "recent_losses": 0},
    )
    payload = {
        "paused": control["paused"],
        "reason": control["reason"],
        "cash": broker.state["cash"],
        "daily_notional": broker.state["daily_notional"],
        "positions": len(broker.state["positions"]),
        "adaptive_mode": adaptive["mode"],
        "adaptive_level": adaptive["level"],
    }
    return json.dumps(payload, indent=2)


def command_report(root: Path, cfg: dict) -> None:
    print(format_report(root, cfg))


def format_report(root: Path, cfg: dict) -> str:
    data_dir = root / cfg["telemetry"]["data_dir"]
    signals = _load_json_rows(data_dir / "signals.csv")
    orders = _load_json_rows(data_dir / "orders.csv")
    performance = _report_performance(root, cfg)
    adaptive = load_json(
        data_dir / "adaptive_profile.json",
        {"mode": "neutral", "level": 0, "recent_blocked": 0, "recent_losses": 0, "reasons": []},
    )
    macro = load_json(
        data_dir / "macro_snapshot.json",
        {"mode": "neutral", "combined_score": 0.0, "market_score": 0.0, "news_score": 0.0, "notes": []},
    )
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
        f"Adaptive mode: {adaptive['mode']} (level {adaptive['level']})",
        f"Macro mode: {macro.get('mode', 'neutral')} (score {macro.get('combined_score', 0.0):+.2f})",
        f"Recent blocked setups: {adaptive.get('recent_blocked', 0)}",
        f"Recent losing closes: {adaptive.get('recent_losses', 0)}",
    ]
    if adaptive.get("reasons"):
        lines.append(f"Adaptive note: {adaptive['reasons'][0]}")
    if macro.get("notes"):
        lines.append(f"Macro note: {macro['notes'][0]}")
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
