from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import requests

from .sports import google_news_headlines
from .storage import load_json, save_json

STOCK_PROXY_SYMBOLS = ["SPY", "QQQ", "TLT", "UUP", "GLD", "USO", "VIXY"]
NEWS_PROXY_SYMBOLS = ["SPY", "QQQ", "TLT", "GLD", "USO", "UUP", "BTCUSD", "ETHUSD"]
PUBLIC_PROXY_SYMBOLS = ["SPY", "QQQ", "TLT", "UUP", "GLD", "USO", "VIXY"]

POSITIVE_THEMES = {
    "disinflation": 0.18,
    "cooling inflation": 0.20,
    "rate cut": 0.16,
    "soft landing": 0.22,
    "stimulus": 0.14,
    "ceasefire": 0.16,
    "deal": 0.08,
    "liquidity": 0.12,
    "easing": 0.12,
    "beat": 0.08,
    "approval": 0.08,
    "inflows": 0.10,
    "rebound": 0.06,
}

NEGATIVE_THEMES = {
    "tariff": -0.18,
    "sanction": -0.14,
    "war": -0.20,
    "missile": -0.18,
    "strike": -0.12,
    "oil spike": -0.18,
    "hawkish": -0.16,
    "recession": -0.20,
    "downgrade": -0.10,
    "default": -0.18,
    "layoffs": -0.08,
    "guidance cut": -0.12,
    "crackdown": -0.14,
    "inflation hotter": -0.22,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _alpaca_headers() -> dict[str, str]:
    api_key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
    if not api_key or not secret_key:
        return {}
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_fresh(ts: str, max_age_minutes: int) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= timedelta(minutes=max_age_minutes)


def score_market_regime(changes: dict[str, float]) -> dict[str, Any]:
    spy = changes.get("SPY", 0.0)
    qqq = changes.get("QQQ", 0.0)
    tlt = changes.get("TLT", 0.0)
    uup = changes.get("UUP", 0.0)
    gld = changes.get("GLD", 0.0)
    uso = changes.get("USO", 0.0)
    vixy = changes.get("VIXY", 0.0)

    score = 0.0
    notes: list[str] = []

    if spy > 0.0035:
        score += 0.22
        notes.append("SPY strong")
    elif spy < -0.0035:
        score -= 0.22
        notes.append("SPY weak")

    if qqq > 0.0045:
        score += 0.26
        notes.append("QQQ strong")
    elif qqq < -0.0045:
        score -= 0.26
        notes.append("QQQ weak")

    if tlt > 0.004 and (spy < 0 or qqq < 0):
        score -= 0.10
        notes.append("bonds bid")
    elif tlt < -0.003 and spy > 0 and qqq > 0:
        score += 0.08
        notes.append("bonds offered")

    if uup > 0.0025:
        score -= 0.12
        notes.append("dollar firm")
    elif uup < -0.0025:
        score += 0.08
        notes.append("dollar softer")

    if gld > 0.006 and spy < 0:
        score -= 0.08
        notes.append("gold safety bid")

    if uso > 0.012:
        score -= 0.12
        notes.append("oil shock risk")

    if vixy > 0.015:
        score -= 0.18
        notes.append("volatility spike")
    elif vixy < -0.01 and spy > 0:
        score += 0.06
        notes.append("volatility easing")

    score = round(_clamp(score, -1.0, 1.0), 4)
    if score >= 0.2:
        mode = "risk_on"
    elif score <= -0.2:
        mode = "risk_off"
    else:
        mode = "neutral"

    return {"score": score, "mode": mode, "notes": notes}


def score_news_headlines(headlines: list[str]) -> dict[str, Any]:
    score = 0.0
    matches: list[str] = []
    for headline in headlines:
        lowered = headline.lower()
        for phrase, weight in POSITIVE_THEMES.items():
            if phrase in lowered:
                score += weight
                matches.append(f"+ {phrase}")
        for phrase, weight in NEGATIVE_THEMES.items():
            if phrase in lowered:
                score += weight
                matches.append(f"- {phrase}")
    normalized = round(_clamp(score / max(1.0, len(headlines) * 0.4), -1.0, 1.0), 4)
    if normalized >= 0.15:
        mode = "risk_on"
    elif normalized <= -0.15:
        mode = "risk_off"
    else:
        mode = "neutral"
    return {"score": normalized, "mode": mode, "matches": matches[:8]}


def _fetch_stock_proxy_changes() -> dict[str, float]:
    headers = _alpaca_headers()
    if not headers:
        return {}
    response = requests.get(
        "https://data.alpaca.markets/v2/stocks/snapshots",
        params={"symbols": ",".join(STOCK_PROXY_SYMBOLS)},
        headers=headers,
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    snapshots = payload.get("snapshots", {})
    changes: dict[str, float] = {}
    for symbol in STOCK_PROXY_SYMBOLS:
        snap = snapshots.get(symbol, {})
        daily = snap.get("dailyBar", {}) or {}
        prev = snap.get("prevDailyBar", {}) or snap.get("previousDailyBar", {}) or {}
        close = float(daily.get("c", 0.0) or 0.0)
        prev_close = float(prev.get("c", 0.0) or 0.0)
        if close and prev_close:
            changes[symbol] = (close - prev_close) / prev_close
    return changes


def _fetch_news_headlines(limit: int) -> list[str]:
    headers = _alpaca_headers()
    if not headers:
        return []
    response = requests.get(
        "https://data.alpaca.markets/v1beta1/news",
        params={
            "symbols": ",".join(NEWS_PROXY_SYMBOLS),
            "limit": str(limit),
            "sort": "desc",
        },
        headers=headers,
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("news", payload if isinstance(payload, list) else [])
    headlines: list[str] = []
    for item in items:
        headline = str(item.get("headline", "")).strip()
        if headline:
            headlines.append(headline)
    return headlines


def _fetch_stock_proxy_changes_public() -> dict[str, float]:
    changes: dict[str, float] = {}
    for symbol in PUBLIC_PROXY_SYMBOLS:
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "5d"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart", {}) or {}).get("result", [])
        if not result:
            continue
        closes = (((result[0].get("indicators", {}) or {}).get("quote", [{}])[0]).get("close", []) or [])
        closes = [float(value) for value in closes if value is not None]
        if len(closes) >= 2 and closes[-2]:
            changes[symbol] = (closes[-1] - closes[-2]) / closes[-2]
    return changes


def _fetch_news_headlines_public(limit: int) -> list[str]:
    queries = [
        "fed inflation rates jobs recession",
        "tariff sanctions war oil opec",
        "china stimulus liquidity markets earnings",
    ]
    headlines: list[str] = []
    per_query = max(2, limit // max(1, len(queries)))
    for query in queries:
        try:
            headlines.extend(google_news_headlines(query, limit=per_query))
        except (requests.RequestException, ET.ParseError):
            continue
    deduped: list[str] = []
    for headline in headlines:
        if headline not in deduped:
            deduped.append(headline)
    return deduped[:limit]


def load_macro_context(data_dir: Path, cfg: dict) -> dict[str, Any]:
    macro_cfg = cfg.get("analysis", {}).get("macro_overlay", {})
    if not macro_cfg.get("enabled", True):
        return {
            "ts": _iso_now(),
            "enabled": False,
            "market_score": 0.0,
            "news_score": 0.0,
            "combined_score": 0.0,
            "mode": "neutral",
            "notes": ["macro overlay disabled"],
            "proxy_changes": {},
            "headlines": [],
            "headline_summary": "",
        }

    cache_path = data_dir / "macro_snapshot.json"
    cache_minutes = int(macro_cfg.get("cache_minutes", 20) or 20)
    cached = load_json(cache_path, {})
    if cached and _is_fresh(str(cached.get("ts", "")), cache_minutes):
        return cached

    try:
        proxy_changes = _fetch_stock_proxy_changes()
    except requests.RequestException:
        proxy_changes = {}
    if not proxy_changes:
        try:
            proxy_changes = _fetch_stock_proxy_changes_public()
        except requests.RequestException:
            proxy_changes = {}

    try:
        headlines = _fetch_news_headlines(int(macro_cfg.get("news_limit", 18) or 18))
    except requests.RequestException:
        headlines = []
    if not headlines:
        headlines = _fetch_news_headlines_public(int(macro_cfg.get("news_limit", 18) or 18))

    market = score_market_regime(proxy_changes)
    news = score_news_headlines(headlines)
    combined_score = round(_clamp((market["score"] * 0.7) + (news["score"] * 0.3), -1.0, 1.0), 4)
    if combined_score >= 0.18:
        mode = "risk_on"
    elif combined_score <= -0.18:
        mode = "risk_off"
    else:
        mode = "neutral"

    payload = {
        "ts": _iso_now(),
        "enabled": True,
        "market_score": market["score"],
        "news_score": news["score"],
        "combined_score": combined_score,
        "mode": mode,
        "notes": market["notes"][:4] + news["matches"][:4],
        "proxy_changes": {key: round(value, 5) for key, value in proxy_changes.items()},
        "headlines": headlines[:8],
        "headline_summary": " | ".join(headlines[:4]),
    }
    save_json(cache_path, payload)
    return payload
