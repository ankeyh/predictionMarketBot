from bot.venues import AlpacaVenue, HybridVenue, KalshiVenue, PolymarketVenue


def test_infer_reference_symbol_for_crypto_questions():
    assert PolymarketVenue._infer_reference_symbol("Will Bitcoin hit $100,000 by Friday?") == "BTCUSD"
    assert PolymarketVenue._infer_reference_symbol("Will Ethereum trade above $5,000 in April?") == "ETHUSD"
    assert PolymarketVenue._infer_reference_symbol("Will Solana close above $250 this month?") == "SOLUSD"
    assert PolymarketVenue._infer_reference_symbol("Will XRP trade above $5 this year?") == "XRPUSD"


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


def test_market_theme_groups_similar_markets():
    first = PolymarketVenue._market_theme(
        "Will MegaETH perform an airdrop by June 30?",
        "megaeth-airdrop-june-30",
        "crypto_event",
    )
    second = PolymarketVenue._market_theme(
        "MegaETH market cap (FDV) >$6B one day after launch?",
        "megaeth-market-cap-6b",
        "crypto_event",
    )
    assert first == second == "crypto_event:megaeth"


def test_quality_score_prefers_nearer_cleaner_market():
    near_score = PolymarketVenue._quality_score("crypto_price", 0.48, 0.52, 9000.0, 5.0)
    far_score = PolymarketVenue._quality_score("crypto_event", 0.03, 0.97, 100.0, 120.0)
    assert near_score > far_score


def test_infer_market_type_from_text_for_altcoin_price():
    market_type = PolymarketVenue._infer_market_type_from_text(
        "will xrp price trade above $5 by december"
    )
    assert market_type == "crypto_price"


def test_spot_headline_includes_intraday_context():
    headline = AlpacaVenue._spot_headline(
        "BTC-USD",
        {
            "spot_price": 85000.0,
            "change_5m_pct": 0.002,
            "change_1h_pct": 0.011,
            "realized_vol_1h": 0.009,
        },
        {
            "price_change_percentage_24h": 5.5,
            "market_cap_rank": 1,
        },
    )
    assert "5m drift" in headline
    assert "1h drift" in headline
    assert "1h realized vol" in headline


def test_momentum_score_rewards_aligned_trend():
    score = AlpacaVenue._momentum_score(
        {
            "change_5m_pct": 0.004,
            "change_1h_pct": 0.012,
            "realized_vol_1h": 0.006,
        },
        {
            "price_change_percentage_24h": 6.0,
        },
    )
    assert score > 0


def test_reference_context_indicators_compute_from_candles():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]
    ema_fast = PolymarketVenue._ema(closes, 9)
    ema_slow = PolymarketVenue._ema(closes, 21)
    rsi = PolymarketVenue._rsi(closes, 14)
    candles = [[0, close - 2, close + 1, close - 0.5, close, 0] for close in closes]
    atr = PolymarketVenue._atr(candles, 14)
    bias = PolymarketVenue._candle_bias(candles)

    assert ema_fast > 0
    assert ema_slow > 0
    assert rsi > 50
    assert atr > 0
    assert -1.0 <= bias <= 1.0


def test_setup_score_rewards_aligned_multitimeframe_structure():
    score = PolymarketVenue._setup_score(
        {
            "ema_fast_9": 110.0,
            "ema_slow_21": 108.0,
            "ema_15m_fast": 111.0,
            "ema_15m_slow": 109.0,
            "ema_1h_fast": 113.0,
            "ema_1h_slow": 110.0,
            "change_15m_pct": 0.003,
            "change_1h_pct": 0.008,
            "change_4h_pct": 0.015,
            "rsi_14": 58.0,
            "atr_pct": 0.012,
            "candle_bias": 0.35,
            "breakout_pct": 0.004,
        }
    )
    assert score > 0.3


def test_kalshi_infer_market_type_handles_cricket():
    assert KalshiVenue._infer_market_type("Mumbai Indians vs Chennai Super Kings winner?") == "sports"
    assert KalshiVenue._infer_market_type("Who will win today's IPL cricket match?") == "sports"


def test_hybrid_venue_routes_spot_and_sports(monkeypatch):
    venue = HybridVenue()
    monkeypatch.setattr(
        venue.spot,
        "load_markets",
        lambda cfg: [
            type("Snap", (), {"market_type": "crypto_spot", "volume": 1000.0, "extra": {}, "market_id": "BTC/USD"})()
        ],
    )
    monkeypatch.setattr(
        venue.prediction,
        "load_markets",
        lambda cfg: [
            type("Snap", (), {"market_type": "sports", "volume": 10.0, "extra": {"days_to_close": 1}, "market_id": "ipl-1"})()
        ],
    )

    cfg = {"venue": {"allowed_market_types": ["crypto_spot", "sports"], "max_markets": 4, "max_crypto_markets": 2, "max_sports_markets": 2}}
    markets = venue.load_markets(cfg)

    assert len(markets) == 2
    assert markets[0].market_type == "crypto_spot"
    assert markets[1].market_type == "sports"
