from bot.discord_router import parse_discord_command


def test_parse_report_command():
    cmd = parse_discord_command("prediction bot report")
    assert cmd is not None
    assert cmd.action == "report"


def test_parse_pause_command_with_reason():
    cmd = parse_discord_command("pause prediction bot for maintenance")
    assert cmd is not None
    assert cmd.action == "pause"
    assert cmd.reason == "for maintenance"
