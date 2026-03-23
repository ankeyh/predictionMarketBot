from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MarketSnapshot:
    market_id: str
    market_type: str
    question: str
    yes_price: float
    no_price: float
    reference_symbol: str
    reference_price: float | None = None
    change_5m_pct: float | None = None
    headline_summary: str = ""
    volume: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    probability: float
    edge: float
    recommendation: str
    confidence: float
    reasoning: str


@dataclass
class OrderIntent:
    market_id: str
    side: str
    price: float
    size: float
    probability: float
    edge: float
    confidence: float
    reasoning: str


@dataclass
class Position:
    market_id: str
    side: str
    price: float
    size: float
    opened_at: str


@dataclass
class Fill:
    market_id: str
    side: str
    price: float
    size: float
    notional: float
    status: str
    ts: str
    question: str = ""
    market_slug: str = ""
    end_date_iso: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
