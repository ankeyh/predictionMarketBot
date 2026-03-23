from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests


def check_kalshi(base_url: str, require_auth: bool = False) -> dict:
    payload: dict[str, object] = {
        "base_url": base_url,
        "market_data_ok": False,
        "auth_ok": False,
    }

    markets_response = requests.get(
        f"{base_url}/markets",
        params={"limit": 5, "status": "open"},
        timeout=15,
    )
    markets_response.raise_for_status()
    markets = markets_response.json().get("markets", [])
    payload["market_data_ok"] = True
    payload["market_count"] = len(markets)
    payload["sample_tickers"] = [market.get("ticker") for market in markets[:3]]

    if not require_auth:
        return payload

    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise ValueError("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH before running --auth.")

    try:
        from kalshi_python_sync.auth import KalshiAuth
    except Exception as exc:
        raise RuntimeError("kalshi_python_sync is required for authenticated Kalshi checks.") from exc

    private_key = Path(key_path).read_text(encoding="utf-8")
    auth = KalshiAuth(key_id, private_key)
    path = "/api_keys"
    sign_path = urlparse(f"{base_url}{path}").path
    headers = auth.create_auth_headers("GET", sign_path)
    auth_response = requests.get(f"{base_url}{path}", headers=headers, timeout=15)
    auth_response.raise_for_status()
    api_keys = auth_response.json().get("api_keys", [])
    payload["auth_ok"] = True
    payload["api_key_count"] = len(api_keys)
    payload["api_key_ids"] = [item.get("api_key_id") for item in api_keys[:3]]
    return payload


def format_check(payload: dict) -> str:
    return json.dumps(payload, indent=2)
