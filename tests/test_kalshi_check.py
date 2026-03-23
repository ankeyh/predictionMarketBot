from bot.kalshi_check import format_check


def test_format_check_json():
    payload = {"market_data_ok": True, "auth_ok": False}
    result = format_check(payload)
    assert '"market_data_ok": true' in result
