from __future__ import annotations

import csv
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .control import load_control_state, save_control_state
from .storage import load_json


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            if None in row:
                row.pop(None, None)
            rows.append(row)
        return rows


def _read_latest_cron_line(data_dir: Path) -> str:
    log_files = sorted(data_dir.glob("cron-*.log"))
    if not log_files:
        fallback = data_dir / "cron.log"
        log_files = [fallback] if fallback.exists() else []
    if not log_files:
        return ""
    lines = log_files[-1].read_text(encoding="utf-8").splitlines()
    return lines[-1] if lines else ""


def _short_market_label(text: str, limit: int = 54) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _format_market_id(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 18:
        return value
    return f"{value[:10]}...{value[-6:]}"


def _format_ts(value: str) -> str:
    if not value:
        return ""
    normalized = value.replace("Z", "+00:00")
    try:
        dt = __import__("datetime").datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_status(value: str) -> str:
    if not value:
        return ""
    if value.startswith("rejected:"):
        reason = value.split(":", 1)[1].replace("_", " ").strip()
        return f"Rejected: {reason}"
    if value.startswith("paper-"):
        return value.replace("-", " ").title()
    if value.startswith("live-"):
        return value.replace("-", " ").title()
    return value.replace("_", " ").strip().title()


def _format_pnl(value: Any) -> str:
    amount = _to_float(value)
    prefix = "+" if amount > 0 else ""
    return f"{prefix}{amount:.2f}"


def _format_pct(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{amount:+.2%}"


def _format_24h_pct(value: Any) -> str:
    return _format_pct(_to_float(value) / 100.0)


def _performance_summary(closed_positions: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(closed_positions)
    if total == 0:
        return {
            "closed_count": 0,
            "win_rate": 0.0,
            "average_pnl": 0.0,
            "best_pnl": 0.0,
            "worst_pnl": 0.0,
            "close_reasons": {},
        }

    pnls = [_to_float(row.get("pnl", 0.0)) for row in closed_positions]
    wins = sum(1 for pnl in pnls if pnl > 0)
    reason_counts: dict[str, int] = {}
    for row in closed_positions:
        raw_reason = row.get("close_reason") or row.get("status") or "unknown"
        if row.get("winning_side"):
            raw_reason = f"settled {row.get('winning_side')}"
        label = _format_status(str(raw_reason))
        reason_counts[label] = reason_counts.get(label, 0) + 1

    return {
        "closed_count": total,
        "win_rate": wins / total,
        "average_pnl": sum(pnls) / total,
        "best_pnl": max(pnls),
        "worst_pnl": min(pnls),
        "close_reasons": reason_counts,
    }


def build_dashboard_summary(root: Path, cfg: dict) -> dict[str, Any]:
    data_dir = root / cfg["telemetry"]["data_dir"]
    signals = _load_csv_rows(data_dir / "signals.csv")
    orders = _load_csv_rows(data_dir / "orders.csv")
    blocked_spot = _load_csv_rows(data_dir / "blocked_spot.csv")
    last_scan = load_json(
        data_dir / "last_scan.json",
        {
            "ts": "",
            "status": "",
            "markets_scanned": 0,
            "signals_emitted": 0,
            "blocked_spot_markets": 0,
            "adaptive_mode": "neutral",
            "adaptive_level": 0,
        },
    )
    adaptive = load_json(
        data_dir / "adaptive_profile.json",
        {
            "mode": "neutral",
            "level": 0,
            "recent_blocked": 0,
            "recent_losses": 0,
            "recent_closed": 0,
            "reasons": [],
            "effective_guardrail": {},
            "effective_override": {},
        },
    )
    state = load_json(
        data_dir / "state.json",
        {
            "cash": cfg["risk"]["starting_cash"],
            "day": "",
            "daily_notional": 0.0,
            "realized_pnl": 0.0,
            "positions": [],
            "last_order_at": {},
        },
    )
    control = load_control_state(data_dir)
    state.setdefault("closed_positions", [])

    recommendation_counts: dict[str, int] = {}
    closed_positions = state.get("closed_positions", [])
    performance = _performance_summary(closed_positions)
    for row in signals:
        recommendation = row.get("recommendation", "UNKNOWN") or "UNKNOWN"
        recommendation_counts[recommendation] = recommendation_counts.get(recommendation, 0) + 1

    market_labels = {
        row.get("market_id", ""): row.get("question", "") or row.get("market_id", "")
        for row in signals
        if row.get("market_id")
    }
    latest_signal_by_market = {}
    for row in signals:
        market_id = row.get("market_id", "")
        if market_id:
            latest_signal_by_market[market_id] = row

    recent_signal_chart = []
    for row in signals[-8:]:
        recent_signal_chart.append(
            {
                "question": row.get("question", "Unknown market"),
                "short_question": _short_market_label(row.get("question", "Unknown market"), 42),
                "recommendation": row.get("recommendation", "n/a"),
                "edge": _to_float(row.get("edge")),
                "confidence": _to_float(row.get("confidence")),
                "probability": _to_float(row.get("probability")),
            }
        )

    open_positions = []
    unrealized_total = 0.0
    for position in state.get("positions", []):
        market_id = position.get("market_id", "")
        market_label = (
            position.get("question")
            or market_labels.get(market_id)
            or _format_market_id(market_id)
        )
        latest_row = latest_signal_by_market.get(market_id, {})
        current_price = latest_row.get("reference_price") or latest_row.get("yes_price") or ""
        entry_price = _to_float(position.get("price"))
        size = _to_float(position.get("size"))
        unrealized_pnl = ""
        if position.get("side") == "BUY" and current_price not in {"", None}:
            current_value = _to_float(current_price)
            unrealized = (current_value - entry_price) * size
            unrealized_total += unrealized
            unrealized_pnl = _format_pnl(unrealized)
        open_positions.append(
            {
                "market": market_label,
                "side": position.get("side", ""),
                "price": position.get("price", ""),
                "size": position.get("size", ""),
                "current": current_price,
                "unrealized_pnl": unrealized_pnl,
                "opened": _format_ts(position.get("ts", "")),
            }
        )

    recent_settlements = []
    for closed in reversed(closed_positions[-10:]):
        market_id = closed.get("market_id", "")
        market_label = (
            closed.get("question")
            or market_labels.get(market_id)
            or _format_market_id(market_id)
        )
        close_reason = closed.get("close_reason") or closed.get("status", "")
        if closed.get("winning_side"):
            close_reason = f"settled {closed.get('winning_side')}"
        recent_settlements.append(
            {
                "market": market_label,
                "side": closed.get("side", ""),
                "reason": _format_status(close_reason),
                "payout": closed.get("payout", ""),
                "pnl": _format_pnl(closed.get("pnl", 0.0)),
                "settled_at": _format_ts(closed.get("settled_at", "")),
            }
        )

    recent_orders = []
    for order in reversed(orders[-10:]):
        market_id = order.get("market_id", "")
        market_label = market_labels.get(market_id) or _format_market_id(market_id)
        recent_orders.append(
            {
                **order,
                "market": market_label,
                "status_label": _format_status(order.get("status", "")),
            }
        )

    latest_order = recent_orders[0] if recent_orders else None
    latest_spot_signal = next((row for row in reversed(signals) if row.get("market_type") == "crypto_spot"), None)
    recent_blocked_spot = []
    for row in reversed(blocked_spot[-8:]):
        recent_blocked_spot.append(
            {
                "market": row.get("question", row.get("market_id", "")),
                "reason": _format_status(row.get("reason", "")),
                "momentum": row.get("momentum_score", ""),
                "drift_1h": _format_pct(row.get("change_1h_pct", "")),
                "vol_1h": _format_pct(row.get("realized_vol_1h", "")),
            }
        )

    discord_commands = [
        "prediction bot report",
        "prediction bot status",
        "scan markets now",
        "pause prediction bot",
        "resume prediction bot",
    ]

    return {
        "status": {
            "paused": control["paused"],
            "reason": control["reason"],
            "cash": state.get("cash", cfg["risk"]["starting_cash"]),
            "daily_notional": state.get("daily_notional", 0.0),
            "realized_pnl": state.get("realized_pnl", 0.0),
            "unrealized_pnl": round(unrealized_total, 4),
            "positions": len(state.get("positions", [])),
            "day": state.get("day", ""),
            "latest_cron_line": _read_latest_cron_line(data_dir),
            "last_successful_scan": _format_ts(last_scan.get("ts", "")),
            "last_scan_markets": last_scan.get("markets_scanned", 0),
            "last_scan_blocked": last_scan.get("blocked_spot_markets", 0),
            "adaptive_mode": adaptive.get("mode", "neutral"),
            "adaptive_level": adaptive.get("level", 0),
        },
        "counts": {
            "signals": len(signals),
            "orders": len(orders),
            "closed_positions": len(closed_positions),
            "recommendations": recommendation_counts,
        },
        "charts": {
            "recent_signals": recent_signal_chart,
            "close_reasons": performance["close_reasons"],
        },
        "performance": performance,
        "latest_signal": signals[-1] if signals else None,
        "latest_spot_signal": latest_spot_signal,
        "latest_order": latest_order,
        "recent_signals": list(reversed(signals[-10:])),
        "recent_orders": recent_orders,
        "open_positions": open_positions,
        "recent_settlements": recent_settlements,
        "recent_blocked_spot": recent_blocked_spot,
        "adaptive": adaptive,
        "discord": {
            "channel_url": os.getenv("DISCORD_CHANNEL_URL", ""),
            "commands": discord_commands,
        },
    }


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _card(title: str, value: str, tone: str = "") -> str:
    klass = f"card {tone}".strip()
    return f'<div class="{klass}"><div class="label">{html.escape(title)}</div><div class="value">{html.escape(value)}</div></div>'


def _render_rows(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return '<div class="empty">No data yet.</div>'

    header = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_rows = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            cells.append(f"<td>{html.escape(str(value))}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_recommendation_chart(recommendation_counts: dict[str, int]) -> str:
    if not recommendation_counts:
        return '<div class="empty">No signals yet.</div>'

    total = sum(recommendation_counts.values()) or 1
    rows = []
    tones = {"BUY_YES": "buy", "BUY_NO": "sell", "HOLD": "hold"}
    for name, count in sorted(recommendation_counts.items()):
        width = max(8, round((count / total) * 100))
        tone = tones.get(name, "")
        rows.append(
            f"""
            <div class="chart-row">
              <div class="chart-label">{html.escape(name)}</div>
              <div class="bar-track"><div class="bar-fill {tone}" style="width:{width}%"></div></div>
              <div class="chart-value">{count}</div>
            </div>
            """
        )
    return "".join(rows)


def _render_signal_chart(points: list[dict[str, Any]]) -> str:
    if not points:
        return '<div class="empty">No signal history yet.</div>'

    rows = []
    for point in reversed(points):
        confidence_width = max(6, min(100, round(_to_float(point["confidence"]) * 100)))
        edge_width = max(4, min(100, round(abs(_to_float(point["edge"])) * 100)))
        edge_tone = "negative" if _to_float(point["edge"]) < 0 else "positive"
        rows.append(
            f"""
            <div class="signal-row">
              <div class="signal-market">
                <div class="signal-title">{html.escape(str(point["short_question"]))}</div>
                <div class="signal-meta">{html.escape(str(point["recommendation"]))} · Prob {point["probability"]:.2f}</div>
              </div>
              <div class="signal-metrics">
                <div class="signal-metric">
                  <span class="signal-metric-label">Confidence</span>
                  <div class="bar-track"><div class="bar-fill confidence" style="width:{confidence_width}%"></div></div>
                  <span class="signal-metric-value">{point["confidence"]:.2f}</span>
                </div>
                <div class="signal-metric">
                  <span class="signal-metric-label">Edge</span>
                  <div class="bar-track"><div class="bar-fill edge {edge_tone}" style="width:{edge_width}%"></div></div>
                  <span class="signal-metric-value">{point["edge"]:.2f}</span>
                </div>
              </div>
            </div>
            """
        )
    return f'<div class="signal-chart-rows">{ "".join(rows) }</div><div class="chart-caption">Each row shows the market, recommendation, confidence, and edge across the latest {len(points)} signals.</div>'


def _render_performance_chart(reasons: dict[str, int]) -> str:
    if not reasons:
        return '<div class="empty">No closed trades yet.</div>'

    total = sum(reasons.values()) or 1
    rows = []
    for name, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        width = max(8, round((count / total) * 100))
        rows.append(
            f"""
            <div class="chart-row">
              <div class="chart-label">{html.escape(name)}</div>
              <div class="bar-track"><div class="bar-fill" style="width:{width}%"></div></div>
              <div class="chart-value">{count}</div>
            </div>
            """
        )
    return "".join(rows)


def _render_discord_panel(discord: dict[str, Any]) -> str:
    commands = discord.get("commands", [])
    buttons = []
    for command in commands:
        escaped = html.escape(command)
        buttons.append(
            f'<button class="copy-btn" data-copy="{escaped}">{escaped}</button>'
        )
    channel_url = discord.get("channel_url", "")
    link_html = (
        f'<a class="action-link" href="{html.escape(channel_url)}" target="_blank" rel="noreferrer">Open Discord channel</a>'
        if channel_url
        else '<div class="empty">Optional: set <code>DISCORD_CHANNEL_URL</code> in <code>.env</code> to add a direct channel link here.</div>'
    )
    return f"""
      <div class="meta">
        <div>These are the same phrases your OpenClaw Discord router understands.</div>
        <div>Click a button to copy the command, then paste it into Discord.</div>
      </div>
      <div class="copy-grid">{''.join(buttons)}</div>
      <div style="margin-top:14px;">{link_html}</div>
    """


def render_dashboard_html(summary: dict[str, Any]) -> str:
    status = summary["status"]
    latest_signal = summary["latest_signal"] or {}
    latest_spot_signal = summary.get("latest_spot_signal") or {}
    latest_order = summary["latest_order"] or {}
    performance = summary["performance"]
    adaptive = summary.get("adaptive", {})
    paused_tone = "danger" if status["paused"] else "success"
    paused_text = "Paused" if status["paused"] else "Running"
    recommendation_counts = summary["counts"]["recommendations"]
    recommendation_text = ", ".join(
        f"{name}: {count}" for name, count in sorted(recommendation_counts.items())
    ) or "No signals yet"
    discord = summary["discord"]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prediction Bot Dashboard</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --panel: #fffaf2;
      --ink: #1f1f1b;
      --muted: #726d61;
      --line: #ddd3c2;
      --green: #1f7a4c;
      --red: #9d3b2f;
      --amber: #9a6c15;
      --shadow: 0 18px 42px rgba(42, 31, 16, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      max-width: 100%;
      overflow-x: hidden;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(173, 138, 79, 0.18), transparent 30%),
        linear-gradient(180deg, #f8f3ec 0%, var(--bg) 100%);
    }}
    .shell {{
      max-width: 1200px;
      width: 100%;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: end;
      margin-bottom: 22px;
      min-width: 0;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.5rem);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }}
    .sub {{
      color: var(--muted);
      margin-top: 8px;
      max-width: 760px;
    }}
    .stamp {{
      color: var(--muted);
      font-size: 0.95rem;
      white-space: nowrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
      min-width: 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px 18px 16px;
      box-shadow: var(--shadow);
      min-width: 0;
      overflow: hidden;
    }}
    .card.success {{ border-color: rgba(31, 122, 76, 0.35); }}
    .card.danger {{ border-color: rgba(157, 59, 47, 0.35); }}
    .card.warn {{ border-color: rgba(154, 108, 21, 0.35); }}
    .card.neutral {{ border-color: rgba(125, 100, 61, 0.28); }}
    .label {{
      font-size: 0.85rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
    }}
    .value {{
      font-size: 1.6rem;
      line-height: 1.05;
    }}
    .layout {{
      display: grid;
      grid-template-columns: 1.25fr 0.9fr;
      gap: 18px;
      min-width: 0;
    }}
    .stack {{
      display: grid;
      gap: 18px;
      min-width: 0;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 20px;
      box-shadow: var(--shadow);
      min-width: 0;
      overflow: hidden;
    }}
    .panel h2 {{
      margin: 0 0 14px;
      font-size: 1.25rem;
    }}
    .meta {{
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .reasoning {{
      margin-top: 12px;
      padding: 14px;
      border-radius: 16px;
      background: rgba(125, 100, 61, 0.08);
      color: #3b342b;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
      min-width: 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
      table-layout: fixed;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    th {{
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .empty {{
      color: var(--muted);
      font-style: italic;
    }}
    .pill {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 0.82rem;
      background: rgba(125, 100, 61, 0.1);
      color: #4d4030;
      margin-bottom: 10px;
    }}
    .footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 16px 0 4px;
      min-width: 0;
    }}
    button, .action-link {{
      appearance: none;
      border: none;
      border-radius: 999px;
      padding: 10px 14px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #2f2a24;
      color: #fffaf2;
    }}
    button.secondary {{
      background: #e6dccb;
      color: #2b251e;
    }}
    button.warn {{
      background: #9d3b2f;
    }}
    .status-banner {{
      display: none;
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(31, 122, 76, 0.1);
      color: #214b35;
    }}
    .status-banner.visible {{ display: block; }}
    .chart-row {{
      display: grid;
      grid-template-columns: 88px 1fr 38px;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
    }}
    .chart-label, .chart-value {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .bar-track {{
      height: 12px;
      border-radius: 999px;
      background: rgba(125, 100, 61, 0.11);
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      border-radius: 999px;
      background: #8d7554;
    }}
    .bar-fill.buy {{ background: var(--green); }}
    .bar-fill.sell {{ background: var(--red); }}
    .bar-fill.hold {{ background: var(--amber); }}
    .signal-chart-rows {{
      display: grid;
      gap: 14px;
    }}
    .signal-row {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
      gap: 16px;
      align-items: center;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
    }}
    .signal-row:last-child {{
      border-bottom: none;
    }}
    .signal-market {{
      min-width: 0;
    }}
    .signal-title {{
      font-weight: 700;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .signal-meta {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .signal-metrics {{
      display: grid;
      gap: 10px;
      min-width: 0;
    }}
    .signal-metric {{
      display: grid;
      grid-template-columns: 84px minmax(0, 1fr) 44px;
      gap: 10px;
      align-items: center;
      min-width: 0;
    }}
    .signal-metric-label,
    .signal-metric-value {{
      font-size: 0.86rem;
      color: var(--muted);
    }}
    .bar-fill.confidence {{
      background: #88a9c7;
    }}
    .bar-fill.edge {{
      background: #c49a38;
    }}
    .bar-fill.edge.negative {{
      background: #ba5b4f;
    }}
    .chart-caption {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .copy-grid {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
      min-width: 0;
    }}
    .copy-btn {{
      justify-content: flex-start;
      background: #efe6d7;
      color: #2b251e;
      text-align: left;
      border: 1px solid var(--line);
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .layout {{ grid-template-columns: 1fr; }}
      .kpis {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 640px) {{
      .hero {{ flex-direction: column; align-items: start; }}
      .grid {{ grid-template-columns: 1fr; }}
      .kpis {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 20px 12px 36px; }}
      h1 {{ font-size: clamp(1.8rem, 11vw, 3rem); }}
      .panel, .card {{ padding: 16px; border-radius: 18px; }}
      .signal-row {{ grid-template-columns: 1fr; }}
      .signal-metric {{ grid-template-columns: 72px minmax(0, 1fr) 40px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div>
        <h1>Prediction Bot</h1>
        <div class="sub">Live local view of signals, paper orders, bot status, and recent cron activity. This page refreshes every 15 seconds.</div>
      </div>
      <div class="stamp" id="refresh-stamp">Auto-refreshing</div>
    </div>

    <div class="grid">
      {_card("Bot status", paused_text, paused_tone)}
      {_card("Signals logged", str(summary["counts"]["signals"]))}
      {_card("Orders logged", str(summary["counts"]["orders"]))}
      {_card("Open positions", str(status["positions"]), "warn" if status["positions"] else "")}
      {_card("Closed bets", str(summary["counts"]["closed_positions"]), "neutral")}
      {_card("Last successful scan", str(status["last_successful_scan"] or "No scan yet"), "neutral")}
      {_card("Adaptive mode", f"{status['adaptive_mode']} ({status['adaptive_level']:+d})", "neutral")}
    </div>

    <section class="panel" style="margin-bottom:18px;">
      <h2>Controls</h2>
      <div class="meta">
        <div>Run a manual scan, pause the bot, or resume it without leaving the browser.</div>
      </div>
      <div class="actions">
        <button data-action="/action/scan">Scan now</button>
        <button class="secondary" data-action="/action/resume">Resume bot</button>
        <button class="warn" data-action="/action/pause">Pause bot</button>
      </div>
      <div id="status-banner" class="status-banner"></div>
    </section>

    <div class="layout">
      <div class="stack">
        <section class="panel">
          <div class="pill">Latest signal</div>
          <h2>{html.escape(str(latest_signal.get("question", "No signals yet")))}</h2>
          <div class="meta">
            <div>Type: {html.escape(str(latest_signal.get("market_type", "n/a")))}</div>
            <div>Recommendation: {html.escape(str(latest_signal.get("recommendation", "n/a")))}</div>
            <div>Probability: {html.escape(str(latest_signal.get("probability", "n/a")))}</div>
            <div>Edge: {html.escape(str(latest_signal.get("edge", "n/a")))}</div>
            <div>Confidence: {html.escape(str(latest_signal.get("confidence", "n/a")))}</div>
          </div>
          <div class="reasoning">{html.escape(str(latest_signal.get("reasoning", "No reasoning yet.")))}</div>
        </section>

        <section class="panel">
          <h2>Recent signals</h2>
          {_render_rows(summary["recent_signals"], [("question", "Market"), ("recommendation", "Rec"), ("edge", "Edge"), ("confidence", "Conf"), ("probability", "Prob")])}
        </section>

        <section class="panel">
          <h2>Recent signal chart</h2>
          {_render_signal_chart(summary["charts"]["recent_signals"])}
        </section>

        <section class="panel">
          <h2>Spot signal context</h2>
          <div class="meta">
            <div>Market: {html.escape(str(latest_spot_signal.get("question", "No spot signals yet")))}</div>
            <div>Spot: {html.escape(str(latest_spot_signal.get("reference_price", "n/a")))}</div>
            <div>5m drift: {html.escape(_format_pct(latest_spot_signal.get("change_5m_pct", "")))}</div>
            <div>1h drift: {html.escape(_format_pct(latest_spot_signal.get("change_1h_pct", "")))}</div>
            <div>1h realized vol: {html.escape(_format_pct(latest_spot_signal.get("realized_vol_1h", "")))}</div>
            <div>24h drift: {html.escape(_format_24h_pct(latest_spot_signal.get("price_change_24h_pct", "")))}</div>
            <div>Momentum score: {html.escape(str(latest_spot_signal.get("momentum_score", "n/a")))}</div>
            <div>Rank: {html.escape(str(latest_spot_signal.get("market_cap_rank", "n/a")))}</div>
          </div>
        </section>

        <section class="panel">
          <h2>Recent orders</h2>
          {_render_rows(summary["recent_orders"], [("market", "Market"), ("side", "Side"), ("status_label", "Status"), ("notional", "Notional"), ("confidence", "Conf")])}
        </section>

        <section class="panel">
          <h2>Why no trade</h2>
          {_render_rows(summary["recent_blocked_spot"], [("market", "Market"), ("reason", "Reason"), ("momentum", "Momentum"), ("drift_1h", "1h drift"), ("vol_1h", "1h vol")])}
        </section>

        <section class="panel">
          <h2>Adaptive engine</h2>
          <div class="meta">
            <div>Mode: {html.escape(str(adaptive.get("mode", "neutral")))}</div>
            <div>Level: {html.escape(str(adaptive.get("level", 0)))}</div>
            <div>Recent blocked setups: {html.escape(str(adaptive.get("recent_blocked", 0)))}</div>
            <div>Recent losing closes: {html.escape(str(adaptive.get("recent_losses", 0)))}</div>
            <div>Recent closed trades: {html.escape(str(adaptive.get("recent_closed", 0)))}</div>
            <div>Effective min edge: {html.escape(str(adaptive.get("effective_override", {}).get("min_edge", "n/a")))}</div>
            <div>Effective min confidence: {html.escape(str(adaptive.get("effective_override", {}).get("min_confidence", "n/a")))}</div>
            <div>Effective momentum floor: {html.escape(str(adaptive.get("effective_guardrail", {}).get("min_momentum_score", "n/a")))}</div>
          </div>
          <div class="reasoning">{html.escape(str((adaptive.get("reasons") or ["No adaptive changes yet."])[0]))}</div>
        </section>
      </div>

      <div class="stack">
            <section class="panel">
              <h2>Broker snapshot</h2>
              <div class="kpis">
                {_card("Cash", str(status["cash"]))}
                {_card("Daily notional", str(status["daily_notional"]))}
                {_card("Realized PnL", str(status["realized_pnl"]))}
                {_card("Unrealized PnL", _format_pnl(status["unrealized_pnl"]))}
              </div>
              <div class="meta" style="margin-top: 14px;">
                <div>Day: {html.escape(str(status["day"]))}</div>
                <div>Recommendation mix: {html.escape(recommendation_text)}</div>
                <div>Last scan markets: {html.escape(str(status["last_scan_markets"]))}</div>
                <div>Blocked by guardrail: {html.escape(str(status["last_scan_blocked"]))}</div>
                <div>Pause reason: {html.escape(str(status["reason"] or "Not paused"))}</div>
          </div>
        </section>

        <section class="panel">
          <h2>Performance snapshot</h2>
          <div class="kpis">
            {_card("Win rate", f"{performance['win_rate'] * 100:.0f}%")}
            {_card("Avg PnL", _format_pnl(performance["average_pnl"]))}
            {_card("Best / Worst", f"{_format_pnl(performance['best_pnl'])} / {_format_pnl(performance['worst_pnl'])}")}
          </div>
          <div class="meta" style="margin-top: 14px;">
            <div>Closed trades: {performance['closed_count']}</div>
          </div>
        </section>

        <section class="panel">
          <h2>Recommendation mix</h2>
          {_render_recommendation_chart(recommendation_counts)}
        </section>

        <section class="panel">
          <h2>Close reasons</h2>
          {_render_performance_chart(summary["charts"]["close_reasons"])}
        </section>

        <section class="panel">
          <h2>Latest order</h2>
          <div class="meta">
            <div>Market: {html.escape(str(latest_order.get("market", latest_order.get("market_id", "No orders yet"))))}</div>
            <div>Side: {html.escape(str(latest_order.get("side", "n/a")))}</div>
            <div>Status: {html.escape(str(latest_order.get("status_label", latest_order.get("status", "n/a"))))}</div>
            <div>Notional: {html.escape(str(latest_order.get("notional", "n/a")))}</div>
            <div>Edge: {html.escape(str(latest_order.get("edge", "n/a")))}</div>
            <div>Confidence: {html.escape(str(latest_order.get("confidence", "n/a")))}</div>
          </div>
        </section>

        <section class="panel">
          <h2>Open positions</h2>
          {_render_rows(summary["open_positions"], [("market", "Market"), ("side", "Side"), ("price", "Entry"), ("current", "Current"), ("size", "Size"), ("unrealized_pnl", "Unrealized"), ("opened", "Opened")])}
        </section>

        <section class="panel">
          <h2>Recent closes</h2>
          {_render_rows(summary["recent_settlements"], [("market", "Market"), ("side", "Side"), ("reason", "Reason"), ("payout", "Payout"), ("pnl", "PnL"), ("settled_at", "Closed")])}
        </section>

        <section class="panel">
          <h2>Cron</h2>
          <div class="reasoning">{html.escape(str(status["latest_cron_line"] or "No cron output yet."))}</div>
          <div class="footer">JSON is also available at <code>/api/summary</code>.</div>
        </section>

        <section class="panel">
          <h2>Discord workflow</h2>
          {_render_discord_panel(discord)}
        </section>
      </div>
    </div>
  </div>
  <script>
    const stamp = document.getElementById("refresh-stamp");
    const banner = document.getElementById("status-banner");
    function refreshStamp() {{
      const now = new Date();
      stamp.textContent = "Last paint " + now.toLocaleTimeString();
    }}
    async function callAction(path) {{
      banner.classList.remove("visible");
      const response = await fetch(path, {{ method: "POST" }});
      const payload = await response.json();
      banner.textContent = payload.message || "Done";
      banner.classList.add("visible");
      setTimeout(() => window.location.reload(), 900);
    }}
    document.querySelectorAll("[data-action]").forEach((button) => {{
      button.addEventListener("click", () => callAction(button.dataset.action));
    }});
    document.querySelectorAll("[data-copy]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        await navigator.clipboard.writeText(button.dataset.copy);
        banner.textContent = "Copied Discord command: " + button.dataset.copy;
        banner.classList.add("visible");
      }});
    }});
    refreshStamp();
    setInterval(() => window.location.reload(), 15000);
  </script>
</body>
</html>
"""


def serve_dashboard(root: Path, cfg: dict, host: str = "127.0.0.1", port: int = 8008) -> None:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_bytes(self, payload: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            summary = build_dashboard_summary(root, cfg)
            if self.path in {"/", "/index.html"}:
                payload = render_dashboard_html(summary).encode("utf-8")
                self._send_bytes(payload, "text/html; charset=utf-8")
                self.wfile.write(payload)
                return

            if self.path == "/api/summary":
                payload = json.dumps(summary, indent=2).encode("utf-8")
                self._send_bytes(payload, "application/json; charset=utf-8")
                self.wfile.write(payload)
                return

            self.send_response(404)
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            summary = build_dashboard_summary(root, cfg)
            if self.path in {"/", "/index.html"}:
                payload = render_dashboard_html(summary).encode("utf-8")
                self._send_bytes(payload, "text/html; charset=utf-8")
                return

            if self.path == "/api/summary":
                payload = json.dumps(summary, indent=2).encode("utf-8")
                self._send_bytes(payload, "application/json; charset=utf-8")
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            data_dir = root / cfg["telemetry"]["data_dir"]
            if self.path == "/action/scan":
                from .runner import process_once

                count = process_once(root, cfg)
                self._send_json(
                    {
                        "ok": True,
                        "message": f"Manual scan finished. Signals emitted: {count}",
                        "summary": build_dashboard_summary(root, cfg),
                    }
                )
                return

            if self.path == "/action/pause":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(raw)
                reason = form.get("reason", ["dashboard pause"])[0]
                save_control_state(data_dir, paused=True, reason=reason)
                self._send_json(
                    {
                        "ok": True,
                        "message": f"Bot paused: {reason}",
                        "summary": build_dashboard_summary(root, cfg),
                    }
                )
                return

            if self.path == "/action/resume":
                save_control_state(data_dir, paused=False, reason="")
                self._send_json(
                    {
                        "ok": True,
                        "message": "Bot resumed.",
                        "summary": build_dashboard_summary(root, cfg),
                    }
                )
                return

            self.send_response(404)
            self.end_headers()

        def _send_json(self, payload_obj: dict[str, Any]) -> None:
            payload = json.dumps(payload_obj, indent=2).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Prediction bot dashboard running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
