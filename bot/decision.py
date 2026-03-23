from __future__ import annotations

from .models import AnalysisResult, MarketSnapshot, OrderIntent


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def derive_order(snapshot: MarketSnapshot, analysis: AnalysisResult, cfg: dict) -> OrderIntent | None:
    min_edge = cfg["venue"]["min_edge"]
    min_confidence = cfg["venue"]["min_confidence"]
    if cfg.get("execution", {}).get("mode") == "paper":
        overrides = cfg["venue"].get("paper_overrides", {}).get(snapshot.market_type, {})
        min_edge = overrides.get("min_edge", min_edge)
        min_confidence = overrides.get("min_confidence", min_confidence)

    if analysis.recommendation == "HOLD":
        return None
    if analysis.edge < min_edge or analysis.confidence < min_confidence:
        return None

    max_notional = min(
        cfg["execution"]["max_order_notional"],
        cfg["risk"]["max_single_position_notional"],
    )
    confidence_scale = max(0.25, analysis.confidence)
    edge_scale = max(0.25, analysis.edge)
    notional = max_notional * min(1.0, confidence_scale * edge_scale * 2)

    if analysis.recommendation == "BUY_YES":
        price = snapshot.yes_price
        side = "YES"
    elif analysis.recommendation == "BUY_NO":
        price = snapshot.no_price
        side = "NO"
    else:
        return None

    if price <= 0:
        return None

    size = round(notional / price, 4)
    if size <= 0:
        return None

    return OrderIntent(
        market_id=snapshot.market_id,
        side=side,
        price=price,
        size=size,
        probability=clamp(analysis.probability),
        edge=clamp(analysis.edge),
        confidence=clamp(analysis.confidence),
        reasoning=analysis.reasoning.strip(),
    )
