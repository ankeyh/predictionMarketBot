from bot.macro import score_market_regime, score_news_headlines


def test_score_market_regime_detects_risk_on():
    result = score_market_regime(
        {
            "SPY": 0.007,
            "QQQ": 0.011,
            "TLT": -0.004,
            "UUP": -0.003,
            "GLD": -0.002,
            "USO": 0.001,
            "VIXY": -0.015,
        }
    )

    assert result["mode"] == "risk_on"
    assert result["score"] > 0


def test_score_market_regime_detects_risk_off():
    result = score_market_regime(
        {
            "SPY": -0.009,
            "QQQ": -0.013,
            "TLT": 0.006,
            "UUP": 0.004,
            "GLD": 0.009,
            "USO": 0.018,
            "VIXY": 0.03,
        }
    )

    assert result["mode"] == "risk_off"
    assert result["score"] < 0


def test_score_news_headlines_detects_risk_off_terms():
    result = score_news_headlines(
        [
            "Tariff fears rise as oil spike renews recession worries",
            "Hawkish Fed talk pressures markets as sanctions widen",
        ]
    )

    assert result["mode"] == "risk_off"
    assert result["score"] < 0
