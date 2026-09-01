"""Durable local preferences for YURICH."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filesystem_ops import YurichError

STATE_VERSION = 1
STRING_LIMITS = {
    "root": 4096,
    "query": 4096,
    "include": 2048,
    "exclude": 2048,
}
BOOL_KEYS = {"caseSensitive", "wholeWord", "regex", "terminalOpen"}
INTEGER_LIMITS = {"fontSize": (11, 20), "contextLines": (0, 10), "terminalHeight": (150, 2000)}
ENUM_VALUES = {
    "theme": {"auto", "light", "dark"},
    "searchMode": {"content", "filename"},
}
ARRAY_LIMITS = {
    "searchHistory": (30, 4096),
    "recentFolders": (12, 4096),
    "favoriteFolders": (30, 4096),
}


def state_directory() -> Path:
    configured = os.environ.get("YURICH_DATA_DIR") or os.environ.get("PLUGIN_DATA")
    if configured:
        directory = Path(configured).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        directory = Path(os.environ["LOCALAPPDATA"]) / "YURICH"
    else:
        directory = Path.home() / ".local" / "share" / "yurich"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise YurichError(f"Cannot create the YURICH data folder: {error}") from error
    return directory


def state_path() -> Path:
    return state_directory() / "state.json"


def sanitize_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, limit in STRING_LIMITS.items():
        item = value.get(key)
        if isinstance(item, str):
            clean[key] = item[:limit]
    for key in BOOL_KEYS:
        item = value.get(key)
        if isinstance(item, bool):
            clean[key] = item
    for key, (minimum, maximum) in INTEGER_LIMITS.items():
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            clean[key] = min(maximum, max(minimum, item))
    for key, allowed in ENUM_VALUES.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            clean[key] = item
    for key, (maximum_items, maximum_length) in ARRAY_LIMITS.items():
        item = value.get(key)
        if isinstance(item, list):
            values: list[str] = []
            for entry in item:
                if not isinstance(entry, str):
                    continue
                entry = entry.strip()[:maximum_length]
                if entry and entry not in values:
                    values.append(entry)
                if len(values) >= maximum_items:
                    break
            clean[key] = values
    return clean


def load_state(_: dict[str, Any]) -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"status": "empty", "state": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "empty", "state": {}}
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return {"status": "empty", "state": {}}
    return {"status": "ok", "state": sanitize_state(payload.get("state"))}


def save_state(arguments: dict[str, Any]) -> dict[str, Any]:
    clean = sanitize_state(arguments.get("state"))
    directory = state_directory()
    path = directory / "state.json"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=directory,
            prefix=".state-",
            suffix=".tmp",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            temp_name = handle.name
            json.dump({"version": STATE_VERSION, "state": clean}, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as error:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise YurichError(f"Cannot save YURICH preferences: {error}") from error
    return {"status": "success", "state": clean}
