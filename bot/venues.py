from __future__ import annotations

import json
import os
import re
import statistics
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from .models import Fill, MarketSnapshot, OrderIntent, utc_now_iso
from .sports import build_sports_context

CRYPTO_REFERENCE_MAP = {
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
    "XRPUSD": "XRP-USD",
    "DOGEUSD": "DOGE-USD",
    "ADAUSD": "ADA-USD",
    "AVAXUSD": "AVAX-USD",
    "SUIUSD": "SUI-USD",
}
CRYPTO_TERMS = [
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "crypto",
    "token",
    "coin",
    "airdrop",
    "fdv",
    "market cap",
    "blockchain",
    "defi",
    "stablecoin",
    "nft",
    "altcoin",
    "dogecoin",
    "doge",
    "xrp",
    "ripple",
    "cardano",
    "ada",
    "polkadot",
    "dot",
    "chainlink",
    "link",
    "litecoin",
    "ltc",
    "avalanche",
    "avax",
    "sui",
    "etf",
    "staking",
    "restaking",
    "launch",
    "mainnet",
    "testnet",
    "unlock",
    "listing",
    "bridge",
    "rollup",
    "layer 2",
    "l2",
]
CRYPTO_KEYWORD_SYMBOLS = {
    "bitcoin": "BTCUSD",
    "btc": "BTCUSD",
    "ethereum": "ETHUSD",
    "eth": "ETHUSD",
    "solana": "SOLUSD",
    "sol": "SOLUSD",
    "xrp": "XRPUSD",
    "ripple": "XRPUSD",
    "doge": "DOGEUSD",
    "dogecoin": "DOGEUSD",
    "cardano": "ADAUSD",
    "ada": "ADAUSD",
    "avalanche": "AVAXUSD",
    "avax": "AVAXUSD",
    "sui": "SUIUSD",
}


class Venue:
    def load_markets(self, cfg: dict) -> list[MarketSnapshot]:
        raise NotImplementedError

    def execute(self, intent: OrderIntent, mode: str) -> Fill:
        raise NotImplementedError

    def fetch_settlement(self, position: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def hydrate_position(self, position: dict[str, Any]) -> dict[str, Any]:
        return position


class MockVenue(Venue):
    def load_markets(self, cfg: dict) -> list[MarketSnapshot]:
        snapshots = []
        for market in cfg["venue"]["markets"]:
            context = market.get("context", {})
            snapshots.append(
                MarketSnapshot(
                    market_id=market["id"],
                    market_type=market.get("market_type", "generic"),
                    question=market["question"],
                    yes_price=float(market["yes_price"]),
                    no_price=float(market["no_price"]),
                    reference_symbol=market.get("reference_symbol", "BTCUSD"),
                    reference_price=float(market.get("reference_price", 0.0)) or None,
                    change_5m_pct=float(market.get("change_5m_pct", 0.04)),
                    headline_summary=context.get("headline_summary", ""),
                    volume=float(market.get("volume", 0.0)) or None,
                    extra=context,
                )
            )
        return snapshots

    def execute(self, intent: OrderIntent, mode: str) -> Fill:
        status = "filled-paper" if mode == "paper" else "filled-live-mock"
        return Fill(
            market_id=intent.market_id,
            market_type="mock",
            side=intent.side,
            price=intent.price,
            size=intent.size,
            notional=round(intent.size * intent.price, 4),
            status=status,
            ts=utc_now_iso(),
        )

    def fetch_settlement(self, position: dict[str, Any]) -> dict[str, Any] | None:
        resolved_side = position.get("resolved_side")
        if not resolved_side:
            return None
        return {
            "market_id": position["market_id"],
            "question": position.get("question", position["market_id"]),
            "winning_side": resolved_side,
            "settled_at": utc_now_iso(),
            "market_slug": position.get("market_slug", ""),
        }


class PolymarketVenue(Venue):
    def __init__(self) -> None:
        self.base_url = os.getenv("POLYMARKET_API_URL", "https://gamma-api.polymarket.com")
        self._active_market_cache: dict[str, dict[str, Any]] | None = None

    def load_markets(self, cfg: dict) -> list[MarketSnapshot]:
        response = requests.get(
            f"{self.base_url}/markets",
            params={"active": "true", "closed": "false", "limit": 500},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            data = data.get("markets", [])
        allowed_types = set(cfg["venue"].get("allowed_market_types", []))
        allowed_keywords = [keyword.lower() for keyword in cfg["venue"].get("allowed_keywords", [])]
        max_markets = int(cfg["venue"].get("max_markets", 10))
        max_days_to_close = cfg["venue"].get("max_days_to_close")
        max_per_theme = int(cfg["venue"].get("max_markets_per_theme", 1))
        min_price = float(cfg["venue"].get("min_contract_price", 0.05))
        max_price = float(cfg["venue"].get("max_contract_price", 0.95))
        snapshots = []
        for market in data:
            outcomes = self._parse_list_field(market.get("outcomes", []))
            prices = self._parse_list_field(market.get("outcomePrices", []))
            if len(outcomes) < 2 or len(prices) < 2:
                continue
            try:
                yes_index = outcomes.index("Yes")
                no_index = outcomes.index("No")
            except ValueError:
                continue
            question = market.get("question") or market.get("title") or ""
            haystack = f"{market.get('slug', '')} {question}".lower()
            market_type = self._infer_market_type_from_text(haystack)
            if allowed_types and market_type not in allowed_types:
                continue
            if allowed_keywords and not any(self._matches_keyword(haystack, keyword) for keyword in allowed_keywords):
                continue
            end_date_iso = market.get("endDate") or market.get("end_date_iso")
            days_to_close = self._days_to_close(end_date_iso)
            if days_to_close is not None and days_to_close < 0:
                continue
            if max_days_to_close is not None and days_to_close is not None and days_to_close > float(max_days_to_close):
                continue
            yes_price = float(prices[yes_index])
            no_price = float(prices[no_index])
            if yes_price <= 0 or no_price <= 0:
                continue
            if not (min_price <= yes_price <= max_price and min_price <= no_price <= max_price):
                continue
            reference_symbol = self._infer_reference_symbol(question, cfg["venue"].get("reference_symbol", "BTCUSD"))
            reference_context = self._load_reference_context(reference_symbol)
            contract_context = (
                self._crypto_contract_context(question, reference_context)
                if market_type in {"crypto_price", "crypto_event"}
                else {}
            )
            theme = self._market_theme(question, market.get("slug", ""), market_type)
            volume_value = float(market.get("volume", 0.0) or 0.0)
            snapshots.append(
                MarketSnapshot(
                    market_id=str(market.get("conditionId") or market.get("id")),
                    market_type=market_type,
                    question=question,
                    yes_price=yes_price,
                    no_price=no_price,
                    reference_symbol=reference_symbol,
                    reference_price=reference_context.get("spot_price"),
                    change_5m_pct=reference_context.get("change_5m_pct"),
                    headline_summary=contract_context.get("headline_summary", ""),
                    volume=volume_value if market.get("volume") is not None else None,
                    extra={
                        "slug": market.get("slug", ""),
                        "active": market.get("active"),
                        "end_date_iso": end_date_iso,
                        "days_to_close": days_to_close,
                        "theme": theme,
                        "quality_score": self._quality_score(market_type, yes_price, no_price, volume_value, days_to_close),
                        "volume_score": volume_value,
                        **contract_context,
                    },
                )
            )
        snapshots.sort(
            key=lambda snap: (
                0 if snap.market_type == "crypto_price" else 1,
                -float(snap.extra.get("quality_score", 0.0)),
                snap.extra.get("days_to_close") is None,
                snap.extra.get("days_to_close", 999999),
            )
        )
        selected = []
        per_theme: dict[str, int] = {}
        for snapshot in snapshots:
            theme = str(snapshot.extra.get("theme", "")) or "unknown"
            if per_theme.get(theme, 0) >= max_per_theme:
                continue
            selected.append(snapshot)
            per_theme[theme] = per_theme.get(theme, 0) + 1
            if len(selected) >= max_markets:
                break
        return selected

    def execute(self, intent: OrderIntent, mode: str) -> Fill:
        if mode != "live":
            return Fill(
                market_id=intent.market_id,
                market_type="polymarket",
                side=intent.side,
                price=intent.price,
                size=intent.size,
                notional=round(intent.size * intent.price, 4),
                status="paper-polymarket",
                ts=utc_now_iso(),
            )
        raise NotImplementedError("Live Polymarket execution is intentionally not enabled in this scaffold.")

    def fetch_settlement(self, position: dict[str, Any]) -> dict[str, Any] | None:
        slug = position.get("market_slug")
        if not slug:
            return None
        response = requests.get(f"{self.base_url}/markets/slug/{slug}", timeout=15)
        response.raise_for_status()
        market = response.json()
        winning_side = self._infer_winning_side(market)
        if not winning_side:
            return None
        return {
            "market_id": position["market_id"],
            "question": market.get("question") or position.get("question", position["market_id"]),
            "winning_side": winning_side,
            "settled_at": market.get("closedTime") or market.get("endDate") or utc_now_iso(),
            "market_slug": slug,
        }

    def hydrate_position(self, position: dict[str, Any]) -> dict[str, Any]:
        if position.get("market_slug") and position.get("question"):
            return position
        market = self._find_active_market(position.get("market_id", ""))
        if not market:
            return position
        return {
            **position,
            "question": position.get("question") or market.get("question") or position.get("market_id", ""),
            "market_slug": position.get("market_slug") or market.get("slug", ""),
            "end_date_iso": position.get("end_date_iso") or market.get("endDate") or market.get("end_date_iso", ""),
        }

    @staticmethod
    def _parse_list_field(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return []

    @staticmethod
    def _matches_keyword(haystack: str, keyword: str) -> bool:
        if len(keyword) <= 3:
            return re.search(rf"\b{re.escape(keyword)}\b", haystack) is not None
        return keyword in haystack

    @staticmethod
    def _infer_winning_side(market: dict[str, Any]) -> str | None:
        outcomes = PolymarketVenue._parse_list_field(market.get("outcomes", []))
        prices = [float(value) for value in PolymarketVenue._parse_list_field(market.get("outcomePrices", []))]
        if len(outcomes) < 2 or len(prices) < 2:
            return None
        if not market.get("closed"):
            return None
        for outcome, price in zip(outcomes, prices):
            if price >= 0.999:
                return outcome.upper()
        return None

    def _find_active_market(self, market_id: str) -> dict[str, Any] | None:
        if not market_id:
            return None
        if self._active_market_cache is None:
            response = requests.get(
                f"{self.base_url}/markets",
                params={"active": "true", "closed": "false", "limit": 500},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            self._active_market_cache = {
                str(m.get("conditionId") or m.get("id")): m
                for m in data
            }
        return self._active_market_cache.get(market_id)

    @staticmethod
    def _infer_market_type_from_text(text: str) -> str:
        if any(
            PolymarketVenue._matches_keyword(text, token)
            for token in [
                "bitcoin",
                "btc",
                "ethereum",
                "eth",
                "solana",
                "sol",
                "xrp",
                "ripple",
                "doge",
                "dogecoin",
                "cardano",
                "ada",
                "avalanche",
                "avax",
                "sui",
            ]
        ):
            if any(token in text for token in ["hit $", "above $", "below $", "between $", "range", "price"]):
                return "crypto_price"
        if any(PolymarketVenue._matches_keyword(text, token) for token in CRYPTO_TERMS):
            return "crypto_event"
        return KalshiVenue._infer_market_type(text)

    @staticmethod
    def _infer_reference_symbol(question: str, default_symbol: str = "BTCUSD") -> str:
        lowered = question.lower()
        for keyword, symbol in CRYPTO_KEYWORD_SYMBOLS.items():
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                return symbol
        return default_symbol

    @staticmethod
    def _load_reference_context(reference_symbol: str) -> dict[str, float | str]:
        product = CRYPTO_REFERENCE_MAP.get(reference_symbol)
        if not product:
            return {}
        spot_response = requests.get(
            f"https://api.coinbase.com/v2/prices/{product}/spot",
            timeout=10,
        )
        spot_response.raise_for_status()
        spot_price = float(spot_response.json()["data"]["amount"])

        candle_response = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 300, "limit": 60},
            timeout=10,
        )
        candle_response.raise_for_status()
        candles = candle_response.json()
        change_5m_pct = 0.0
        recent_returns = []
        ordered = list(reversed(candles))
        closes = [float(candle[4]) for candle in ordered]
        if len(closes) >= 2:
            latest_close = closes[-1]
            prior_close = closes[-2]
            if prior_close:
                change_5m_pct = (latest_close - prior_close) / prior_close
            for idx in range(1, len(ordered[-12:])):
                prev_close = float(ordered[-12:][idx - 1][4])
                current_close = float(ordered[-12:][idx][4])
                if prev_close:
                    recent_returns.append((current_close - prev_close) / prev_close)

        hour_response = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 3600, "limit": 48},
            timeout=10,
        )
        hour_response.raise_for_status()
        hour_candles = hour_response.json()
        hour_ordered = list(reversed(hour_candles))
        hour_closes = [float(candle[4]) for candle in hour_ordered]
        change_1h_pct = 0.0
        change_4h_pct = 0.0
        if len(hour_closes) >= 2:
            latest_hour_close = hour_closes[-1]
            prior_hour_close = hour_closes[-2]
            if prior_hour_close:
                change_1h_pct = (latest_hour_close - prior_hour_close) / prior_hour_close
        if len(hour_closes) >= 5:
            prior_4h_close = hour_closes[-5]
            latest_hour_close = hour_closes[-1]
            if prior_4h_close:
                change_4h_pct = (latest_hour_close - prior_4h_close) / prior_4h_close

        quarter_response = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": 900, "limit": 40},
            timeout=10,
        )
        quarter_response.raise_for_status()
        quarter_candles = quarter_response.json()
        quarter_ordered = list(reversed(quarter_candles))
        quarter_closes = [float(candle[4]) for candle in quarter_ordered]
        change_15m_pct = 0.0
        if len(quarter_closes) >= 2:
            latest_15m_close = quarter_closes[-1]
            prior_15m_close = quarter_closes[-2]
            if prior_15m_close:
                change_15m_pct = (latest_15m_close - prior_15m_close) / prior_15m_close

        realized_vol_1h = statistics.pstdev(recent_returns) if len(recent_returns) >= 2 else 0.0
        ema_fast = PolymarketVenue._ema(closes, 9)
        ema_slow = PolymarketVenue._ema(closes, 21)
        ema_15m_fast = PolymarketVenue._ema(quarter_closes, 8)
        ema_15m_slow = PolymarketVenue._ema(quarter_closes, 21)
        ema_1h_fast = PolymarketVenue._ema(hour_closes, 4)
        ema_1h_slow = PolymarketVenue._ema(hour_closes, 9)
        rsi_14 = PolymarketVenue._rsi(closes, 14)
        atr_14 = PolymarketVenue._atr(ordered, 14)
        atr_pct = (atr_14 / spot_price) if spot_price else 0.0
        candle_bias = PolymarketVenue._candle_bias(ordered)
        breakout_pct = PolymarketVenue._breakout_pct(ordered, 20)
        setup_score = PolymarketVenue._setup_score(
            {
                "change_5m_pct": change_5m_pct,
                "change_15m_pct": change_15m_pct,
                "change_1h_pct": change_1h_pct,
                "change_4h_pct": change_4h_pct,
                "ema_fast_9": ema_fast,
                "ema_slow_21": ema_slow,
                "ema_15m_fast": ema_15m_fast,
                "ema_15m_slow": ema_15m_slow,
                "ema_1h_fast": ema_1h_fast,
                "ema_1h_slow": ema_1h_slow,
                "rsi_14": rsi_14,
                "atr_pct": atr_pct,
                "candle_bias": candle_bias,
                "breakout_pct": breakout_pct,
            }
        )
        return {
            "product": product,
            "spot_price": spot_price,
            "change_5m_pct": change_5m_pct,
            "change_15m_pct": change_15m_pct,
            "change_1h_pct": change_1h_pct,
            "change_4h_pct": change_4h_pct,
            "realized_vol_1h": realized_vol_1h,
            "ema_fast_9": ema_fast,
            "ema_slow_21": ema_slow,
            "ema_spread_pct": ((ema_fast - ema_slow) / ema_slow) if ema_slow else 0.0,
            "ema_15m_fast": ema_15m_fast,
            "ema_15m_slow": ema_15m_slow,
            "ema_15m_spread_pct": ((ema_15m_fast - ema_15m_slow) / ema_15m_slow) if ema_15m_slow else 0.0,
            "ema_1h_fast": ema_1h_fast,
            "ema_1h_slow": ema_1h_slow,
            "ema_1h_spread_pct": ((ema_1h_fast - ema_1h_slow) / ema_1h_slow) if ema_1h_slow else 0.0,
            "rsi_14": rsi_14,
            "atr_14": atr_14,
            "atr_pct": atr_pct,
            "candle_bias": candle_bias,
            "breakout_pct": breakout_pct,
            "setup_score": setup_score,
        }

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        alpha = 2 / (period + 1)
        ema = values[0]
        for value in values[1:]:
            ema = (value * alpha) + (ema * (1 - alpha))
        return round(ema, 6)

    @staticmethod
    def _rsi(values: list[float], period: int = 14) -> float:
        if len(values) < period + 1:
            return 50.0
        gains = []
        losses = []
        for idx in range(1, len(values[-(period + 1):])):
            change = values[-(period + 1):][idx] - values[-(period + 1):][idx - 1]
            gains.append(max(change, 0.0))
            losses.append(abs(min(change, 0.0)))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 4)

    @staticmethod
    def _atr(candles: list[list[float]], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        recent = candles[-(period + 1):]
        true_ranges = []
        for idx in range(1, len(recent)):
            high = float(recent[idx][2])
            low = float(recent[idx][1])
            prev_close = float(recent[idx - 1][4])
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return round(sum(true_ranges) / len(true_ranges), 6) if true_ranges else 0.0

    @staticmethod
    def _candle_bias(candles: list[list[float]]) -> float:
        if len(candles) < 2:
            return 0.0
        last = candles[-1]
        prev = candles[-2]
        last_open = float(last[3])
        last_high = float(last[2])
        last_low = float(last[1])
        last_close = float(last[4])
        prev_open = float(prev[3])
        prev_close = float(prev[4])
        candle_range = max(last_high - last_low, 1e-9)
        body = (last_close - last_open) / candle_range
        bullish_engulfing = last_close > last_open and prev_close < prev_open and last_close >= prev_open and last_open <= prev_close
        bearish_engulfing = last_close < last_open and prev_close > prev_open and last_open >= prev_close and last_close <= prev_open
        bias = body
        if bullish_engulfing:
            bias += 0.35
        if bearish_engulfing:
            bias -= 0.35
        return round(max(-1.0, min(1.0, bias)), 4)

    @staticmethod
    def _breakout_pct(candles: list[list[float]], lookback: int = 20) -> float:
        if len(candles) < lookback + 1:
            return 0.0
        recent = candles[-(lookback + 1):]
        latest_close = float(recent[-1][4])
        prior_high = max(float(candle[2]) for candle in recent[:-1])
        if prior_high <= 0:
            return 0.0
        return round((latest_close - prior_high) / prior_high, 6)

    @staticmethod
    def _setup_score(values: dict[str, float]) -> float:
        score = 0.0
        score += 0.16 if values.get("ema_fast_9", 0.0) > values.get("ema_slow_21", 0.0) else -0.10
        score += 0.22 if values.get("ema_15m_fast", 0.0) > values.get("ema_15m_slow", 0.0) else -0.12
        score += 0.28 if values.get("ema_1h_fast", 0.0) > values.get("ema_1h_slow", 0.0) else -0.18
        score += 0.10 if values.get("change_15m_pct", 0.0) > 0 else -0.06
        score += 0.14 if values.get("change_1h_pct", 0.0) > 0 else -0.10
        score += 0.10 if values.get("change_4h_pct", 0.0) > 0 else -0.08
        breakout_pct = values.get("breakout_pct", 0.0)
        if breakout_pct > 0:
            score += min(0.18, breakout_pct * 12)
        elif breakout_pct < -0.01:
            score -= 0.10
        rsi = values.get("rsi_14", 50.0)
        if 48 <= rsi <= 66:
            score += 0.10
        elif rsi > 74 or rsi < 34:
            score -= 0.10
        atr_pct = values.get("atr_pct", 0.0)
        if 0.004 <= atr_pct <= 0.025:
            score += 0.06
        elif atr_pct > 0.04:
            score -= 0.08
        score += max(-0.14, min(0.14, values.get("candle_bias", 0.0) * 0.25))
        return round(max(-1.0, min(1.0, score)), 4)

    @staticmethod
    def _days_to_close(end_date_iso: str | None) -> float | None:
        if not end_date_iso:
            return None
        normalized = end_date_iso.replace("Z", "+00:00")
        try:
            end_dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta = end_dt - datetime.now(timezone.utc)
        return round(delta.total_seconds() / 86400, 3)

    @staticmethod
    def _market_theme(question: str, slug: str, market_type: str) -> str:
        lowered = f"{question} {slug}".lower()
        theme_tokens = {
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
            "ethereum": "ethereum",
            "eth": "ethereum",
            "solana": "solana",
            "sol": "solana",
            "megaeth": "megaeth",
            "xrp": "xrp",
            "ripple": "xrp",
            "doge": "doge",
            "dogecoin": "doge",
            "cardano": "cardano",
            "ada": "cardano",
            "avalanche": "avax",
            "avax": "avax",
            "sui": "sui",
            "etf": "etf",
            "staking": "staking",
            "restaking": "staking",
        }
        for token, theme in theme_tokens.items():
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                return f"{market_type}:{theme}"
        words = re.findall(r"[a-z0-9]+", lowered)
        keep = [word for word in words if len(word) > 3][:3]
        stem = "-".join(keep) if keep else market_type
        return f"{market_type}:{stem}"

    @staticmethod
    def _quality_score(
        market_type: str,
        yes_price: float,
        no_price: float,
        volume: float,
        days_to_close: float | None,
    ) -> float:
        score = 0.0
        score += 1.5 if market_type == "crypto_price" else 0.8
        score += min(volume / 5000.0, 2.0)
        price_balance = 1.0 - abs(yes_price - no_price)
        score += max(price_balance, 0.0)
        if days_to_close is not None:
            if days_to_close <= 7:
                score += 1.0
            elif days_to_close <= 21:
                score += 0.5
            elif days_to_close > 60:
                score -= 1.0
        return round(score, 4)

    @staticmethod
    def _crypto_contract_context(question: str, reference_context: dict[str, Any]) -> dict[str, Any]:
        lowered = question.lower()
        context: dict[str, Any] = {}
        spot_price = reference_context.get("spot_price")

        explicit_dollar_values = [
            float(match.replace(",", ""))
            for match in re.findall(r"\$([0-9][0-9,]*(?:\.\d+)?)", question)
        ]
        if explicit_dollar_values:
            context["price_targets"] = explicit_dollar_values[:4]
            if len(explicit_dollar_values) == 1:
                target = explicit_dollar_values[0]
                context["target_price"] = target
                if spot_price:
                    context["distance_to_target"] = round(target - float(spot_price), 2)

        if any(token in lowered for token in ["between", "range", "from $"]):
            context["contract_style"] = "range"
            if len(explicit_dollar_values) >= 2:
                lower_bound, upper_bound = sorted(explicit_dollar_values[:2])
                context["lower_bound"] = lower_bound
                context["upper_bound"] = upper_bound
                if spot_price:
                    context["distance_to_lower"] = round(float(spot_price) - lower_bound, 2)
                    context["distance_to_upper"] = round(upper_bound - float(spot_price), 2)
                    context["spot_inside_range"] = lower_bound <= float(spot_price) <= upper_bound
        elif any(token in lowered for token in ["above", "over", "at least", "higher than"]):
            context["contract_style"] = "above"
        elif any(token in lowered for token in ["below", "under", "at most", "lower than"]):
            context["contract_style"] = "below"
        else:
            context["contract_style"] = "event"

        if spot_price:
            direction = "up" if reference_context.get("change_5m_pct", 0.0) > 0 else "down"
            context["headline_summary"] = (
                f"{reference_context.get('product', reference_context)} spot {spot_price:.2f}; "
                f"5m drift {reference_context.get('change_5m_pct', 0.0):+.2%} ({direction})."
            )
        elif context.get("price_targets"):
            context["headline_summary"] = "Crypto market contract with explicit target levels but no direct spot mapping."
        return context


class AlpacaVenue(Venue):
    SUPPORTED_SYMBOLS = {
        "BTCUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
        "SOLUSD": "SOL-USD",
        "XRPUSD": "XRP-USD",
        "DOGEUSD": "DOGE-USD",
        "ADAUSD": "ADA-USD",
        "AVAXUSD": "AVAX-USD",
        "SUIUSD": "SUI-USD",
    }

    def __init__(self) -> None:
        self.api_key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
        self.secret_key = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
        self._discovery_cache: dict[str, dict[str, Any]] = {}

    def load_markets(self, cfg: dict) -> list[MarketSnapshot]:
        symbols = cfg["venue"].get("spot_symbols") or self._discover_symbols(cfg)
        max_markets = int(cfg["venue"].get("max_markets", len(symbols) or 1))
        horizon_hours = int(cfg["venue"].get("spot_horizon_hours", 4))
        snapshots = []
        for reference_symbol in symbols:
            product = self.SUPPORTED_SYMBOLS.get(reference_symbol)
            if not product:
                continue
            context = PolymarketVenue._load_reference_context(reference_symbol)
            discovery = self._discover_market_metadata(reference_symbol)
            if not context:
                continue
            momentum_score = self._momentum_score(context, discovery)
            snapshots.append(
                MarketSnapshot(
                    market_id=product.replace("-", "/"),
                    market_type="crypto_spot",
                    question=f"{product} candlestick setup ({horizon_hours}h)",
                    yes_price=0.5,
                    no_price=0.5,
                    reference_symbol=reference_symbol,
                    reference_price=float(context.get("spot_price", 0.0) or 0.0),
                    change_5m_pct=float(context.get("change_5m_pct", 0.0) or 0.0),
                    headline_summary=self._spot_headline(product, context, discovery),
                    volume=float(discovery.get("total_volume", 0.0) or 0.0),
                    extra={
                        "spot_price": float(context.get("spot_price", 0.0) or 0.0),
                        "product": product,
                        "change_1h_pct": float(context.get("change_1h_pct", 0.0) or 0.0),
                        "change_15m_pct": float(context.get("change_15m_pct", 0.0) or 0.0),
                        "change_4h_pct": float(context.get("change_4h_pct", 0.0) or 0.0),
                        "realized_vol_1h": float(context.get("realized_vol_1h", 0.0) or 0.0),
                        "ema_fast_9": float(context.get("ema_fast_9", 0.0) or 0.0),
                        "ema_slow_21": float(context.get("ema_slow_21", 0.0) or 0.0),
                        "ema_spread_pct": float(context.get("ema_spread_pct", 0.0) or 0.0),
                        "ema_15m_spread_pct": float(context.get("ema_15m_spread_pct", 0.0) or 0.0),
                        "ema_1h_spread_pct": float(context.get("ema_1h_spread_pct", 0.0) or 0.0),
                        "rsi_14": float(context.get("rsi_14", 50.0) or 50.0),
                        "atr_pct": float(context.get("atr_pct", 0.0) or 0.0),
                        "candle_bias": float(context.get("candle_bias", 0.0) or 0.0),
                        "breakout_pct": float(context.get("breakout_pct", 0.0) or 0.0),
                        "setup_score": float(context.get("setup_score", 0.0) or 0.0),
                        "price_change_24h_pct": discovery.get("price_change_percentage_24h"),
                        "market_cap_rank": discovery.get("market_cap_rank"),
                        "momentum_score": momentum_score,
                        "discovery_source": "coingecko",
                        "horizon_hours": horizon_hours,
                    },
                )
            )
        snapshots.sort(
            key=lambda snap: (
                -(snap.volume or 0.0),
                -abs(float(snap.extra.get("price_change_24h_pct") or 0.0)),
            )
        )
        return snapshots[:max_markets]

    def execute(self, intent: OrderIntent, mode: str) -> Fill:
        if mode != "live":
            if self.api_key and self.secret_key:
                status = self._submit_order(intent, paper=True)
            else:
                status = "paper-alpaca-sim"
            return Fill(
                market_id=intent.market_id,
                market_type="alpaca",
                side=intent.side,
                price=intent.price,
                size=intent.size,
                notional=round(intent.size * intent.price, 4),
                status=status,
                ts=utc_now_iso(),
            )
        status = self._submit_order(intent, paper=False)
        return Fill(
            market_id=intent.market_id,
            market_type="alpaca",
            side=intent.side,
            price=intent.price,
            size=intent.size,
            notional=round(intent.size * intent.price, 4),
            status=status,
            ts=utc_now_iso(),
        )

    def _discover_symbols(self, cfg: dict) -> list[str]:
        symbols = []
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "volume_desc",
                    "per_page": int(cfg["venue"].get("discovery_limit", 25)),
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
                timeout=15,
            )
            response.raise_for_status()
            for coin in response.json():
                reference_symbol = f"{str(coin.get('symbol', '')).upper()}USD"
                if reference_symbol in self.SUPPORTED_SYMBOLS and reference_symbol not in symbols:
                    symbols.append(reference_symbol)
        except requests.RequestException:
            pass
        configured = cfg["venue"].get("spot_symbols", [])
        for symbol in configured:
            if symbol not in symbols and symbol in self.SUPPORTED_SYMBOLS:
                symbols.insert(0, symbol)
        return symbols

    def _discover_market_metadata(self, reference_symbol: str) -> dict[str, Any]:
        if reference_symbol in self._discovery_cache:
            return self._discovery_cache[reference_symbol]
        base = reference_symbol[:-3].lower()
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "symbols": base,
                    "order": "market_cap_desc",
                    "per_page": 1,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
                timeout=15,
            )
            response.raise_for_status()
            rows = response.json()
            metadata = rows[0] if rows else {}
        except requests.RequestException:
            metadata = {}
        self._discovery_cache[reference_symbol] = metadata
        return metadata

    @staticmethod
    def _spot_headline(product: str, context: dict[str, Any], discovery: dict[str, Any]) -> str:
        spot = float(context.get("spot_price", 0.0) or 0.0)
        drift_5m = float(context.get("change_5m_pct", 0.0) or 0.0)
        drift_1h = float(context.get("change_1h_pct", 0.0) or 0.0)
        vol_1h = float(context.get("realized_vol_1h", 0.0) or 0.0)
        drift_24h = float(discovery.get("price_change_percentage_24h", 0.0) or 0.0) / 100.0
        rank = discovery.get("market_cap_rank")
        pieces = [
            f"{product} spot {spot:.4f}" if spot else f"{product} spot unavailable",
            f"5m drift {drift_5m:+.2%}",
            f"1h drift {drift_1h:+.2%}",
            f"1h realized vol {vol_1h:.2%}",
            f"24h drift {drift_24h:+.2%}",
        ]
        ema_spread = float(context.get("ema_spread_pct", 0.0) or 0.0)
        rsi = float(context.get("rsi_14", 50.0) or 50.0)
        atr_pct = float(context.get("atr_pct", 0.0) or 0.0)
        candle_bias = float(context.get("candle_bias", 0.0) or 0.0)
        pieces.extend(
            [
                f"EMA spread {ema_spread:+.2%}",
                f"RSI14 {rsi:.1f}",
                f"ATR14 {atr_pct:.2%}",
                f"candle bias {candle_bias:+.2f}",
            ]
        )
        if rank:
            pieces.append(f"market-cap rank #{rank}")
        return "; ".join(pieces) + "."

    @staticmethod
    def _momentum_score(context: dict[str, Any], discovery: dict[str, Any]) -> float:
        drift_5m = float(context.get("change_5m_pct", 0.0) or 0.0)
        drift_1h = float(context.get("change_1h_pct", 0.0) or 0.0)
        drift_24h = float(discovery.get("price_change_percentage_24h", 0.0) or 0.0) / 100.0
        vol_1h = float(context.get("realized_vol_1h", 0.0) or 0.0)

        raw = (drift_5m * 2.0) + (drift_1h * 3.0) + drift_24h - (vol_1h * 1.5)
        normalized = max(-1.0, min(1.0, raw / 0.08))
        return round(normalized, 4)

    def _submit_order(self, intent: OrderIntent, paper: bool) -> str:
        if not self.api_key or not self.secret_key:
            raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for Alpaca execution.")
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }
        payload = {
            "symbol": intent.market_id.replace("/", ""),
            "side": "buy" if intent.side == "BUY" else "sell",
            "type": "market",
            "time_in_force": "gtc",
            "qty": f"{intent.size:.8f}",
        }
        response = requests.post(f"{base_url}/v2/orders", headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        order = response.json()
        return f"{'paper' if paper else 'live'}-alpaca:{order.get('status', 'submitted')}"


class KalshiVenue(Venue):
    def __init__(self) -> None:
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID")
        self.private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

    def _host(self, cfg: dict) -> str:
        default_host = "https://demo-api.kalshi.co/trade-api/v2" if cfg["venue"].get("kalshi_demo", True) else "https://api.elections.kalshi.com/trade-api/v2"
        return os.getenv("KALSHI_API_URL", default_host)

    def load_markets(self, cfg: dict) -> list[MarketSnapshot]:
        btc_context = self._load_btc_context() if cfg["venue"].get("reference_symbol", "BTCUSD") == "BTCUSD" else {}
        event_ticker = cfg["venue"].get("kalshi_event_ticker")
        if not event_ticker:
            event_ticker = None
        if event_ticker:
            response = requests.get(f"{self._host(cfg)}/events/{event_ticker}", timeout=15)
            response.raise_for_status()
            events = [response.json().get("event", {})]
        else:
            response = requests.get(f"{self._host(cfg)}/markets", params={"limit": 200, "status": "open"}, timeout=15)
            response.raise_for_status()
            events = [{"markets": response.json().get("markets", [])}]
        snapshots = []
        allowed_types = set(cfg["venue"].get("allowed_market_types", []))
        allowed_keywords = [keyword.lower() for keyword in cfg["venue"].get("allowed_keywords", [])]
        max_days_to_close = cfg["venue"].get("max_days_to_close")
        for event in events:
            for market in event.get("markets", []):
                yes_price = self._best_price(market, "yes")
                no_price = self._best_price(market, "no")
                if yes_price is None or no_price is None or yes_price <= 0 or no_price <= 0:
                    continue
                title = market.get("title") or market.get("subtitle") or market["ticker"]
                market_type = self._infer_market_type(title)
                close_time = market.get("close_time")
                days_to_close = self._days_to_close(close_time)
                haystack = f"{market.get('ticker', '')} {title}".lower()
                if allowed_types and market_type not in allowed_types:
                    continue
                if allowed_keywords and not any(keyword in haystack for keyword in allowed_keywords):
                    continue
                if max_days_to_close is not None and days_to_close is not None and days_to_close > float(max_days_to_close):
                    continue
                yes_price, no_price = self._normalize_prices(yes_price, no_price)
                use_btc_context = btc_context if market_type == "crypto_price" else {}
                contract_context = self._contract_context(market["ticker"], title, use_btc_context)
                sports_context = build_sports_context(title) if market_type == "sports" else {}
                snapshots.append(
                    MarketSnapshot(
                        market_id=market["ticker"],
                        market_type=market_type,
                        question=title,
                        yes_price=yes_price,
                        no_price=no_price,
                        reference_symbol=cfg["venue"].get("reference_symbol", "BTCUSD"),
                        reference_price=use_btc_context.get("spot_price"),
                        change_5m_pct=use_btc_context.get("change_5m_pct"),
                        headline_summary=sports_context.get("headline_summary", ""),
                        volume=float(market.get("volume", 0.0)) if market.get("volume") is not None else None,
                        extra={
                            "status": market.get("status"),
                            "close_time": close_time,
                            "days_to_close": days_to_close,
                            **contract_context,
                            **sports_context,
                        },
                    )
                )
        snapshots.sort(key=lambda snap: (snap.extra.get("days_to_close") is None, snap.extra.get("days_to_close", 999999)))
        max_markets = int(cfg["venue"].get("max_markets", len(snapshots) or 1))
        return snapshots[:max_markets]

    def execute(self, intent: OrderIntent, mode: str) -> Fill:
        if mode != "live":
            return Fill(
                market_id=intent.market_id,
                market_type="kalshi",
                side=intent.side,
                price=intent.price,
                size=intent.size,
                notional=round(intent.size * intent.price, 4),
                status="paper-kalshi",
                ts=utc_now_iso(),
            )
        return self._place_live_order(intent)

    @staticmethod
    def _best_price(market: dict[str, Any], side: str) -> float | None:
        dollars_key = f"{side}_ask_dollars"
        cents_key = f"{side}_ask"
        if market.get(dollars_key) is not None:
            return float(market[dollars_key])
        if market.get(cents_key) is not None:
            return float(market[cents_key]) / 100
        return None

    @staticmethod
    def _infer_market_type(title: str) -> str:
        lowered = title.lower()
        if "fed" in lowered or "fomc" in lowered or "rate" in lowered:
            return "fed_rates"
        if any(token in lowered for token in ["election", "president", "senate", "governor", "house"]):
            return "election"
        if any(token in lowered for token in ["game", "winner", "nba", "nfl", "nhl", "mlb", "soccer", "basketball", "baseball", "football", "tennis"]):
            return "sports"
        if any(token in lowered for token in ["btc", "bitcoin", "eth", "ethereum", "nasdaq", "s&p", "spx"]):
            return "crypto_price"
        return "generic"

    @staticmethod
    def _normalize_prices(yes_price: float, no_price: float) -> tuple[float, float]:
        total = yes_price + no_price
        if total <= 0:
            return yes_price, no_price
        if total > 1.05:
            return yes_price / total, no_price / total
        return yes_price, no_price

    @staticmethod
    def _load_btc_context() -> dict[str, float]:
        response = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=10,
        )
        response.raise_for_status()
        spot_price = float(response.json()["data"]["amount"])

        candle_response = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles",
            params={"granularity": 300, "limit": 2},
            timeout=10,
        )
        candle_response.raise_for_status()
        candles = candle_response.json()
        change_5m_pct = 0.0
        if len(candles) >= 2:
            latest_close = float(candles[0][4])
            prior_close = float(candles[1][4])
            if prior_close:
                change_5m_pct = (latest_close - prior_close) / prior_close

        return {
            "spot_price": spot_price,
            "change_5m_pct": change_5m_pct,
        }

    @staticmethod
    def _contract_context(ticker: str, title: str, btc_context: dict[str, float]) -> dict[str, Any]:
        context: dict[str, Any] = {}
        lower_match = re.search(r"-B(\d+(?:\.\d+)?)", ticker)
        upper_match = re.search(r"-T(\d+(?:\.\d+)?)", ticker)

        if lower_match:
            context["lower_bound"] = float(lower_match.group(1))
        if upper_match:
            context["upper_bound"] = float(upper_match.group(1))

        if "price range" in title.lower():
            context["contract_style"] = "range"
        elif "above" in title.lower() or "over" in title.lower():
            context["contract_style"] = "above"
        elif "below" in title.lower() or "under" in title.lower():
            context["contract_style"] = "below"

        spot_price = btc_context.get("spot_price")
        if spot_price:
            if "lower_bound" in context:
                context["distance_to_lower"] = round(spot_price - context["lower_bound"], 2)
            if "upper_bound" in context:
                context["distance_to_upper"] = round(context["upper_bound"] - spot_price, 2)
            if "lower_bound" in context and "upper_bound" in context:
                inside = context["lower_bound"] <= spot_price <= context["upper_bound"]
                context["spot_inside_range"] = inside

        return context

    @staticmethod
    def _days_to_close(close_time: str | None) -> float | None:
        if not close_time:
            return None
        normalized = close_time.replace("Z", "+00:00")
        close_dt = datetime.fromisoformat(normalized)
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        delta = close_dt - datetime.now(timezone.utc)
        return round(delta.total_seconds() / 86400, 3)

    def _place_live_order(self, intent: OrderIntent) -> Fill:
        if not self.api_key_id or not self.private_key_path:
            raise ValueError("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for live Kalshi execution.")

        try:
            from kalshi_python_sync.auth import KalshiAuth
        except Exception as exc:
            raise RuntimeError("kalshi_python_sync is required for live Kalshi execution.") from exc

        with open(self.private_key_path, "r", encoding="utf-8") as handle:
            private_key = handle.read()

        count = max(1, int(Decimal(str(intent.size)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        price = Decimal(str(intent.price)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        order_path = "/portfolio/orders"
        payload = {
            "ticker": intent.market_id,
            "side": intent.side.lower(),
            "action": "buy",
            "count": count,
            "type": "market",
        }
        if intent.side == "YES":
            payload["yes_price_dollars"] = f"{price:.4f}"
        else:
            payload["no_price_dollars"] = f"{price:.4f}"

        auth = KalshiAuth(self.api_key_id, private_key)
        headers = auth.create_auth_headers("POST", order_path)
        sign_path = urlparse(
            f"{os.getenv('KALSHI_API_URL', 'https://api.elections.kalshi.com/trade-api/v2')}{order_path}"
        ).path
        headers = auth.create_auth_headers("POST", sign_path)
        headers["Content-Type"] = "application/json"
        response = requests.post(
            f"{os.getenv('KALSHI_API_URL', 'https://api.elections.kalshi.com/trade-api/v2')}{order_path}",
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        order = response.json().get("order", {})
        fill_price = float(order.get("yes_price_dollars") or order.get("no_price_dollars") or price)
        filled_count = float(order.get("fill_count_fp") or order.get("initial_count") or count)
        return Fill(
            market_id=intent.market_id,
            market_type="kalshi",
            side=intent.side,
            price=fill_price,
            size=filled_count,
            notional=round(fill_price * filled_count, 4),
            status=f"live-kalshi:{order.get('status', 'submitted')}",
            ts=utc_now_iso(),
        )


def build_venue(cfg: dict) -> Venue:
    venue_name = cfg["venue"]["name"]
    if venue_name == "mock":
        return MockVenue()
    if venue_name == "polymarket":
        return PolymarketVenue()
    if venue_name == "alpaca":
        return AlpacaVenue()
    if venue_name == "kalshi":
        return KalshiVenue()
    raise ValueError(f"Unsupported venue: {venue_name}")
