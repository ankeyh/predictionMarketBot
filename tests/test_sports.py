from bot.sports import extract_rank_hints, extract_teams, fetch_odds_context


def test_extract_teams_handles_at_format():
    teams = extract_teams("Missouri St. at Texas Winner?")
    assert teams == {"away_team": "Missouri St.", "home_team": "Texas"}


def test_extract_rank_hints_finds_common_markers():
    hints = extract_rank_hints(
        [
            "No. 1 Texas opens tournament run after AP Poll dominance",
            "Missouri State faces a Top 25 test on the road",
        ]
    )
    assert "No. 1" in hints
    assert "Top 25" in hints


def test_fetch_odds_context_returns_empty_without_api_key(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert fetch_odds_context("Texas", "Missouri St.") == {}
