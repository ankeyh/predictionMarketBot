# OpenClaw Automation Notes

Recommended scan cadence:

- Every 5 minutes during active market hours for short-dated BTC contracts
- Every 15 to 30 minutes for macro and election markets

Suggested commands:

- `python run_bot.py scan --once`
- `python run_bot.py status`
- `python run_bot.py report`
- `python run_bot.py pause --reason "telegram pause"`
- `python run_bot.py resume`
- `python run_bot.py discord-command --text "prediction bot report"`
- `python run_bot.py discord-command --text "prediction bot status"`
- `python run_bot.py discord-command --text "scan markets now"`
