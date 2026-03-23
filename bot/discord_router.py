from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiscordCommand:
    action: str
    reason: str = ""


def parse_discord_command(text: str) -> DiscordCommand | None:
    normalized = " ".join(text.lower().strip().split())
    if not normalized:
        return None

    if normalized in {"prediction bot report", "bot report", "report"}:
        return DiscordCommand(action="report")
    if normalized in {"prediction bot status", "bot status", "status"}:
        return DiscordCommand(action="status")
    if normalized in {"scan markets now", "prediction bot scan", "scan now"}:
        return DiscordCommand(action="scan")
    if normalized.startswith("pause prediction bot"):
        reason = text.strip()[len("pause prediction bot"):].strip(" :-")
        return DiscordCommand(action="pause", reason=reason or "discord pause")
    if normalized in {"resume prediction bot", "resume bot", "resume"}:
        return DiscordCommand(action="resume")
    return None
