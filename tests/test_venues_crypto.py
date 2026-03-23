from bot.venues import PolymarketVenue


def test_infer_reference_symbol_for_crypto_questions():
    assert PolymarketVenue._infer_reference_symbol("Will Bitcoin hit $100,000 by Friday?") == "BTCUSD"
    assert PolymarketVenue._infer_reference_symbol("Will Ethereum trade above $5,000 in April?") == "ETHUSD"
    assert PolymarketVenue._infer_reference_symbol("Will Solana close above $250 this month?") == "SOLUSD"


def test_crypto_contract_context_parses_range():
    context = PolymarketVenue._crypto_contract_context(
        "Will Bitcoin be between $95,000 and $100,000 on March 31?",
        {"product": "BTC-USD", "spot_price": 97850.0, "change_5m_pct": 0.0042},
    )

    assert context["contract_style"] == "range"
    assert context["lower_bound"] == 95000.0
    assert context["upper_bound"] == 100000.0
    assert context["spot_inside_range"] is True
    assert "headline_summary" in context


def test_crypto_contract_context_parses_single_target():
    context = PolymarketVenue._crypto_contract_context(
        "Will Ethereum be above $4,000 by the end of the week?",
        {"product": "ETH-USD", "spot_price": 3850.0, "change_5m_pct": -0.0015},
    )

    assert context["target_price"] == 4000.0
    assert context["distance_to_target"] == 150.0
    assert context["contract_style"] == "above"


def test_infer_market_type_from_text_for_crypto_event():
    market_type = PolymarketVenue._infer_market_type_from_text(
        "will megaeth perform an airdrop by june 30 megaeth airdrop token"
    )
    assert market_type == "crypto_event"
