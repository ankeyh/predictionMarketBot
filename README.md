# Prediction Market Agent

OpenClaw-friendly prediction market bot scaffold with:

- Swappable venue adapters (`kalshi`, `polymarket`, `mock`)
- Claude-based market analysis with strict JSON parsing
- Paper execution engine with exposure limits and cooldowns
- CLI controls for OpenClaw (`scan`, `status`, `report`, `pause`, `resume`)
- CSV + JSON logging for signals, orders, and portfolio state
- Tests for decision logic and paper fills

## Status

This project now includes a `Kalshi` adapter with:

- public market loading for paper mode
- optional live order placement through Kalshi's official Python SDK
- control commands for OpenClaw and Telegram-driven workflows

The sample config still favors safe operation first. Live execution requires venue-specific credentials, legal review, and tighter ops controls.

The current default paper strategy is crypto-first:

- public Polymarket market discovery for crypto contracts
- Coinbase spot plus 5-minute drift context for BTC/ETH/SOL
- Claude analysis on real crypto market questions
- paper-only execution by default

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python run_bot.py scan --once
pytest -q
```

## Configuration

Main settings live in `config.yaml`.

- `venue.name`: `mock`, `polymarket`, or `kalshi`
- `analysis.provider`: `mock` or `anthropic`
- `execution.mode`: `paper` or `live`
- `venue.kalshi_demo`: use Kalshi demo API for paper testing
- `venue.allowed_market_types`: set to `crypto_price` for crypto-only scanning

## CLI commands

```bash
python run_bot.py scan --once
python run_bot.py status
python run_bot.py report
python run_bot.py serve-dashboard --host 127.0.0.1 --port 8008
python run_bot.py pause --reason "manual stop"
python run_bot.py resume
```

Then open [http://127.0.0.1:8008](http://127.0.0.1:8008) in your browser for a local dashboard showing bot status, recent signals, recent orders, open positions, charts, Discord command shortcuts, and the latest cron output. Raw JSON is available at `/api/summary`.

## Why crypto-first

The Kalshi demo feed is useful for wiring and auth, but it often skews toward sports/event inventory. For signal quality, this bot now defaults to Polymarket public crypto markets in paper mode so the scanner sees actual BTC/ETH/SOL prediction contracts instead of widening into sports just to stay active.

## Environment variables

- `ANTHROPIC_API_KEY` for Claude analysis
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for alerts
- `ODDS_API_KEY` for optional structured sports odds context
- `DISCORD_CHANNEL_URL` optional direct link for the dashboard Discord panel
- `POLYMARKET_API_URL` optional override for Polymarket data API
- `KALSHI_API_URL` optional override for Kalshi API
- `KALSHI_API_KEY_ID` for live Kalshi orders
- `KALSHI_PRIVATE_KEY_PATH` path to the Kalshi RSA private key PEM

For Kalshi demo mode, start from [.env.example](/Users/hiteshreddyankey/Documents/Playground/prediction-market-bot/.env.example).
The runner auto-loads a project-local `.env` file if present.

For sports markets, adding `ODDS_API_KEY` lets the bot enrich payloads with structured head-to-head and spread pricing from The Odds API when available. Without it, the bot falls back to headlines and ranking hints only.

The localhost dashboard now includes browser-side control buttons for `scan`, `pause`, and `resume`, plus copy-ready Discord phrases that match the OpenClaw Discord router.

## Kalshi setup

1. Create a demo account at [demo.kalshi.co](https://demo.kalshi.co/)
2. Generate a demo API key from Account Settings
3. Save:
   - the `API Key ID`
   - the downloaded private key file
4. Export these env vars:

```bash
export KALSHI_API_URL="https://demo-api.kalshi.co/trade-api/v2"
export KALSHI_API_KEY_ID="your-demo-key-id"
export KALSHI_PRIVATE_KEY_PATH="/absolute/path/to/your-demo-private-key.key"
```

5. Verify market data:

```bash
python run_bot.py check-kalshi
```

6. Verify authenticated access:

```bash
python run_bot.py check-kalshi --auth
```

## Layout

- `bot/runner.py` main loop
- `bot/analyzer.py` Claude and mock analyzers
- `bot/venues.py` market adapters
- `bot/paper.py` paper execution engine
- `bot/control.py` pause/resume/status/report commands
- `bot/alerts.py` Telegram notifier
- `system_prompt.txt` analysis prompt template

## OpenClaw integration idea

Run this on a cron-like cadence from OpenClaw:

```bash
cd /Users/hiteshreddyankey/.openclaw/workspace/prediction-market-bot
source .venv/bin/activate
python run_bot.py scan --once
```

Suggested OpenClaw message handlers:

- `"scan kalshi markets"` -> `python run_bot.py scan --once`
- `"prediction bot status"` -> `python run_bot.py status`
- `"prediction bot report"` -> `python run_bot.py report`
- `"pause prediction bot"` -> `python run_bot.py pause --reason "telegram pause"`
- `"resume prediction bot"` -> `python run_bot.py resume`

Discord/OpenClaw command router:

```bash
python run_bot.py discord-command --text "prediction bot report"
python run_bot.py discord-command --text "prediction bot status"
python run_bot.py discord-command --text "scan markets now"
python run_bot.py discord-command --text "pause prediction bot for maintenance"
python run_bot.py discord-command --text "resume prediction bot"
```

Example 5-minute cron entry is included in [openclaw/cron.example](/Users/hiteshreddyankey/Documents/Playground/prediction-market-bot/openclaw/cron.example).

## Kalshi notes

The live adapter uses Kalshi's official SDK and credentials model. As of March 20, 2026, the official docs recommend `kalshi_python_sync` or `kalshi_python_async` and API-key plus RSA-PSS authentication:

- [Kalshi SDK overview](https://docs.kalshi.com/sdks/overview)
- [Python SDK quick start](https://docs.kalshi.com/python-sdk)
- [Create order API](https://docs.kalshi.com/api-reference/orders/create-order)
- [API keys](https://docs.kalshi.com/getting_started/api_keys)
- [Demo environment](https://docs.kalshi.com/getting_started/demo_env)
