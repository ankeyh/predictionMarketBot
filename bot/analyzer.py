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
    if provider == "anthropic":
        return AnthropicAnalyzer(
            model=cfg["analysis"]["anthropic_model"],
            prompt_path=root / "system_prompt.txt",
        )
    raise ValueError(f"Unsupported analysis provider: {provider}")
