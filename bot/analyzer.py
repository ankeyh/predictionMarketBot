from __future__ import annotations

import json
import os
import re
from pathlib import Path

from anthropic import Anthropic

from .models import AnalysisResult, MarketSnapshot


class Analyzer:
    def analyze(self, snapshot: MarketSnapshot) -> AnalysisResult:
        raise NotImplementedError


class CandleAnalyzer(Analyzer):
    def analyze(self, snapshot: MarketSnapshot) -> AnalysisResult:
        if snapshot.market_type != "crypto_spot":
            return MockAnalyzer().analyze(snapshot)

        extra = snapshot.extra
        setup_score = float(extra.get("setup_score", 0.0) or 0.0)
        ema_spread = float(extra.get("ema_spread_pct", 0.0) or 0.0)
        ema_15m_spread = float(extra.get("ema_15m_spread_pct", 0.0) or 0.0)
        ema_1h_spread = float(extra.get("ema_1h_spread_pct", 0.0) or 0.0)
        rsi = float(extra.get("rsi_14", 50.0) or 50.0)
        atr_pct = float(extra.get("atr_pct", 0.0) or 0.0)
        candle_bias = float(extra.get("candle_bias", 0.0) or 0.0)
        breakout_pct = float(extra.get("breakout_pct", 0.0) or 0.0)
        drift_5m = float(snapshot.change_5m_pct or 0.0)
        drift_15m = float(extra.get("change_15m_pct", 0.0) or 0.0)
        drift_1h = float(extra.get("change_1h_pct", 0.0) or 0.0)
        drift_4h = float(extra.get("change_4h_pct", 0.0) or 0.0)
        drift_24h = float(extra.get("price_change_24h_pct", 0.0) or 0.0) / 100.0
        spot_price = float(snapshot.reference_price or 0.0)
        ema_fast = float(extra.get("ema_fast_9", 0.0) or 0.0)

        trend_score = setup_score * 0.65
        if ema_spread > 0:
            trend_score += 0.10
        elif ema_spread < 0:
            trend_score -= 0.10
        if ema_15m_spread > 0:
            trend_score += 0.12
        elif ema_15m_spread < 0:
            trend_score -= 0.10
        if ema_1h_spread > 0:
            trend_score += 0.15
        elif ema_1h_spread < 0:
            trend_score -= 0.14
        if spot_price and ema_fast and spot_price > ema_fast:
            trend_score += 0.10
        elif spot_price and ema_fast:
            trend_score -= 0.10
        trend_score += 0.06 if drift_15m > 0 else -0.05
        trend_score += 0.10 if drift_1h > 0 else -0.08
        trend_score += 0.10 if drift_4h > 0 else -0.08
        trend_score += 0.04 if drift_5m > 0 else -0.03
        trend_score += max(-0.10, min(0.10, breakout_pct * 10))
        trend_score += max(-0.12, min(0.12, candle_bias * 0.18))
        trend_score += max(-0.08, min(0.08, drift_24h * 0.8))
        if 48 <= rsi <= 66:
            trend_score += 0.08
        elif rsi > 74 or rsi < 32:
            trend_score -= 0.08
        if atr_pct > 0.03:
            trend_score -= 0.08
        elif 0.004 <= atr_pct <= 0.02:
            trend_score += 0.05

        probability = max(0.05, min(0.95, 0.5 + (trend_score * 0.45)))
        edge = max(0.0, min(0.45, abs(probability - 0.5) * 1.8))
        positive_signals = sum(
            1
            for condition in [
                ema_spread > 0,
                ema_15m_spread > 0,
                ema_1h_spread > 0,
                spot_price > ema_fast if spot_price and ema_fast else False,
                drift_15m > 0,
                drift_1h > 0,
                drift_4h > 0,
                drift_5m > 0,
                breakout_pct > -0.002,
                candle_bias > 0,
                48 <= rsi <= 66,
            ]
            if condition
        )
        confidence = max(0.1, min(0.85, 0.2 + (positive_signals * 0.09) + min(0.2, abs(trend_score) * 0.25)))
        aligned_trend = ema_15m_spread > 0 and ema_1h_spread > 0 and drift_1h > 0
        trigger_ready = breakout_pct > -0.004 and candle_bias > -0.10 and 42 <= rsi <= 70
        bearish_trend = ema_15m_spread < 0 and ema_1h_spread < 0 and (drift_1h < 0 or drift_4h < 0)
        bearish_trigger = breakout_pct < 0.006 and candle_bias < 0.18 and 28 <= rsi <= 60
        if trend_score >= 0.22 and setup_score >= 0.18 and aligned_trend and trigger_ready:
            recommendation = "BUY_YES"
        elif trend_score <= -0.18 and setup_score <= -0.15 and bearish_trend and bearish_trigger:
            recommendation = "BUY_NO"
        else:
            recommendation = "HOLD"
        reasoning = (
            f"Candle strategy on {snapshot.reference_symbol}: "
            f"setup score {setup_score:+.2f}, EMA spread {ema_spread:+.2%}, 15m EMA {ema_15m_spread:+.2%}, "
            f"1h EMA {ema_1h_spread:+.2%}, RSI14 {rsi:.1f}, ATR {atr_pct:.2%}, breakout {breakout_pct:+.2%}, "
            f"15m drift {drift_15m:+.2%}, 1h drift {drift_1h:+.2%}, 4h drift {drift_4h:+.2%}, candle bias {candle_bias:+.2f}. "
            f"Trend score {trend_score:+.2f}."
        )
        return AnalysisResult(
            probability=probability,
            edge=edge,
            recommendation=recommendation,
            confidence=confidence,
            reasoning=reasoning,
        )


class MockAnalyzer(Analyzer):
    def analyze(self, snapshot: MarketSnapshot) -> AnalysisResult:
        drift = snapshot.change_5m_pct or 0.0
        yes_price = snapshot.yes_price
        market_type_bias = {
            "crypto_price": 1.5,
            "crypto_spot": 1.8,
            "fed_rates": 0.4,
            "election": 0.2,
        }.get(snapshot.market_type, 0.5)
        probability = min(0.95, max(0.05, yes_price + (drift * market_type_bias)))
        edge = abs(probability - yes_price)
        if probability - yes_price > 0.10:
            recommendation = "BUY_YES"
        elif (1.0 - probability) - snapshot.no_price > 0.10:
            recommendation = "BUY_NO"
        else:
            recommendation = "HOLD"
        confidence = min(0.9, 0.5 + abs(drift) * (3 if snapshot.market_type == "crypto_price" else 1.2))
        reasoning = f"Mock analysis for {snapshot.market_type} from current prices and payload context."
        return AnalysisResult(
            probability=probability,
            edge=edge,
            recommendation=recommendation,
            confidence=confidence,
            reasoning=reasoning,
        )


class AnthropicAnalyzer(Analyzer):
    def __init__(self, model: str, prompt_path: str | Path):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = model
        self.system_prompt = Path(prompt_path).read_text(encoding="utf-8")

    def analyze(self, snapshot: MarketSnapshot) -> AnalysisResult:
        payload = {
            "market_id": snapshot.market_id,
            "market_type": snapshot.market_type,
            "question": snapshot.question,
            "yes_price": snapshot.yes_price,
            "no_price": snapshot.no_price,
            "reference_symbol": snapshot.reference_symbol,
            "reference_price": snapshot.reference_price,
            "change_5m_pct": snapshot.change_5m_pct,
            "headline_summary": snapshot.headline_summary,
            "volume": snapshot.volume,
            "extra": snapshot.extra,
        }
        response = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            temperature=0.2,
            system=self.system_prompt,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text_blocks = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        raw = "\n".join(text_blocks).strip()
        data = json.loads(self._coerce_json(raw))
        return AnalysisResult(
            probability=float(data["probability"]),
            edge=float(data["edge"]),
            recommendation=str(data["recommendation"]),
            confidence=float(data["confidence"]),
            reasoning=str(data["reasoning"]),
        )

    def _coerce_json(self, raw: str) -> str:
        try:
            return _extract_json_object(raw)
        except ValueError:
            repair_system = (
                "Convert the provided market analysis into valid JSON only. "
                "Return exactly this schema with no markdown: "
                '{"probability":0.0,"edge":0.0,"recommendation":"HOLD","confidence":0.0,"reasoning":"..."}'
            )
            repair = self.client.messages.create(
                model=self.model,
                max_tokens=250,
                temperature=0,
                system=repair_system,
                messages=[{"role": "user", "content": raw}],
            )
            repair_blocks = [block.text for block in repair.content if getattr(block, "type", "") == "text"]
            repaired_raw = "\n".join(repair_blocks).strip()
            return _extract_json_object(repaired_raw)


def _extract_json_object(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return match.group(0)

    raise ValueError(f"Anthropic response did not contain JSON: {raw[:300]}")


def build_analyzer(cfg: dict, root: Path) -> Analyzer:
    provider = cfg["analysis"]["provider"]
    if provider == "mock":
        return MockAnalyzer()
    if provider == "candles":
        return CandleAnalyzer()
    if provider == "anthropic":
        return AnthropicAnalyzer(
            model=cfg["analysis"]["anthropic_model"],
            prompt_path=root / "system_prompt.txt",
        )
    raise ValueError(f"Unsupported analysis provider: {provider}")
