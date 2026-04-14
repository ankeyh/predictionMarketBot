from __future__ import annotations

from functools import lru_cache
import os
import re
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests


def extract_teams(question: str) -> dict[str, str]:
    text = question.replace("?", "").strip()
    lowered = text.lower()

    if ": first half winner" in lowered and " vs " in lowered:
        left, right = text.split(": First Half Winner", 1)[0].split(" vs ", 1)
        return {"team_a": left.strip(), "team_b": right.strip()}
    if " at " in lowered and " winner" in lowered:
        left, right = text.rsplit(" Winner", 1)[0].split(" at ", 1)
        return {"away_team": left.strip(), "home_team": right.strip()}
    if " vs " in lowered and " winner" in lowered:
        left, right = text.rsplit(" Winner", 1)[0].split(" vs ", 1)
        return {"team_a": left.strip(), "team_b": right.strip()}
    return {}


@lru_cache(maxsize=64)
def google_news_headlines(query: str, limit: int = 3) -> list[str]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}"
    response = requests.get(url, timeout=4)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    headlines: list[str] = []
    for item in root.findall(".//item/title"):
        title = (item.text or "").strip()
        if not title:
            continue
        headlines.append(title)
        if len(headlines) >= limit:
            break
    return headlines


def extract_rank_hints(headlines: list[str]) -> list[str]:
    hints: list[str] = []
    patterns = [
        r"\bNo\.\s?\d+\b",
        r"\bTop\s?25\b",
        r"\bAP\s+Poll\b",
        r"\bseed(?:ed)?\s+\d+\b",
        r"\bNo\.\s?\d+\s+seed\b",
    ]
    for headline in headlines:
        for pattern in patterns:
            for match in re.findall(pattern, headline, flags=re.IGNORECASE):
                cleaned = match.strip()
                if cleaned not in hints:
                    hints.append(cleaned)
    return hints[:6]


def _normalize_team_name(team: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", team.lower())
    cleaned = re.sub(r"\b(state|st)\b", "state", cleaned)
    cleaned = re.sub(r"\buniv(?:ersity)?\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _team_aliases(team: str) -> set[str]:
    normalized = _normalize_team_name(team)
    aliases = {normalized}
    aliases.add(normalized.replace(" state", " st"))
    aliases.add(normalized.replace(" st", " state"))
    aliases = {alias.strip() for alias in aliases if alias.strip()}
    return aliases


def _match_outcome_team_name(teams: list[str], outcome_name: str) -> str | None:
    outcome_normalized = _normalize_team_name(outcome_name)
    for team in teams:
        for alias in _team_aliases(team):
            if alias == outcome_normalized or alias in outcome_normalized or outcome_normalized in alias:
                return team
    return None


def fetch_odds_context(team_a: str, team_b: str) -> dict:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return {}
    return _fetch_odds_context_cached(api_key, team_a, team_b)


@lru_cache(maxsize=64)
def _fetch_odds_context_cached(api_key: str, team_a: str, team_b: str) -> dict:

    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads",
        "oddsFormat": "american",
    }
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    events = response.json()

    teams = [team_a, team_b]
    for event in events:
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        event_names = [home_team, away_team]
        if not all(any(_match_outcome_team_name(teams, name) for name in [event_name]) for event_name in event_names):
            continue

        result: dict[str, object] = {
            "odds_sport_key": event.get("sport_key", ""),
            "odds_sport_title": event.get("sport_title", ""),
            "odds_home_team": home_team,
            "odds_away_team": away_team,
        }
        bookmakers = event.get("bookmakers") or []
        if not bookmakers:
            return result

        bookmaker = bookmakers[0]
        result["odds_bookmaker"] = bookmaker.get("title", "")
        markets = bookmaker.get("markets") or []
        h2h_prices: dict[str, int | None] = {}
        spread_prices: dict[str, dict[str, float | int | None]] = {}
        for market in markets:
            key = market.get("key")
            for outcome in market.get("outcomes") or []:
                outcome_name = outcome.get("name", "")
                matched_team = _match_outcome_team_name(teams, outcome_name)
                if not matched_team:
                    continue
                if key == "h2h":
                    h2h_prices[matched_team] = outcome.get("price")
                if key == "spreads":
                    spread_prices[matched_team] = {
                        "point": outcome.get("point"),
                        "price": outcome.get("price"),
                    }
        if h2h_prices:
            result["odds_h2h"] = h2h_prices
        if spread_prices:
            result["odds_spreads"] = spread_prices
        return result

    return {}


def build_sports_context(question: str) -> dict:
    teams = extract_teams(question)
    lowered = question.lower()
    sport = "cricket" if any(token in lowered for token in ["ipl", "cricket", "t20", "odi", "test match"]) else "sports"
    league = "IPL" if "ipl" in lowered else ""
    if not teams:
        return {"sport": sport, "league": league}

    ordered_teams = list(teams.values())
    query = " vs ".join(ordered_teams)
    matchup_headlines: list[str] = []
    odds_headlines: list[str] = []
    team_headlines: dict[str, list[str]] = {}
    ranking_headlines: dict[str, list[str]] = {}
    form_headlines: dict[str, list[str]] = {}
    cricket_headlines: list[str] = []

    matchup_query = f"{query} preview odds injury"
    odds_query = f"{query} odds spread line preview"
    team_query_suffix = "injury preview"
    ranking_query_suffix = "ranking ap poll seed"
    if sport == "cricket":
        matchup_query = f"{query} IPL preview playing xi toss injury form"
        odds_query = f"{query} IPL prediction betting odds preview"
        team_query_suffix = "IPL injury playing xi form"
        ranking_query_suffix = ""

    try:
        matchup_headlines = google_news_headlines(matchup_query)
    except Exception:
        matchup_headlines = []
    try:
        odds_headlines = google_news_headlines(odds_query, limit=3)
    except Exception:
        odds_headlines = []
    if sport == "cricket":
        try:
            cricket_headlines = google_news_headlines(f"{query} IPL toss playing xi fantasy preview", limit=4)
        except Exception:
            cricket_headlines = []

    for team in ordered_teams[:2]:
        if sport == "cricket":
            team_headlines[team] = []
            ranking_headlines[team] = []
            form_headlines[team] = []
            continue
        try:
            team_headlines[team] = google_news_headlines(f"{team} {team_query_suffix}", limit=2)
        except Exception:
            team_headlines[team] = []
        if ranking_query_suffix:
            try:
                ranking_headlines[team] = google_news_headlines(f"{team} {ranking_query_suffix}", limit=2)
            except Exception:
                ranking_headlines[team] = []
        else:
            ranking_headlines[team] = []

    combined = list(matchup_headlines)
    combined.extend(odds_headlines)
    combined.extend(cricket_headlines)
    for headlines in team_headlines.values():
        combined.extend(headlines)
    for headlines in ranking_headlines.values():
        combined.extend(headlines)
    for headlines in form_headlines.values():
        combined.extend(headlines)

    rank_hints = extract_rank_hints(combined)
    odds_context: dict = {}
    if len(ordered_teams) >= 2:
        try:
            odds_context = fetch_odds_context(ordered_teams[0], ordered_teams[1])
        except Exception:
            odds_context = {}

    return {
        **teams,
        "matchup_query": query,
        "sport": sport,
        "league": league,
        "sports_headlines": matchup_headlines,
        "cricket_headlines": cricket_headlines,
        "odds_headlines": odds_headlines,
        "team_headlines": team_headlines,
        "ranking_headlines": ranking_headlines,
        "form_headlines": form_headlines,
        "rank_hints": rank_hints,
        "headline_summary": " | ".join(combined[:7]),
        **odds_context,
    }
