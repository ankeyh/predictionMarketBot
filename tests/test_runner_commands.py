import json
from pathlib import Path

from bot.config import load_config
from bot.runner import command_pause, command_report, command_resume, command_status, process_once


def test_runner_commands_flow(tmp_path: Path, capsys):
    root = tmp_path
    (root / "data").mkdir()
    config_path = root / "config.yaml"
    config_path.write_text(
        """
loop_seconds: 300
venue:
  name: mock
  min_edge: 0.10
  min_confidence: 0.70
  markets:
    - id: btc-5m-up
      market_type: crypto_price
      question: Will BTC/USD be higher in 5 minutes?
      yes_price: 0.46
      no_price: 0.54
      reference_symbol: BTCUSD
      context:
        headline_summary: "BTC moves higher."
analysis:
  provider: mock
  anthropic_model: claude-sonnet-4-20250514
execution:
  enabled: true
  mode: paper
  max_order_notional: 25.0
  max_daily_notional: 100.0
  cooldown_minutes: 15
  allow_repeat_market_orders: false
risk:
  starting_cash: 250.0
  max_open_positions: 2
  max_single_position_notional: 25.0
  daily_loss_limit: 30.0
telemetry:
  data_dir: data
""",
        encoding="utf-8",
    )
    (root / "system_prompt.txt").write_text("unused", encoding="utf-8")
    cfg = load_config(config_path)

    process_once(root, cfg)
    capsys.readouterr()
    command_status(root, cfg)
    status = json.loads(capsys.readouterr().out)
    assert status["paused"] is False

    command_pause(root, cfg, "test pause")
    paused = json.loads(capsys.readouterr().out)
    assert paused["paused"] is True

    process_once(root, cfg)
    skipped_output = capsys.readouterr().out
    assert '"status": "paused"' in skipped_output

    command_resume(root, cfg)
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["paused"] is False

    command_report(root, cfg)
    report = capsys.readouterr().out
    assert "Prediction Bot Report" in report
    assert "Signals logged:" in report
    assert "Closed trades:" in report
    assert "Win rate:" in report
