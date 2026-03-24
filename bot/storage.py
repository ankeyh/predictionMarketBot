from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _rotate_if_header_mismatch(path: Path, headers: list[str]) -> bool:
    if not path.exists():
        return False

    with open(path, "r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline().strip()
    existing_headers = first_line.split(",") if first_line else []
    if existing_headers == headers:
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    path.rename(rotated)
    return True


def append_csv(path: Path, row: dict, headers: list[str]) -> None:
    ensure_dir(path.parent)
    _rotate_if_header_mismatch(path, headers)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
