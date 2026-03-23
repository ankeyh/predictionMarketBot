from __future__ import annotations

from pathlib import Path

from .storage import load_json, save_json


def _control_path(data_dir: Path) -> Path:
    return data_dir / "control.json"


def load_control_state(data_dir: Path) -> dict:
    return load_json(
        _control_path(data_dir),
        {
            "paused": False,
            "reason": "",
        },
    )


def save_control_state(data_dir: Path, paused: bool, reason: str = "") -> dict:
    payload = {"paused": paused, "reason": reason}
    save_json(_control_path(data_dir), payload)
    return payload
